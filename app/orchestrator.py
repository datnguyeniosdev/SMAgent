"""Task coordinator and workflow engine (asyncio, in-memory; state is lost on restart)."""

import asyncio
import json
import logging
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from app import formatting as fmt
from app.actions import Action, ActionError, ActionRegistry
from app.assets import AssetRules, ManifestEntry, check_manifest, manifest_dict, manifest_entry
from app.config import Settings
from app.connectors.base import (
    AgentConnector,
    AgentRequest,
    AgentResult,
    Cap,
    ConnectorError,
    ImageGenerationProvider,
    UnsupportedCapability,
)
from app.logs import correlation_id
from app.models import (
    TERMINAL,
    AgentProfile,
    AgentSession,
    ReviewFinding,
    Task,
    TaskState,
    UsageRecord,
    Workflow,
    new_id,
    now,
)
from app.qa import QAResult, QARunner
from app.router import Router
from app.storage import ArtifactStore, AttachmentError, AttachmentStore
from app.workspace import Workspace, WorkspaceError, WorkspaceManager

log = logging.getLogger(__name__)

WRITES_WORKSPACE = {
    Workflow.code,
    Workflow.delegate_review,
    Workflow.parallel,
    Workflow.design_implement_review,
}
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
DESIGN_SCHEMA = {
    "type": "object",
    "properties": {
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
        "design_spec": {"type": "string"},
        "assets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "prompt": {"type": "string"},
                    "width": {"type": "integer"},
                    "height": {"type": "integer"},
                    "transparent": {"type": "boolean"},
                },
                "required": ["name", "prompt", "width", "height", "transparent"],
            },
        },
    },
    "required": ["acceptance_criteria", "design_spec", "assets"],
}
IMPLEMENTER_SYSTEM = (
    "You are Agent A, the implementer. Work only inside the current directory, which is an "
    "isolated git worktree. Do not commit, push, merge or delete branches. If Unity-MCP tools "
    "are available, list them before use, use only tools that exist, and verify each editor "
    "operation's result; if they are unavailable, say so instead of claiming editor changes. "
    "End with a short summary of what you changed and what you could not verify."
)
REVIEWER_SYSTEM = (
    "You are Agent C, an independent reviewer. Judge the actual diff and evidence against the "
    "requirements; do not trust the implementer's summary. Report concrete defects with file:line, "
    "evidence and remediation. Mark a finding verified only if you confirmed it in the code or "
    "the provided test output. A clean review is not proof the build or runtime works."
)
DESIGNER_SYSTEM = (
    "You are Agent B, the designer. Analyse the references and requirements, write testable "
    "acceptance criteria, an implementation-ready design spec, and image-generation prompts for "
    "any required assets (PNG; state exact pixel size and whether transparency is needed). "
    "Use snake_case asset names with no extension."
)


class Rejected(Exception):
    pass


class Notifier(Protocol):
    async def report(
        self,
        task: Task,
        html: str,
        *,
        actions: list[Action] = (),
        files: list[tuple[str, bytes]] = (),
        photos: list[tuple[str, bytes]] = (),
        control: bool = False,
    ) -> None: ...


class LogNotifier:
    async def report(self, task, html, *, actions=(), files=(), photos=(), control=False):
        log.info("report %s: %s", task.task_id, fmt.plain(html)[:300])


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        router: Router,
        workspaces: WorkspaceManager,
        attachments: AttachmentStore,
        artifacts: ArtifactStore,
        qa: QARunner,
        actions: ActionRegistry,
        image_provider: ImageGenerationProvider | None = None,
        notifier: Notifier | None = None,
    ):
        self.s = settings
        self.router = router
        self.workspaces = workspaces
        self.attachments = attachments
        self.artifacts = artifacts
        self.qa = qa
        self.actions = actions
        self.image_provider = image_provider
        self.notifier: Notifier = notifier or LogNotifier()
        self.tasks: dict[str, Task] = {}
        self.sessions: dict[str, AgentSession] = {}
        self._running: dict[str, asyncio.Task] = {}
        self._approvals: dict[str, asyncio.Future] = {}
        self._active_calls: dict[str, AgentConnector] = {}
        self._sem = asyncio.Semaphore(settings.max_concurrent_tasks)
        self.rules = AssetRules(
            settings.asset_require_alpha, settings.asset_max_dimension, settings.asset_power_of_two
        )

    # ---------- public API ----------

    def profiles(self) -> list[AgentProfile]:
        s = self.s
        return [
            AgentProfile(
                role="designer",
                label="Agent B",
                description="References -> spec, prompts, assets",
                preference=s.designer_preference.split(","),
            ),
            AgentProfile(
                role="implementer",
                label="Agent A",
                description="Code changes in an isolated worktree",
                preference=s.implementer_preference.split(","),
            ),
            AgentProfile(
                role="reviewer",
                label="Agent C",
                description="Independent review of diff, tests and assets",
                preference=s.reviewer_preference.split(","),
            ),
        ]

    def _pref(self, role: str) -> list[str]:
        return next(p.preference for p in self.profiles() if p.role == role)

    async def submit(
        self,
        description: str,
        workflow: Workflow = Workflow.ask,
        created_by: str = "",
        attachment_ids: list[str] = (),
        agent: str | None = None,
        title: str | None = None,
        retry_of: Task | None = None,
    ) -> Task:
        description = description.strip()
        if not description:
            raise ValueError("task description is empty")
        if workflow in WRITES_WORKSPACE and not self.workspaces.enabled:
            raise UnsupportedCapability(f"workflow {workflow} needs WORKSPACE_ROOT configured")
        task = Task(
            title=title or fmt.truncate(description.splitlines()[0], 60),
            description=description,
            workflow=workflow,
            agent=agent,
            created_by=created_by,
        )
        if retry_of:
            task.attempt = retry_of.attempt + 1
            task.correlation_id = retry_of.correlation_id
            task.attachment_ids = list(retry_of.attachment_ids)
            task.parent_id = None
        else:
            for att_id in attachment_ids:
                self.attachments.bind(att_id, task.task_id)
            task.attachment_ids = list(attachment_ids)
        self.tasks[task.task_id] = task
        task.transition(TaskState.queued)
        await self._notify(task, f"🆕 Task accepted\n{fmt.task_line(task)}")
        self._running[task.task_id] = asyncio.create_task(self._run(task))
        return task

    async def stop(self, ref: str) -> bool:
        task = self.tasks.get(ref) or self._task_for_session(ref)
        if task is None:
            raise KeyError(ref)
        if task.done:
            return False
        for child in [t for t in self.tasks.values() if t.parent_id == task.task_id] + [task]:
            conn = self._active_calls.get(child.task_id)
            if conn:
                await conn.cancel(child.task_id)
        fut = self._approvals.get(task.task_id)
        if fut and not fut.done():
            fut.cancel()
        runner = self._running.get(task.task_id)
        if runner:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
        return True

    async def retry(self, task_id: str) -> Task:
        task = self.tasks[task_id]
        if task.state not in {TaskState.failed, TaskState.cancelled, TaskState.unknown}:
            raise ValueError(f"task is {task.state}; only failed/cancelled/unknown can be retried")
        if task.parent_id:
            raise ValueError("retry the parent task instead of a workflow step")
        if task.attempt > self.s.max_retries:
            raise ValueError(f"retry limit reached ({self.s.max_retries})")
        return await self.submit(
            task.description,
            task.workflow,
            task.created_by,
            agent=task.agent,
            title=task.title,
            retry_of=task,
        )

    async def apply_action(self, token: str, actor: int | str) -> str:
        a = self.actions.get(token)
        if a is None:
            raise ActionError("unknown or expired action")
        task = self.tasks.get(a.task_id)
        if task is None:
            raise ActionError("task no longer exists")
        a = self.actions.consume(token, actor, task.state)
        log.info("action %s on %s by %s", a.kind, task.task_id, actor)
        if a.kind in ("approve", "reject"):
            fut = self._approvals.get(task.task_id)
            if fut is None or fut.done():
                raise ActionError("no pending approval")
            fut.set_result(a.kind == "approve")
            return "Approved" if a.kind == "approve" else "Rejected"
        if a.kind == "stop":
            await self.stop(task.task_id)
            return "Stopped"
        if a.kind == "retry":
            new = await self.retry(task.task_id)
            return f"Retrying as {new.task_id}"
        if a.kind == "view_report":
            await self._notify(task, *self._report_message(task))
            return "Report sent"
        if a.kind == "review_diff":
            diffs = [x for x in self._all_artifacts(task) if x.kind == "diff"]
            if not diffs:
                return "No diff recorded"
            files = [(d.filename, d.path.read_bytes()) for d in diffs]
            await self.notifier.report(task, "Diff(s) attached", files=files)
            return "Diff sent"
        raise ActionError(f"action {a.kind!r} is not implemented")

    def report(self, task: Task) -> str:
        parts = [fmt.task_summary(task)]
        children = [t for t in self.tasks.values() if t.parent_id == task.task_id]
        if children:
            parts.append("<b>Steps</b>\n" + "\n".join(fmt.task_line(c) for c in children))
        findings = [f for c in [task, *children] for f in c.findings]
        if findings:
            parts.append(fmt.findings_report(findings))
        arts = self._all_artifacts(task)
        if arts:
            parts.append(
                "<b>Artifacts</b>\n" + fmt.bullet([f"{a.kind}: {a.filename}" for a in arts])
            )
        return "\n\n".join(parts)

    # ---------- execution ----------

    async def _run(self, task: Task) -> None:
        token = correlation_id.set(task.correlation_id)
        try:
            task.transition(TaskState.running)
            await self._notify(task, f"⏳ Started\n{fmt.task_line(task)}")
            await getattr(self, f"_wf_{task.workflow}")(task)
            if not task.done:
                task.transition(TaskState.succeeded)
        except (asyncio.CancelledError, Rejected) as e:
            task.error = "rejected by user" if isinstance(e, Rejected) else "cancelled"
            self._finish(task, TaskState.cancelled)
        except ConnectorError as e:
            task.error, task.error_category = str(e), e.category
            self._finish(task, TaskState.unknown if e.outcome == "unknown" else TaskState.failed)
        except (WorkspaceError, AttachmentError, ValueError) as e:
            task.error, task.error_category = str(e), type(e).__name__
            self._finish(task, TaskState.failed)
        except Exception as e:
            log.exception("task %s crashed", task.task_id)
            task.error, task.error_category = f"internal error: {type(e).__name__}", "internal"
            self._finish(task, TaskState.failed)
        finally:
            self._running.pop(task.task_id, None)
            self._finish_children(task)
            log.info("task finished", extra={"task_id": task.task_id, "state": task.state})
            group = new_id("grp")
            acts = [self.actions.create("view_report", task.task_id, "View report", TERMINAL)]
            if task.state != TaskState.succeeded:
                acts.append(self.actions.create("retry", task.task_id, "Retry", TERMINAL, group))
            if any(a.kind == "diff" for a in self._all_artifacts(task)):
                acts.append(
                    self.actions.create("review_diff", task.task_id, "Review diff", TERMINAL)
                )
            html, full = self._report_message(task)
            await self._notify(task, html, files=full, actions=acts)
            correlation_id.reset(token)

    def _finish(self, task: Task, state: TaskState) -> None:
        if not task.done:
            task.transition(state)

    def _finish_children(self, task: Task) -> None:
        for c in self.tasks.values():
            if c.parent_id == task.task_id and not c.done:
                c.error = c.error or f"parent {task.state}"
                c.transition(TaskState.cancelled)

    def _child(self, parent: Task, role: str, title: str, depends_on: list[Task] = ()) -> Task:
        for dep in depends_on:
            if dep.state != TaskState.succeeded:
                raise ValueError(f"dependency {dep.task_id} is {dep.state}")
        child = Task(
            title=title,
            description=parent.description,
            workflow=parent.workflow,
            role=role,
            parent_id=parent.task_id,
            depends_on=[d.task_id for d in depends_on],
            correlation_id=parent.correlation_id,
            created_by=parent.created_by,
        )
        self.tasks[child.task_id] = child
        child.transition(TaskState.queued)
        child.transition(TaskState.running)
        return child

    async def _call(
        self, step: Task, connector: AgentConnector, req: AgentRequest, role: str, ws=None
    ) -> AgentResult:
        step.provider, step.model = connector.name, getattr(connector, "model", None)
        started = now()
        self._active_calls[step.task_id] = connector
        if self.s.telegram_progress_updates:
            await self._notify(step, f"▶️ {role}: {fmt.task_line(step)} via {connector.name}")
        try:
            async with self._sem:
                result = await connector.execute(req)
        except ConnectorError as e:
            step.usage.append(
                UsageRecord(
                    task_id=step.task_id,
                    provider=connector.name,
                    model=step.model,
                    started_at=started,
                    ended_at=now(),
                    status="error",
                    error_category=e.category,
                )
            )
            step.error, step.error_category = str(e), e.category
            self._finish(step, TaskState.unknown if e.outcome == "unknown" else TaskState.failed)
            raise
        finally:
            self._active_calls.pop(step.task_id, None)
        step.model = result.model or step.model
        step.usage.append(
            UsageRecord(
                task_id=step.task_id,
                provider=result.provider,
                model=result.model,
                started_at=started,
                ended_at=now(),
                status="ok",
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cost_usd=result.cost_usd,
            )
        )
        sess = AgentSession(
            connector=connector.name,
            native_session_id=result.session_id,
            task_id=step.task_id,
            role=role,
            workspace=ws.path if ws else None,
        )
        self.sessions[sess.session_id] = sess
        step.session_ids.append(sess.session_id)
        return result

    def _task_attachments(self, task: Task):
        out = []
        for att_id in task.attachment_ids:
            att = self.attachments.get(att_id)
            out.append((att, self.attachments.read(att)))
        return out

    def _caps_for(self, atts) -> set[Cap]:
        caps = {Cap.text}
        if any(a.is_image for a, _ in atts):
            caps.add(Cap.image_input)
        if any(not a.is_image for a, _ in atts):
            caps.add(Cap.file_input)
        return caps

    def _scratch(self, task_id: str) -> Path:
        p = self.s.data_dir / "scratch" / task_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    async def _approval(self, task: Task, html: str, photos=()) -> None:
        fut = asyncio.get_running_loop().create_future()
        self._approvals[task.task_id] = fut
        task.transition(TaskState.waiting_for_approval)
        group = new_id("grp")
        states = {TaskState.waiting_for_approval}
        acts = [
            self.actions.create("approve", task.task_id, "Approve", states, group),
            self.actions.create("reject", task.task_id, "Reject", states, group),
            self.actions.create("stop", task.task_id, "Stop", states),
        ]
        await self.notifier.report(task, html, actions=acts, photos=photos, control=True)
        await self._notify(task, f"🔐 Waiting for approval: {fmt.task_line(task)}")
        try:
            ok = await asyncio.wait_for(fut, self.s.action_ttl_s)
        except TimeoutError as e:
            raise Rejected("approval expired") from e
        finally:
            self._approvals.pop(task.task_id, None)
        if not ok:
            raise Rejected()
        task.transition(TaskState.running)

    async def _notify(self, task: Task, html: str, files=None, actions=()) -> None:
        try:
            await self.notifier.report(task, html, actions=list(actions), files=files or [])
        except Exception:
            task.delivery_failures += 1
            log.exception("report delivery failed for %s", task.task_id)

    def _report_message(self, task: Task):
        html = self.report(task)
        msg, overflow = fmt.fit(html)
        files = [(f"{task.task_id}_report.txt", overflow.encode())] if overflow else []
        return msg, files

    def _all_artifacts(self, task: Task):
        ids = {task.task_id} | {
            t.task_id for t in self.tasks.values() if t.parent_id == task.task_id
        }
        return [a for i in ids for a in self.artifacts.for_task(i)]

    def _task_for_session(self, session_id: str) -> Task | None:
        sess = self.sessions.get(session_id)
        if not sess:
            return None
        t = self.tasks.get(sess.task_id)
        return self.tasks.get(t.parent_id) if t and t.parent_id else t

    # ---------- steps ----------

    async def _implement(
        self, parent: Task, ws: Workspace, prompt: str, title: str, resume: str | None = None,
        connector: AgentConnector | None = None, depends_on=(),
    ) -> tuple[Task, AgentResult]:  # fmt: skip
        conn = connector or self.router.route(
            {Cap.workspace_write}, self._pref("implementer"), parent.agent
        )
        step = self._child(parent, "implementer", title, depends_on)
        self.workspaces.acquire_write(ws, step.task_id)
        try:
            req = AgentRequest(
                task_id=step.task_id,
                prompt=prompt,
                system=IMPLEMENTER_SYSTEM,
                attachments=self._task_attachments(parent),
                workspace=ws.path,
                read_only=False,
                resume_session=resume if Cap.session_resume in conn.caps else None,
            )
            result = await self._call(step, conn, req, "implementer", ws)
        finally:
            self.workspaces.release_write(ws, step.task_id)
        step.result = result.text
        diff, files = await self.workspaces.diff(ws)
        self.artifacts.add(step.task_id, "diff", f"{step.task_id}.diff", diff, files=files)
        step.transition(TaskState.succeeded)
        return step, result

    async def _qa(self, parent: Task, ws: Workspace) -> list[QAResult]:
        if not self.qa.commands:
            await self._notify(parent, "⚠️ No QA commands configured: tests/builds were NOT run.")
            return []
        step = self._child(parent, "qa", "QA checks")
        results = await self.qa.run_all(ws.path)
        log_text = "\n\n".join(
            f"$ {r.command}\nexit={r.exit_code} timed_out={r.timed_out} "
            f"{r.duration_s}s\n{r.output_tail}"
            for r in results
        )
        self.artifacts.add(step.task_id, "log", "qa.log", log_text)
        lines = [
            f"{'✅' if r.passed else '❌'} {r.name} ({r.duration_s}s"
            + (", timed out" if r.timed_out else "")
            + ")"
            for r in results
        ]
        failed = [r for r in results if not r.passed]
        html = "<b>QA results</b>\n" + "\n".join(lines)
        if failed:
            html += "\n" + fmt.log_block(failed[0].output_tail, 1500)
        await self._notify(parent, html)
        step.result = "\n".join(lines)
        self._finish(step, TaskState.failed if failed else TaskState.succeeded)
        return results

    async def _review(
        self,
        parent: Task,
        ws: Workspace,
        diff: str,
        qa: list[QAResult],
        exclude: str | None,
        extra: str = "",
    ) -> list[ReviewFinding]:
        need = {Cap.text, Cap.structured_output}
        pref = self._pref("reviewer")
        try:  # prefer a different provider than the implementer for independence
            conn = self.router.route(need, pref, exclude={exclude} if exclude else set())
        except UnsupportedCapability:
            conn = self.router.route(need, pref)
        step = self._child(parent, "reviewer", "Independent review")
        qa_text = (
            "\n\n".join(
                f"[{r.name}] exit={r.exit_code} timed_out={r.timed_out}\n{r.output_tail[-4000:]}"
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
        result = await self._call(step, conn, req, "reviewer", ws if read_ws else None)
        try:
            data = result.structured or json.loads(result.text)
            findings = [ReviewFinding(**f) for f in data["findings"]]
        except (ValueError, KeyError, TypeError, ValidationError) as e:
            step.error = f"reviewer returned invalid findings: {e}"
            self._finish(step, TaskState.failed)
            raise ConnectorError(step.error, "provider") from e
        step.findings = findings
        step.result = data.get("summary", "")
        self.artifacts.add(
            step.task_id, "review", "review.json", json.dumps(data, indent=2), reviewer=conn.name
        )
        await self._notify(parent, fmt.findings_report(findings))
        step.transition(TaskState.succeeded)
        return findings

    async def _review_loop(
        self, task: Task, ws: Workspace, impl: AgentResult, step: Task, extra: str = ""
    ):
        qa = await self._qa(task, ws)
        for round_no in range(self.s.max_fix_rounds + 1):
            diff, _ = await self.workspaces.diff(ws)
            findings = await self._review(task, ws, diff, qa, step.provider, extra)
            blocking = [f for f in findings if f.severity in BLOCKING]
            qa_failed = [r for r in qa if not r.passed]
            if (not blocking and not qa_failed) or round_no == self.s.max_fix_rounds:
                break
            fix = "Fix these issues found by an independent reviewer and QA:\n" + "\n".join(
                f"- [{f.severity}] {f.location}: {f.description} -> {f.remediation}"
                for f in blocking
            )
            fix += "".join(f"\n- QA `{r.name}` failed:\n{r.output_tail[-3000:]}" for r in qa_failed)
            step, impl = await self._implement(
                task,
                ws,
                fix,
                f"Fix round {round_no + 1}",
                resume=impl.session_id,
                connector=self.router.connectors[step.provider],  # same implementer
            )
            qa = await self._qa(task, ws)
        task.findings = findings
        lines = [f"Branch <code>{ws.branch}</code> (not merged)", f"Worktree: {ws.path.name}"]
        if qa:
            lines.append(f"QA: {sum(r.passed for r in qa)}/{len(qa)} passed")
        else:
            lines.append("QA: not run (no commands configured)")
        lines.append(f"Review: {len(findings)} findings, {len(blocking)} blocking")
        task.result = "\n".join(lines) + "\n\n" + (impl.text or "")
        if blocking or qa_failed:
            task.error = f"{len(blocking)} blocking findings, {len(qa_failed)} failed QA checks"
            task.error_category = "review_blocked"
            task.transition(TaskState.failed)

    # ---------- workflows ----------

    async def _wf_ask(self, task: Task) -> None:
        atts = self._task_attachments(task)
        conn = self.router.route(self._caps_for(atts), self._pref("designer"), task.agent)
        req = AgentRequest(
            task_id=task.task_id,
            prompt=task.description,
            attachments=atts,
            workspace=self._scratch(task.task_id) if Cap.workspace_read in conn.caps else None,
        )
        result = await self._call(task, conn, req, "assistant")
        task.result = result.text
        self.artifacts.add(task.task_id, "report", "answer.md", result.text)

    async def _wf_code(self, task: Task) -> None:
        conn = self.router.route({Cap.workspace_write}, self._pref("implementer"), task.agent)
        await self._approval(
            task,
            f"🔐 <b>{conn.name}</b> will edit files in a new isolated git worktree for "
            f"{fmt.task_line(task)}\nNothing is merged automatically. Approve?",
        )
        ws = await self.workspaces.create(task.task_id)
        _, impl = await self._implement(task, ws, task.description, "Implement", connector=conn)
        qa = await self._qa(task, ws)
        task.result = f"Branch <code>{ws.branch}</code> (not merged)\n\n{impl.text}"
        if any(not r.passed for r in qa):
            task.error, task.error_category = "QA checks failed", "qa_failed"
            task.transition(TaskState.failed)

    async def _wf_delegate_review(self, task: Task) -> None:
        conn = self.router.route({Cap.workspace_write}, self._pref("implementer"), task.agent)
        await self._approval(
            task,
            f"🔐 <b>{conn.name}</b> will implement, then an independent reviewer checks the diff."
            f"\n{fmt.task_line(task)}\nEdits stay in an isolated worktree. Approve?",
        )
        ws = await self.workspaces.create(task.task_id)
        step, impl = await self._implement(task, ws, task.description, "Implement", connector=conn)
        await self._review_loop(task, ws, impl, step)

    async def _wf_parallel(self, task: Task) -> None:
        writers = [
            c
            for c in self.router.connectors.values()
            if c.available and Cap.workspace_write in c.caps
        ]
        if not writers:
            raise UnsupportedCapability("no available agent can write to a workspace")
        pair = (writers * 2)[:2]  # same runtime twice if only one is available
        await self._approval(
            task,
            f"🔐 Run {pair[0].name} and {pair[1].name} in two separate worktrees for "
            f"{fmt.task_line(task)}? Nothing is merged automatically.",
        )

        async def one(i: int, conn: AgentConnector):
            ws = await self.workspaces.create(f"{task.task_id}_{i}")
            step, impl = await self._implement(
                task, ws, task.description, f"Candidate {i} ({conn.name})", connector=conn
            )
            diff, files = await self.workspaces.diff(ws)
            qa = await self._qa(task, ws)
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
        self.artifacts.add(task.task_id, "report", "comparison.json", json.dumps(results, indent=2))
        task.result = "Comparison (objective metrics only; review the diffs before choosing):"
        await self._notify(task, "<b>Parallel comparison</b>\n" + fmt.json_block(results))

    async def _wf_design_implement_review(self, task: Task) -> None:
        atts = self._task_attachments(task)
        designer = self.router.route(
            self._caps_for(atts) | {Cap.structured_output}, self._pref("designer")
        )
        implementer = self.router.route(
            {Cap.workspace_write}, self._pref("implementer"), task.agent
        )
        await self._approval(
            task,
            f"🔐 Design ({designer.name}) → implement ({implementer.name}, isolated worktree) → "
            f"QA → independent review for {fmt.task_line(task)}. Approve?",
        )
        # 1. Design
        d_step = self._child(task, "designer", "Design spec")
        req = AgentRequest(
            task_id=d_step.task_id,
            prompt=task.description,
            system=DESIGNER_SYSTEM,
            attachments=atts,
            workspace=self._scratch(d_step.task_id)
            if Cap.workspace_read in designer.caps
            else None,
            json_schema=DESIGN_SCHEMA,
        )
        res = await self._call(d_step, designer, req, "designer")
        try:
            design = res.structured or json.loads(res.text)
            specs = design["assets"]
        except (ValueError, KeyError, TypeError) as e:
            raise ConnectorError("designer returned invalid design JSON", "provider") from e
        self.artifacts.add(d_step.task_id, "design_spec", "design_spec.md", design["design_spec"])
        prompt_art = self.artifacts.add(
            d_step.task_id, "prompt", "asset_prompts.json", json.dumps(specs, indent=2)
        )
        d_step.result = "\n".join(design["acceptance_criteria"])
        d_step.transition(TaskState.succeeded)

        # 2. Assets
        a_step = self._child(task, "asset_processor", "Assets", [d_step])
        entries, images = await self._generate_assets(task, a_step, specs, atts, prompt_art)
        problems = check_manifest(entries, [s["name"] for s in specs])
        report = fmt.manifest_report(entries)
        if problems:
            report += "\n" + fmt.bullet(problems)
        if images:
            await self._approval(task, report + "\n\nApprove these assets for integration?", images)
            for e in entries:
                e.approval_status = "approved"
        else:
            await self._notify(task, report)
        self.artifacts.add(
            a_step.task_id,
            "manifest",
            "asset_manifest.json",
            json.dumps({"assets": manifest_dict(entries), "problems": problems}, indent=2),
        )
        a_step.result = f"{len(entries)} assets, {len(problems)} problems"
        a_step.transition(TaskState.succeeded)

        # 3. Implement with approved assets copied into the worktree
        ws = await self.workspaces.create(task.task_id)
        target = (ws.path / self.s.asset_target_dir).resolve()
        if not target.is_relative_to(ws.path.resolve()):
            raise WorkspaceError("ASSET_TARGET_DIR escapes the worktree")
        target.mkdir(parents=True, exist_ok=True)
        for e in entries:
            with open(target / f"{e.name}.png", "xb") as f:  # never overwrite existing assets
                f.write(Path(e.storage_path).read_bytes())
        criteria = fmt.bullet(design["acceptance_criteria"])
        prompt = (
            f"{task.description}\n\nAcceptance criteria:\n{criteria}\n\n"
            f"Design spec:\n{design['design_spec']}\n\n"
            f"Approved assets were placed in {self.s.asset_target_dir}/: "
            f"{', '.join(e.name + '.png' for e in entries) or 'none (generation unavailable)'}"
        )
        step, impl = await self._implement(
            task, ws, prompt, "Implement", connector=implementer, depends_on=[d_step, a_step]
        )
        extra = f"Acceptance criteria:\n{criteria}\n\nAsset manifest problems: {problems or 'none'}"
        await self._review_loop(task, ws, impl, step, extra)

    async def _generate_assets(self, task, step, specs, atts, prompt_art):
        entries: list[ManifestEntry] = []
        images: list[tuple[str, bytes]] = []
        ref_ids = [a.attachment_id for a, _ in atts if a.is_image]
        if specs and self.image_provider is None:
            await self._notify(
                task, "⚠️ No image-generation provider configured; assets were not generated."
            )
            return entries, images
        for spec in specs:
            name = "".join(ch for ch in spec["name"] if ch.isalnum() or ch in "_-")[:60] or "asset"
            size = f"{spec['width']}x{spec['height']}"
            data = await self.image_provider.generate(spec["prompt"], size, spec["transparent"])
            art = self.artifacts.add(
                step.task_id, "image", f"{name}.png", data, prompt=spec["prompt"]
            )
            rules = AssetRules(
                require_alpha=spec["transparent"] and self.rules.require_alpha,
                max_dimension=self.rules.max_dimension,
                power_of_two=self.rules.power_of_two,
                expected_width=spec["width"],
                expected_height=spec["height"],
            )
            entries.append(
                manifest_entry(
                    art.artifact_id,
                    task.task_id,
                    name,
                    data,
                    str(art.path),
                    rules,
                    ref_ids,
                    self.image_provider.name,
                    self.image_provider.model,
                    prompt_art.artifact_id,
                )
            )
            images.append((f"{name}.png", data))
        return entries, images
