"""Software Engineering plugin.

Deterministic operations (no model needed): inspect_repo, run_checks, apply_files.
Agent operations (need a verified workspace-writing runtime): implement,
implement_reviewed, parallel_implement. Nothing is ever committed, merged or pushed.
"""

import asyncio
import json
import re
from collections import Counter

from app.engine import StepError, StepOutput
from app.intake import TaskRequest
from app.plugins.base import Classification, OperationSpec, PlanResult, PluginManifest
from app.workflow import Criterion, GateResult, RetryPolicy, StepDef, WorkflowDef

KEYWORDS = {
    "code": 0.3,
    "bug": 0.35,
    "fix": 0.25,
    "refactor": 0.4,
    "test": 0.25,
    "tests": 0.3,
    "repository": 0.35,
    "repo": 0.35,
    "function": 0.25,
    "lint": 0.4,
    "diff": 0.35,
    "implement": 0.35,
    "python": 0.25,
    "commit": 0.2,
    "compile": 0.25,
    "pytest": 0.45,
    "debug": 0.35,
    "module": 0.2,
    "api": 0.15,
    "review": 0.15,
    "codebase": 0.4,
}
FILES_SCHEMA = {
    "type": "object",
    "properties": {
        "files": {
            "type": "object",
            "minProperties": 1,
            "maxProperties": 20,
            "additionalProperties": {"type": "string", "maxLength": 200_000},
        }
    },
    "required": ["files"],
}


class SoftwarePlugin:
    manifest = PluginManifest(
        id="software",
        name="Software Engineering",
        version="1.0.0",
        description="Repository inspection, checks, scoped changes in isolated worktrees, review.",
        categories=["software", "coding", "testing", "review"],
        keywords=list(KEYWORDS),
        permissions=["workspace:read", "workspace:write", "process:execute", "artifact:write"],
        artifact_types=["diff", "report", "log"],
        operations=[
            OperationSpec(
                task_type="inspect_repo",
                description="Git status, file inventory and structure report",
                required_capabilities=["tool:git.status", "tool:workspace.list_files"],
            ),
            OperationSpec(
                task_type="run_checks",
                description="Run every configured QA check in parallel",
                required_capabilities=["tool:qa.run"],
            ),
            OperationSpec(
                task_type="apply_files",
                description="Create the given files in an isolated worktree, run checks, report the diff",
                required_capabilities=["tool:workspace.create_file", "tool:git.diff"],
                input_schema=FILES_SCHEMA,
                side_effects=True,
                example={"data": {"files": {"greeting.py": "def greet():\n    return 'hi'\n"}}},
            ),
            OperationSpec(
                task_type="implement",
                description="Agent implements a scoped change in an isolated worktree, then QA",
                required_capabilities=["workspace.write"],
                side_effects=True,
            ),
            OperationSpec(
                task_type="implement_reviewed",
                description="Implement, QA, independent review and bounded fix rounds",
                required_capabilities=["workspace.write", "model.structured"],
                side_effects=True,
            ),
            OperationSpec(
                task_type="parallel_implement",
                description="Two implementers in separate worktrees, compared",
                required_capabilities=["workspace.write"],
                side_effects=True,
            ),
        ],
    )

    def classify(self, req: TaskRequest, attachments: list) -> Classification | None:
        words = re.findall(r"[a-z]+", req.objective.lower())
        hits = {w for w in words if w in KEYWORDS}
        score = min(1.0, sum(KEYWORDS[w] for w in hits))
        if "files" in req.data:
            score = max(score, 0.9)
        if score <= 0:
            return None
        if "files" in req.data:
            ttype = "apply_files"
        elif hits & {"test", "tests", "lint", "pytest", "checks"} and not hits & {
            "implement",
            "fix",
            "refactor",
            "bug",
        }:
            ttype = "run_checks"
        elif hits & {"implement", "fix", "refactor", "bug", "debug"}:
            ttype = "implement_reviewed"
        else:
            ttype = "inspect_repo"
        return Classification("software", ttype, score, [f"keywords: {sorted(hits)}"])

    def plan(self, req: TaskRequest, task_type: str, env) -> PlanResult:
        builder = getattr(self, f"_plan_{task_type}")
        return builder(req, env)

    def _plan_inspect_repo(self, req, env) -> PlanResult:
        steps = [
            StepDef(id="status", executor="tool", ref="git.status", permissions=["workspace:read"]),
            StepDef(
                id="files",
                executor="tool",
                ref="workspace.list_files",
                permissions=["workspace:read"],
            ),
            StepDef(
                id="report",
                executor="plugin",
                ref="software.repo_report",
                depends_on=["status", "files"],
                inputs={"status": "$steps.status.output", "files": "$steps.files.output.files"},
                output_schema={"type": "object", "required": ["files", "by_extension"]},
            ),
            StepDef(
                id="verify",
                executor="validator",
                ref="core.artifacts_readable",
                depends_on=["report"],
                inputs={"artifact_ids": "$steps.report.artifacts"},
            ),
        ]
        limitations = (
            []
            if env.workspace_configured
            else ["WORKSPACE_ROOT not set: inspected the task scratch directory"]
        )
        return PlanResult(
            WorkflowDef(
                id="software.inspect_repo",
                domain="software",
                steps=steps,
                success_criteria=[Criterion(kind="artifact_exists", step="report")],
                outputs={"report": "$steps.report.output"},
            ),
            limitations=limitations,
        )

    def _check_steps(self, env, after: list[str]) -> list[StepDef]:
        return [
            StepDef(
                id=f"check_{re.sub(r'[^a-z0-9_]', '_', c.lower())}"[:60],
                executor="tool",
                ref="qa.run",
                inputs={"check": c},
                depends_on=after,
                on_failure="continue",
                permissions=["workspace:read", "process:execute"],
            )
            for c in env.qa_checks
        ]

    def _plan_run_checks(self, req, env) -> PlanResult:
        if not env.qa_checks:
            return PlanResult(
                question="No QA checks are configured (QA_COMMANDS). Which other operation?",
                options=["software:inspect_repo"],
            )
        checks = self._check_steps(env, [])
        gate = StepDef(
            id="gate",
            executor="validator",
            ref="software.checks_passed",
            depends_on=[c.id for c in checks],
            inputs={"results": [f"$steps.{c.id}.output" for c in checks]},
        )
        return PlanResult(
            WorkflowDef(
                id="software.run_checks",
                domain="software",
                failure_policy="continue",
                steps=[*checks, gate],
                outputs={"gate": "$steps.gate.output"},
            )
        )

    def _plan_apply_files(self, req, env) -> PlanResult:
        steps: list[StepDef] = []
        prev: list[str] = []
        for i, (path, content) in enumerate(sorted(req.data["files"].items())):
            sid = f"write_{i}"
            # Sequential: one writer per worktree at a time.
            steps.append(
                StepDef(
                    id=sid,
                    executor="tool",
                    ref="workspace.create_file",
                    depends_on=prev,
                    inputs={"path": path, "content": content},
                    requires_approval=i == 0,
                    permissions=["workspace:read", "workspace:write"],
                )
            )
            prev = [sid]
        checks = self._check_steps(env, prev)
        diff = StepDef(
            id="diff",
            executor="tool",
            ref="git.diff",
            depends_on=prev,
            permissions=["workspace:read"],
        )
        status = StepDef(
            id="status",
            executor="tool",
            ref="git.status",
            depends_on=prev,
            permissions=["workspace:read"],
        )
        after = ["diff", "status", *[c.id for c in checks]]
        report = StepDef(
            id="report",
            executor="plugin",
            ref="software.change_report",
            depends_on=after,
            on_failure="continue",
            inputs={
                "diff": "$steps.diff.output",
                "status": "$steps.status.output.changed",
                "expected": sorted(req.data["files"]),
                "checks": [f"$steps.{c.id}.output" for c in checks],
            },
        )
        steps += [diff, status, *checks, report]
        crit = [Criterion(kind="output_equals", step="report", path="files_match", value=True)]
        if checks:
            steps.append(
                StepDef(
                    id="gate",
                    executor="validator",
                    ref="software.checks_passed",
                    depends_on=[c.id for c in checks],
                    inputs={"results": [f"$steps.{c.id}.output" for c in checks]},
                )
            )
        limitations = [] if checks else ["no QA checks configured: tests were NOT run"]
        return PlanResult(
            WorkflowDef(
                id="software.apply_files",
                domain="software",
                steps=steps,
                failure_policy="continue",
                success_criteria=crit,
                outputs={"report": "$steps.report.output"},
            ),
            limitations=limitations,
        )

    def _agent_plan(self, name: str, req, env) -> PlanResult:
        if not env.workspace_configured:
            return PlanResult(
                question="WORKSPACE_ROOT is not configured, so no repository can be changed. "
                "Which read-only operation instead?",
                options=["software:inspect_repo"],
            )
        step = StepDef(
            id="work",
            executor="plugin",
            ref=f"software.{name}",
            timeout_s=7200,
            retry=RetryPolicy(max_attempts=1),
            required_capabilities=["workspace.write"],
        )
        return PlanResult(
            WorkflowDef(
                id=f"software.{name}",
                domain="software",
                steps=[step],
                timeout_s=7200,
                outputs={"result": "$steps.work.output"},
            )
        )

    def _plan_implement(self, req, env):
        return self._agent_plan("implement", req, env)

    def _plan_implement_reviewed(self, req, env):
        return self._agent_plan("implement_reviewed", req, env)

    def _plan_parallel_implement(self, req, env):
        return self._agent_plan("parallel_implement", req, env)

    def tools(self, services) -> list:
        return []

    def idempotent_operations(self) -> set[str]:
        return {"repo_report", "change_report"}

    def operations(self) -> dict:
        return {
            "repo_report": self.op_repo_report,
            "change_report": self.op_change_report,
            "implement": self.op_implement,
            "implement_reviewed": self.op_implement_reviewed,
            "parallel_implement": self.op_parallel,
        }

    def validators(self) -> dict:
        return {"checks_passed": self.gate_checks_passed}

    # ---------- operations ----------

    async def op_repo_report(self, ctx, inputs: dict) -> StepOutput:
        files = inputs["files"]
        by_ext = Counter(
            (f.rsplit(".", 1)[-1] if "." in f.rsplit("/", 1)[-1] else "(none)") for f in files
        )
        top = Counter(f.split("/", 1)[0] if "/" in f else "." for f in files)
        data = {
            "files": len(files),
            "by_extension": dict(by_ext.most_common()),
            "top_level": dict(top.most_common(20)),
            "changed": inputs["status"].get("changed", []),
        }
        md = ["# Repository inspection", "", f"Files: {len(files)}", "", "## By extension"]
        md += [f"- {k}: {v}" for k, v in by_ext.most_common()]
        md += ["", "## Uncommitted changes", *([f"- {c}" for c in data["changed"]] or ["- none"])]
        art = ctx.add_artifact("report", "repo_report.md", "\n".join(md) + "\n")
        return StepOutput(data, [art.artifact_id])

    async def op_change_report(self, ctx, inputs: dict) -> StepOutput:
        changed = sorted(inputs.get("status") or [])
        expected = inputs["expected"]
        checks = [c for c in inputs.get("checks") or [] if isinstance(c, dict)]
        data = {
            "changed_files": changed,
            "expected_files": expected,
            "files_match": sorted(expected) == changed,
            "checks": {c.get("name"): c.get("status") for c in checks},
            "exit_codes": {c.get("name"): c.get("exit_code") for c in checks},
            "diff_bytes": (inputs.get("diff") or {}).get("bytes"),
        }
        md = [
            "# Change report",
            "",
            "## Changed files (git status)",
            *[f"- {f}" for f in changed],
            "",
            "## Checks",
            *(
                [f"- {k}: {v} (exit {data['exit_codes'][k]})" for k, v in data["checks"].items()]
                or ["- none configured: tests NOT run"]
            ),
            "",
            f"Files match request: {data['files_match']}",
            "",
            "Nothing was committed or merged.",
        ]
        art = ctx.add_artifact("report", "change_report.md", "\n".join(md) + "\n")
        return StepOutput(data, [art.artifact_id])

    async def _approve_and_worktree(self, ctx, conn, what: str):
        from app import formatting as fmt

        await ctx.approval(
            f"🔐 <b>{conn.name}</b> will {what} for {fmt.task_line(ctx.task)}\n"
            "Edits stay in an isolated worktree; nothing is merged. Approve?"
        )
        return await ctx.svc.workspaces.create(ctx.task_id)

    def _implementer(self, ctx):
        from app.connectors.base import Cap

        svc = ctx.svc
        return svc.router.route({Cap.workspace_write}, svc._pref("implementer"), ctx.task.agent)

    async def op_implement(self, ctx, inputs: dict) -> StepOutput:
        conn = self._implementer(ctx)
        ws = await self._approve_and_worktree(
            ctx, conn, "edit files in a new isolated git worktree"
        )
        agents = ctx.agents()
        _, impl = await agents.implement(ctx.task, ws, ctx.objective, "Implement", connector=conn)
        qa = await agents.qa(ctx.task, ws)
        ctx.task.result = f"Branch <code>{ws.branch}</code> (not merged)\n\n{impl.text}"
        if any(not r.passed for r in qa):
            raise StepError("QA checks failed", "qa_failed", data={r.name: r.status for r in qa})
        return StepOutput({"branch": ws.branch, "qa": {r.name: r.status for r in qa}})

    async def op_implement_reviewed(self, ctx, inputs: dict) -> StepOutput:
        conn = self._implementer(ctx)
        ws = await self._approve_and_worktree(
            ctx, conn, "implement, then an independent reviewer checks the diff"
        )
        agents = ctx.agents()
        step, impl = await agents.implement(
            ctx.task, ws, ctx.objective, "Implement", connector=conn
        )
        verdict = await agents.review_loop(ctx.task, ws, impl, step)
        if not verdict["ok"]:
            raise StepError(
                f"{verdict['blocking']} blocking findings, {len(verdict['qa_failed'])} failed QA checks",
                "review_blocked",
                data=verdict,
            )
        return StepOutput(verdict)

    async def op_parallel(self, ctx, inputs: dict) -> StepOutput:
        from app.connectors.base import Cap, UnsupportedCapability

        svc = ctx.svc
        writers = [
            c
            for c in svc.router.connectors.values()
            if c.available and Cap.workspace_write in c.caps
        ]
        if not writers:
            raise UnsupportedCapability("no available agent can write to a workspace")
        pair = (writers * 2)[:2]  # same runtime twice if only one is available
        await ctx.approval(
            f"🔐 Run {pair[0].name} and {pair[1].name} in two separate worktrees? "
            "Nothing is merged automatically."
        )
        agents = ctx.agents()

        async def one(i, conn):
            ws = await svc.workspaces.create(f"{ctx.task_id}_{i}")
            step, _ = await agents.implement(
                ctx.task, ws, ctx.objective, f"Candidate {i} ({conn.name})", connector=conn
            )
            diff, files = await svc.workspaces.diff(ws)
            qa = await agents.qa(ctx.task, ws)
            u = step.usage[-1]
            return {
                "candidate": i,
                "agent": conn.name,
                "branch": ws.branch,
                "files_changed": len(files),
                "diff_lines": diff.count("\n"),
                "qa_passed": f"{sum(r.passed for r in qa)}/{len(qa)}" if qa else "not run",
                "seconds": round(u.duration_ms / 1000, 1),
                "cost_usd_estimate": u.cost_usd,
            }

        results = await asyncio.gather(*(one(i, c) for i, c in enumerate(pair, 1)))
        art = ctx.add_artifact("report", "comparison.json", json.dumps(results, indent=2))
        ctx.task.result = "Comparison (objective metrics only; review the diffs before choosing):"
        from app import formatting as fmt

        await ctx.notify("<b>Parallel comparison</b>\n" + fmt.json_block(results))
        return StepOutput({"candidates": results}, [art.artifact_id])

    async def gate_checks_passed(self, ctx, inputs: dict) -> GateResult:
        results = [r for r in inputs.get("results") or [] if isinstance(r, dict)]
        if not results:
            return GateResult(
                validator_id="software.checks_passed",
                status="skipped",
                findings=[{"severity": "high", "message": "no check results"}],
            )
        bad = [r for r in results if r.get("status") != "passed"]
        status = (
            "passed"
            if not bad
            else ("unknown" if all(r.get("status") == "skipped" for r in bad) else "failed")
        )
        return GateResult(
            validator_id="software.checks_passed",
            status=status,
            evidence=[r["artifact_id"] for r in results if r.get("artifact_id")],
            findings=[
                {
                    "severity": "high",
                    "message": f"{r.get('name')}: {r.get('status')} exit={r.get('exit_code')}",
                }
                for r in bad
            ],
            metrics={"checks": len(results), "passed": len(results) - len(bad)},
        )


def create_plugin():
    return SoftwarePlugin()
