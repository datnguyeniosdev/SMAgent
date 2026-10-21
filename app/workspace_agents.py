"""Generic "agent edits an isolated workspace, then independent review" primitives.

Domain-neutral: plugins compose these with their own prompts.
Moved here from the Phase 1 orchestrator without behaviour changes.
"""

import json

from pydantic import ValidationError

from app import formatting as fmt
from app.connectors.base import (
    AgentConnector,
    AgentRequest,
    AgentResult,
    Cap,
    ConnectorError,
    UnsupportedCapability,
)
from app.models import ReviewFinding, Task, TaskState
from app.qa import QAResult
from app.workspace import Workspace

BLOCKING = {"critical", "high"}
FINDINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"enum": ["critical", "high", "medium", "low", "info"]},
                    "description": {"type": "string"},
                    "location": {"type": "string"},
                    "reproduction": {"type": "string"},
                    "evidence": {"type": "string"},
                    "remediation": {"type": "string"},
                    "verification_status": {"enum": ["verified", "unverified"]},
                },
                "required": ["severity", "description", "location", "evidence", "remediation"],
            },
        },
    },
    "required": ["summary", "findings"],
}
IMPLEMENTER_SYSTEM = (
    "You are the implementer. Work only inside the current directory, which is an isolated "
    "workspace. Do not commit, push, merge or delete branches. Use only tools that exist and "
    "verify each operation's result. End with a short summary of what you changed and what you "
    "could not verify."
)
REVIEWER_SYSTEM = (
    "You are an independent reviewer. Judge the actual diff and evidence against the "
    "requirements; do not trust the implementer's summary. Report concrete defects with file:line, "
    "evidence and remediation. Mark a finding verified only if you confirmed it in the code or "
    "the provided test output. A clean review is not proof the build or runtime works."
)


class WorkspaceAgents:
    def __init__(self, svc, extra_system: str = ""):
        self.svc = svc
        self.extra_system = extra_system

    async def implement(
        self,
        parent: Task,
        ws: Workspace,
        prompt: str,
        title: str,
        resume: str | None = None,
        connector: AgentConnector | None = None,
        depends_on=(),
    ) -> tuple[Task, AgentResult]:
        svc = self.svc
        conn = connector or svc.router.route(
            {Cap.workspace_write}, svc._pref("implementer"), parent.agent
        )
        step = svc._child(parent, "implementer", title, depends_on)
        svc.workspaces.acquire_write(ws, step.task_id)
        try:
            req = AgentRequest(
                task_id=step.task_id,
                prompt=prompt,
                system=(IMPLEMENTER_SYSTEM + " " + self.extra_system).strip(),
                attachments=svc._task_attachments(parent),
                workspace=ws.path,
                read_only=False,
                resume_session=resume if Cap.session_resume in conn.caps else None,
            )
            result = await svc._call(step, conn, req, "implementer", ws)
        finally:
            svc.workspaces.release_write(ws, step.task_id)
        step.result = result.text
        diff, files = await svc.workspaces.diff(ws)
        svc.artifacts.add(
            step.task_id, "diff", f"{step.task_id}.diff", diff, files=files, producer=conn.name
        )
        svc._set_state(step, TaskState.succeeded)
        return step, result

    async def qa(self, parent: Task, ws: Workspace) -> list[QAResult]:
        svc = self.svc
        if not svc.qa.commands:
            await svc._notify(
                parent, "No QA commands configured: tests/builds were NOT run.", "warn"
            )
            return []
        step = svc._child(parent, "qa", "QA checks")
        results = await svc.qa.run_all(ws.path, step.task_id)
        log_text = "\n\n".join(
            f"$ {r.command}\nstatus={r.status} exit={r.exit_code} {r.duration_s}s\n{r.output_tail}"
            for r in results
        )
        svc.artifacts.add(step.task_id, "log", "qa.log", log_text)
        lines = [
            f"{'✅' if r.passed else '❌'} {r.name}: {r.status} ({r.duration_s}s)" for r in results
        ]
        failed = [r for r in results if not r.passed]
        html = "\n".join(lines)
        if failed:
            html += "\n" + fmt.log_block(failed[0].output_tail, 1500)
        await svc._notify(parent, html, "qa")
        step.result = "\n".join(lines)
        svc._set_state(step, TaskState.failed if failed else TaskState.succeeded)
        return results

    async def review(
        self,
        parent: Task,
        ws: Workspace,
        diff: str,
        qa: list[QAResult],
        exclude: str | None,
        extra: str = "",
    ) -> list[ReviewFinding]:
        svc = self.svc
        need = {Cap.text, Cap.structured_output}
        pref = svc._pref("reviewer")
        try:  # prefer a different provider than the implementer for independence
            conn = svc.router.route(need, pref, exclude={exclude} if exclude else set())
        except UnsupportedCapability:
            conn = svc.router.route(need, pref)
        step = svc._child(parent, "reviewer", "Independent review")
        qa_text = (
            "\n\n".join(
                f"[{r.name}] status={r.status} exit={r.exit_code}\n{r.output_tail[-4000:]}"
                for r in qa
            )
            or "No automated checks were run."
        )
        prompt = (
            f"Requirements:\n{parent.description}\n\n{extra}\n\n"
            f"Diff against base {ws.base_commit[:10]}:\n{fmt.truncate(diff, 150_000)}\n\n"
            f"Automated check output:\n{qa_text}"
        )
        read_ws = Cap.workspace_read in conn.caps
        req = AgentRequest(
            task_id=step.task_id,
            prompt=prompt,
            system=REVIEWER_SYSTEM,
            workspace=ws.path if read_ws else None,
            read_only=True,
            json_schema=FINDINGS_SCHEMA,
        )
        result = await svc._call(step, conn, req, "reviewer", ws if read_ws else None)
        try:
            data = result.structured or json.loads(result.text)
            findings = [ReviewFinding(**f) for f in data["findings"]]
        except (ValueError, KeyError, TypeError, ValidationError) as e:
            step.error = f"reviewer returned invalid findings: {e}"
            svc._set_state(step, TaskState.failed)
            raise ConnectorError(step.error, "provider") from e
        step.findings = findings
        step.result = data.get("summary", "")
        svc.artifacts.add(
            step.task_id,
            "review",
            "review.json",
            json.dumps(data, indent=2),
            reviewer=conn.name,
            producer=conn.name,
        )
        await svc._notify(parent, fmt.findings_report(findings), "review")
        svc._set_state(step, TaskState.succeeded)
        return findings

    async def review_loop(
        self, task: Task, ws: Workspace, impl: AgentResult, step: Task, extra: str = ""
    ) -> dict:
        """QA -> review -> bounded fix rounds. Returns a verdict; never marks the task itself."""
        svc = self.svc
        qa = await self.qa(task, ws)
        findings, blocking, qa_failed = [], [], []
        for round_no in range(svc.s.max_fix_rounds + 1):
            diff, _ = await svc.workspaces.diff(ws)
            findings = await self.review(task, ws, diff, qa, step.provider, extra)
            blocking = [f for f in findings if f.severity in BLOCKING]
            qa_failed = [r for r in qa if not r.passed]
            if (not blocking and not qa_failed) or round_no == svc.s.max_fix_rounds:
                break
            fix = "Fix these issues found by an independent reviewer and QA:\n" + "\n".join(
                f"- [{f.severity}] {f.location}: {f.description} -> {f.remediation}"
                for f in blocking
            )
            fix += "".join(f"\n- QA `{r.name}` failed:\n{r.output_tail[-3000:]}" for r in qa_failed)
            step, impl = await self.implement(
                task,
                ws,
                fix,
                f"Fix round {round_no + 1}",
                resume=impl.session_id,
                connector=svc.router.connectors[step.provider],  # same implementer
            )
            qa = await self.qa(task, ws)
        task.findings = findings
        lines = [f"Branch <code>{ws.branch}</code> (not merged)", f"Worktree: {ws.path.name}"]
        lines.append(
            f"QA: {sum(r.passed for r in qa)}/{len(qa)} passed"
            if qa
            else "QA: not run (no commands configured)"
        )
        lines.append(f"Review: {len(findings)} findings, {len(blocking)} blocking")
        task.result = "\n".join(lines) + "\n\n" + (impl.text or "")
        return {
            "branch": ws.branch,
            "qa": {r.name: r.status for r in qa},
            "findings": len(findings),
            "blocking": len(blocking),
            "qa_failed": [r.name for r in qa_failed],
            "ok": not blocking and not qa_failed,
        }
