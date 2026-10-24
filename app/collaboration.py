"""Reports, human feedback, human input, handoffs, task context and the prompt builder.

Shared by Telegram, REST and the gateway MCP server through `CollaborationService`.
Every record is persisted in the existing SQLite store (kinds: report, feedback,
human_input, handoff, telegram_update).
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.assets import inspect_image
from app.models import ART_TASK_TYPES, TaskState, new_id, now

log = logging.getLogger(__name__)

AgentStatus = Literal["completed", "partial", "blocked", "failed", "in_progress"]


class TelegramRef(BaseModel):
    channel_chat_id: int | None = None
    channel_message_id: int | None = None
    discussion_chat_id: int | None = None
    discussion_message_id: int | None = None
    photo_message_ids: list[int] = []


class Report(BaseModel):
    report_id: str = Field(default_factory=lambda: new_id("rep"))
    task_id: str
    workflow_run_id: str | None = None
    source: str  # agent/runtime id that produced the report, or "gateway"
    text: str
    agent_status: AgentStatus | None = None  # what the agent claims
    verified_status: Literal["verified", "unverified", "failed", "pending"] = "unverified"
    artifact_ids: list[str] = []
    artifact_validation: dict[str, str] = {}  # artifact id -> passed|failed|unchecked
    pending_actions: list[str] = []
    handoff_id: str | None = None
    telegram: TelegramRef = TelegramRef()
    created_at: datetime = Field(default_factory=now)

    @property
    def has_images(self) -> bool:
        return any(v == "passed" for v in self.artifact_validation.values())


class Feedback(BaseModel):
    feedback_id: str = Field(default_factory=lambda: new_id("fb"))
    task_id: str
    report_id: str | None
    text: str
    user_id: str
    source: str  # telegram | api | mcp
    telegram_update_id: int | None = None
    telegram_message_id: int | None = None
    route_id: str | None = None
    follow_up_task_id: str | None = None
    artifact_ids: list[str] = []  # screenshots attached to the feedback itself
    live_message_id: str | None = None  # pushed into a live Claude Code session
    delivered: bool | None = None  # live session was open when it was pushed (else it waits)
    created_at: datetime = Field(default_factory=now)


class HumanInput(BaseModel):
    request_id: str = Field(default_factory=lambda: new_id("hin"))
    task_id: str | None = None
    question: str
    options: list[str] = []
    project: str | None = None
    session_id: str | None = None  # who asked: the answer is pushed there too
    status: Literal["pending", "answered", "expired"] = "pending"
    response: str | None = None
    responder: str | None = None
    created_at: datetime = Field(default_factory=now)
    expires_at: datetime
    answered_at: datetime | None = None


class Handoff(BaseModel):
    """Explicit contract for passing implementation work to an agent runtime or caller."""

    handoff_id: str = Field(default_factory=lambda: new_id("hof"))
    task_id: str
    workflow_run_id: str | None = None
    target: str  # e.g. "claude_code"
    mode: Literal["new_execution", "return_to_caller"]
    prompt: str
    prompt_sha256: str
    context_refs: list[str] = []  # artifact ids / report ids the prompt relies on
    session_ref: str | None = None  # runtime session id, only when one was actually returned
    resumed_session: bool = False  # never true unless the runtime confirmed a resume
    status: Literal["pending_manual_continuation", "running", "succeeded", "failed", "unknown"]
    result_summary: str | None = None
    artifact_ids: list[str] = []
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


DeployStatus = Literal["succeeded", "failed", "partial", "dry_run", "started", "cancelled"]
_DEPLOY_ICON = {
    "succeeded": "✅",
    "failed": "❌",
    "partial": "⚠️",
    "dry_run": "🧪",
    "started": "🚀",
    "cancelled": "⏹",
}


class Deployment(BaseModel):
    deployment_id: str = Field(default_factory=lambda: new_id("dep"))
    project: str = Field(min_length=1, max_length=120)
    environment: str = Field(min_length=1, max_length=40)
    status: DeployStatus
    version: str | None = Field(None, max_length=60)
    build_number: str | None = Field(None, max_length=40)
    commit: str | None = Field(None, max_length=60)
    platforms: list[str] = Field(default=[], max_length=10)
    targets: list[str] = Field(
        default=[], max_length=10
    )  # where it shipped (e.g. "Firebase App Distribution")
    url: str | None = Field(None, max_length=500)
    notes: str | None = Field(None, max_length=3000)  # the release note
    error: str | None = Field(None, max_length=1500)
    source: str = "api"
    task_id: str | None = None
    session_id: str | None = Field(None, max_length=120)
    delivered: bool = False
    delivery_error: str | None = None
    channel_message_id: int | None = None
    created_at: datetime = Field(default_factory=now)


def task_data_project(task) -> str:
    return str(((task.request or {}).get("data") or {}).get("project") or "gateway")


def deployment_html(d: Deployment) -> str:
    from app import formatting as fmt

    e = fmt.escape
    lines = []
    if d.version or d.build_number:
        lines.append(
            f"Version: {e(d.version or '?')}"
            + (f" (build {e(d.build_number)})" if d.build_number else "")
        )
    if d.commit:
        lines.append(f"Commit: <code>{e(d.commit)}</code>")
    if d.platforms:
        lines.append(f"Platforms: {e(', '.join(d.platforms))}")
    if d.targets:
        lines.append(f"Shipped to: {e(', '.join(d.targets))}")
    if d.url and d.url.startswith(("https://", "http://")):
        lines.append(f"Link: {e(d.url)}")
    if d.notes:
        lines.append(f"\n<b>Release note</b>\n{e(d.notes)}")
    if d.error:
        lines.append(f"Error: {e(fmt.truncate(d.error, 800))}")
    lines.append(
        f"<i>{e(d.created_at.astimezone().strftime('%Y-%m-%d %H:%M %Z'))} · via {e(d.source)}</i>"
    )
    return fmt.envelope(
        "deploy",
        "\n".join(lines),
        project=d.project,
        task_id=d.task_id,
        title=f"{_DEPLOY_ICON.get(d.status, '')} {d.environment} · {d.status.replace('_', ' ')}",
        session=d.session_id,
        ref=d.deployment_id,
    )


# ---------------------------------------------------------------- prompt builder

DESIGN_SPEC_SCHEMA = {
    "type": "object",
    "required": ["summary", "issues", "acceptance_criteria"],
    "properties": {
        "summary": {"type": "string", "minLength": 1},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["element", "problem", "fix"],
                "properties": {
                    "element": {"type": "string"},
                    "problem": {"type": "string"},
                    "fix": {"type": "string"},
                },
            },
        },
        "layout": {"type": "object"},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "assumptions": {"type": "array", "items": {"type": "string"}},
    },
}


def build_implementation_prompt(
    *,
    objective: str,
    feedback: str,
    design_spec: dict,
    constraints: list[str],
    artifacts: list[dict],
    validation_requirements: list[str],
    task_id: str,
) -> dict:
    """Deterministic assembly: same inputs -> byte-identical prompt.

    Only references artifacts that exist (passed in), never invents file paths, and states
    explicitly that the design has NOT been implemented yet.
    """
    issues = design_spec.get("issues") or []
    lines = [
        f"# Implementation request (task {task_id})",
        "",
        "## Original objective",
        objective.strip(),
        "",
        "## Latest user feedback (verbatim)",
        feedback.strip(),
        "",
        "## Constraints (must all be respected)",
        *(
            [f"- {c}" for c in constraints]
            or ["- None stated beyond the existing project conventions."]
        ),
        "- Work in the existing project; do not recreate it. Do not commit, push or merge.",
        "",
        "## Design specification (from design analysis; NOT yet implemented)",
        design_spec.get("summary", "").strip(),
        "",
        *[
            f"{i}. **{x.get('element', '')}**: {x.get('problem', '')} -> {x.get('fix', '')}"
            for i, x in enumerate(issues, 1)
        ],
    ]
    if design_spec.get("layout"):
        lines += [
            "",
            "Layout details:",
            "```json",
            json.dumps(design_spec["layout"], indent=2, sort_keys=True),
            "```",
        ]
    if design_spec.get("assumptions"):
        lines += [
            "",
            "Design assumptions to confirm:",
            *[f"- {a}" for a in design_spec["assumptions"]],
        ]
    lines += [
        "",
        "## Reference artifacts (available via the gateway; no other files are implied)",
        *(
            [
                f"- {a['artifact_id']}: {a.get('filename', '')} ({a.get('kind', '')})"
                for a in sorted(artifacts, key=lambda a: a["artifact_id"])
            ]
            or ["- none"]
        ),
        "",
        "## Acceptance criteria",
        *[f"- [ ] {c}" for c in design_spec.get("acceptance_criteria", [])],
        "",
        "## Validation required before reporting completion",
        *[f"- {v}" for v in validation_requirements],
        "- Report which files you changed, which checks you ran with their results, and attach new screenshots.",
        "- Say explicitly which acceptance criteria you could not verify.",
    ]
    prompt = "\n".join(lines).rstrip() + "\n"
    return {
        "prompt": prompt,
        "sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "issue_count": len(issues),
        "artifact_refs": [a["artifact_id"] for a in artifacts],
    }


# ---------------------------------------------------------------- service


class CollaborationError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = (
            code  # unknown_task | unknown_report | ambiguous | forbidden | duplicate | invalid
        )


class CollaborationService:
    def __init__(self, svc):
        self.svc = svc  # Orchestrator

    @property
    def store(self):
        return self.svc.store

    # ---- reports ----

    def get_report(self, report_id: str) -> Report | None:
        body = self.store.get("report", report_id)
        return Report.model_validate(body) if body else None

    def reports_for(self, task_id: str) -> list[Report]:
        return [Report.model_validate(b) for b in self.store.list("report", task_id=task_id)]

    def feedback_for(self, task_id: str) -> list[Feedback]:
        return [Feedback.model_validate(b) for b in self.store.list("feedback", task_id=task_id)]

    def _save_report(self, r: Report) -> None:
        self.store.put(
            "report",
            r.report_id,
            r,
            task_id=r.task_id,
            run_id=r.workflow_run_id,
            state=r.verified_status,
        )
        if r.telegram.channel_message_id is not None:
            self.store.put(
                "tg_msg",
                f"c:{r.telegram.channel_chat_id}:{r.telegram.channel_message_id}",
                {"report_id": r.report_id},
                task_id=r.task_id,
            )
        for mid in [r.telegram.discussion_message_id, *r.telegram.photo_message_ids]:
            if mid is not None:
                chat = (
                    r.telegram.discussion_chat_id
                    if mid == r.telegram.discussion_message_id
                    else r.telegram.channel_chat_id
                )
                self.store.put(
                    "tg_msg", f"c:{chat}:{mid}", {"report_id": r.report_id}, task_id=r.task_id
                )

    def verified_status(self, task_id: str) -> str:
        task = self.svc.tasks.get(task_id)
        if task is None:
            return "unverified"
        if task.state == TaskState.succeeded:
            # Verified needs positive evidence: at least one passed gate, none failed, no pending handoff.
            vals = self.svc.envelope(task).validations
            pending = any(
                h.status in ("pending_manual_continuation", "running")
                for h in self.handoffs_for(task_id)
            )
            ok = bool(vals) and all(v.status == "passed" for v in vals) and not pending
            return "verified" if ok else "unverified"
        if task.state in (TaskState.failed, TaskState.unknown):
            return "failed"
        return "pending"

    async def submit_report(
        self,
        task_id: str,
        text: str,
        source: str,
        agent_status: AgentStatus | None = None,
        images: list[tuple[str, bytes]] = (),
        artifact_ids: list[str] = (),
        handoff_id: str | None = None,
        workflow_run_id: str | None = None,
    ) -> Report:
        task = self.svc.tasks.get(task_id)
        if task is None:
            raise CollaborationError("unknown_task", f"unknown task {task_id}")
        validation: dict[str, str] = {}
        ids = []
        for aid in artifact_ids:
            if not self.svc.artifact_ok(task, aid):
                raise CollaborationError(
                    "invalid", f"artifact {aid} is not available to task {task_id}"
                )
            ids.append(aid)
            validation[aid] = self.svc.artifacts.get(aid).validation_status
        for name, data in images:
            info = inspect_image(data)
            if info.format == "unknown":
                raise CollaborationError(
                    "invalid", f"{name}: not a readable PNG/JPEG/WebP/GIF image"
                )
            art = self.svc.artifacts.add(
                task_id,
                "screenshot",
                name,
                data,
                producer=source,
                width=info.width,
                height=info.height,
            )
            self.svc.artifacts.set_validation(art.artifact_id, "passed")
            ids.append(art.artifact_id)
            validation[art.artifact_id] = "passed"
        report = Report(
            task_id=task_id,
            workflow_run_id=workflow_run_id or task.run_id,
            source=source,
            text=text,
            agent_status=agent_status,
            artifact_ids=ids,
            artifact_validation=validation,
            handoff_id=handoff_id,
        )
        report.verified_status = self.verified_status(task_id)
        if agent_status == "completed" and report.verified_status != "verified":
            report.pending_actions.append("agent báo đã xong; gateway chưa tự kiểm chứng")
        if handoff_id:
            self._complete_handoff(handoff_id, report)
        self._save_report(report)
        self.store.event(
            "report.submitted",
            task_id=task_id,
            run_id=report.workflow_run_id,
            report_id=report.report_id,
            source=source,
            agent_status=agent_status,
            verified=report.verified_status,
            artifacts=ids,
        )
        await self.publish(report)
        return report

    def _complete_handoff(self, handoff_id: str, report: Report) -> None:
        body = self.store.get("handoff", handoff_id)
        if body is None:
            raise CollaborationError("invalid", f"unknown handoff {handoff_id}")
        h = Handoff.model_validate(body)
        if h.task_id != report.task_id:
            raise CollaborationError("forbidden", "handoff belongs to another task")
        h.status = (
            "succeeded"
            if report.agent_status == "completed"
            else ("failed" if report.agent_status == "failed" else h.status)
        )
        h.result_summary = report.text[:1000]
        h.artifact_ids = report.artifact_ids
        h.updated_at = now()
        self.save_handoff(h)

    async def publish(self, report: Report) -> None:
        from app import formatting as fmt

        task = self.svc.tasks[report.task_id]
        # sent in full (agents keep it short); the bot splits anything over Telegram's limit
        title, _, rest = report.text.strip().partition("\n")
        vi = fmt.STATUS_VI
        meta = [f"từ {fmt.escape(report.source)}",
                f"gateway: {vi.get(report.verified_status, report.verified_status)}"]  # fmt: skip
        if report.agent_status:
            meta.insert(1, f"agent: {fmt.escape(vi.get(report.agent_status, report.agent_status))}")
        if report.artifact_ids:
            meta.append(f"📎 {len(report.artifact_ids)}")
        body = fmt.escape(rest.strip())
        if report.pending_actions:
            body += "\n\n⚠️ " + fmt.escape("; ".join(report.pending_actions))
        html = self.svc.msg(
            "report", task, (body + "\n\n" if body else "") + " · ".join(meta),
            title=title, ref=report.report_id, reply="Dạ, trả lời tin này để gửi góp ý cho agent ạ",
        )  # fmt: skip
        limit = self.svc.gateway_cfg.telegram.max_report_photos if self.svc.gateway_cfg else 4
        photos = []
        for aid in report.artifact_ids:
            art = self.svc.artifacts.get(aid)
            if art and art.mime_type and art.mime_type.startswith("image/") and len(photos) < limit:
                photos.append((art.filename, self.svc.artifacts.read(art)))
        publish = getattr(self.svc.notifier, "publish_report", None)
        try:
            if publish is None:
                await self.svc.notifier.report(task, html, photos=photos)
                return
            ref: TelegramRef = await publish(task, html, photos)
        except Exception:
            task.delivery_failures += 1
            log.exception("report delivery failed for %s", report.report_id)
            self.store.event(
                "report.delivery_failed", task_id=task.task_id, report_id=report.report_id
            )
            return
        report.telegram = ref
        self._save_report(report)

    async def publish_final(
        self, task, html: str, photos: list[tuple[str, bytes, str]], files=(), actions=()
    ) -> Report:
        """The end-of-workflow message for every task, recorded as a report so replies become feedback."""
        report = Report(
            task_id=task.task_id,
            workflow_run_id=task.run_id,
            source="gateway",
            text=(task.result or task.error or task.state.value)[:4000],
            artifact_ids=[aid for _, _, aid in photos],
            artifact_validation={
                aid: self.svc.artifacts.get(aid).validation_status for _, _, aid in photos
            },
            verified_status=self.verified_status(task.task_id),
        )
        publish = getattr(self.svc.notifier, "publish_report", None)
        pics = [(name, data) for name, data, _ in photos]
        try:
            if publish is not None:
                report.telegram = await publish(
                    task, html, pics, files=list(files), actions=list(actions)
                )
            else:
                await self.svc.notifier.report(
                    task, html, photos=pics, files=list(files), actions=list(actions)
                )
        except Exception:
            task.delivery_failures += 1
            log.exception("final report delivery failed for %s", task.task_id)
        self._save_report(report)
        return report

    def link_discussion_copy(
        self,
        channel_chat_id: int,
        channel_message_id: int,
        discussion_chat_id: int,
        discussion_message_id: int,
    ) -> str | None:
        """Record the automatic forward of a channel post into the linked discussion group."""
        hit = self.store.get("tg_msg", f"c:{channel_chat_id}:{channel_message_id}")
        if not hit:
            return None
        report = self.get_report(hit["report_id"])
        report.telegram.discussion_chat_id = discussion_chat_id
        report.telegram.discussion_message_id = discussion_message_id
        self._save_report(report)
        return report.report_id

    def resolve_report(
        self,
        *,
        chat_id: int,
        message_id: int,
        forward_chat_id: int | None = None,
        forward_message_id: int | None = None,
    ) -> Report | None:
        """Map the replied-to Telegram message to a report. Returns None when unknown (never guesses)."""
        for key in [f"c:{chat_id}:{message_id}"] + (
            [f"c:{forward_chat_id}:{forward_message_id}"] if forward_message_id else []
        ):
            hit = self.store.get("tg_msg", key)
            if hit:
                return self.get_report(hit["report_id"])
        return None

    # ---- feedback ----

    def claim_update(self, update_key: str) -> bool:
        """Idempotency for Telegram updates. False when already processed."""
        if self.store.get("telegram_update", update_key):
            return False
        self.store.put("telegram_update", update_key, {"at": now().isoformat()})
        return True

    async def submit_feedback(
        self,
        *,
        text: str,
        user_id: str,
        source: str,
        report_id: str | None = None,
        task_id: str | None = None,
        update_id: int | None = None,
        message_id: int | None = None,
        images: list[tuple[str, bytes]] = (),
    ) -> Feedback:
        text = text.strip()
        if not text and not images:
            raise CollaborationError("invalid", "empty feedback")
        if len(images) > 10:
            raise CollaborationError("invalid", "at most 10 images per feedback")
        infos = []
        for name, data in images:  # validate everything before storing anything
            info = inspect_image(data)
            if info.format == "unknown":
                raise CollaborationError(
                    "invalid", f"{name}: not a readable PNG/JPEG/WebP/GIF image"
                )
            infos.append(info)
        report = self.get_report(report_id) if report_id else None
        if report_id and report is None:
            raise CollaborationError("unknown_report", f"unknown report {report_id}")
        if report is None and task_id is None:
            raise CollaborationError("ambiguous", "cannot tell which task this feedback is for")
        task_id = report.task_id if report else task_id
        if task_id not in self.svc.tasks:
            raise CollaborationError("unknown_task", f"unknown task {task_id}")
        if report is None:
            reports = self.reports_for(task_id)
            report = max(reports, key=lambda r: r.created_at) if reports else None
        shot_ids = []
        for (name, data), info in zip(images, infos, strict=True):
            art = self.svc.artifacts.add(
                task_id, "screenshot", name, data, producer=f"feedback:{source}",
                width=info.width, height=info.height,
            )  # fmt: skip
            self.svc.artifacts.set_validation(art.artifact_id, "passed")
            shot_ids.append(art.artifact_id)
        fb = Feedback(
            task_id=task_id,
            report_id=report.report_id if report else None,
            text=text or f"(see {len(shot_ids)} attached screenshot(s))",
            artifact_ids=shot_ids,
            user_id=user_id,
            source=source,
            telegram_update_id=update_id,
            telegram_message_id=message_id,
        )
        req = self.svc.tasks[task_id].request or {}
        data = req.get("data") or {}
        live = data.get("channel_session") or data.get("session_id")
        # an agent's own work: only an agent can act on it, so "recorded" helps nobody
        external = req.get("source") == "external"
        visual = self._feedback_route(fb, report) if external and not live else None
        if visual is not None and visual.id == "acknowledge":
            visual = None
        hub = getattr(self.svc, "channel", None)
        if (
            (live or external) and visual is None and hub is not None
        ):  # that session, else the project's
            route, fb.route_id = None, "channel"
            text_for_session = f"Telegram reply from the operator:\n{fb.text}"
            for aid in shot_ids:
                text_for_session += f"\nScreenshot: {self.svc.artifacts.get(aid).path}"
            _msg, target = hub.send(
                task_data_project(self.svc.tasks[task_id]), text_for_session,
                session_id=live or None, sender=user_id,
            )  # fmt: skip
            fb.follow_up_task_id = None
            fb.live_message_id, fb.delivered = _msg["message_id"], target is not None
        elif (src := self.svc.tasks[task_id]).task_type in ART_TASK_TYPES and src.done:
            route, fb.route_id = None, "art_revision"  # Feedback on generated art: regenerate it
            fb.follow_up_task_id = (await self.svc.regenerate_art(src, fb.text, user_id)).task_id
        else:
            route = self._feedback_route(fb, report)
            fb.route_id = route.id if route else None
        self.store.put("feedback", fb.feedback_id, fb, task_id=task_id)
        self.store.event(
            "feedback.received",
            task_id=task_id,
            feedback_id=fb.feedback_id,
            report_id=fb.report_id,
            route=fb.route_id,
            user=user_id,
            images=len(shot_ids),
        )
        if route is not None:
            from app.intake import TaskRequest

            original = self.svc.tasks[task_id]
            req = TaskRequest(
                objective=f"Follow-up on {task_id}: {fb.text}",
                user_id=user_id,
                workflow_id=route.workflow,
                parent_task_id=task_id,
                correlation_id=original.correlation_id,
                constraints=list((original.request or {}).get("constraints", [])),
                artifact_ids=list(
                    dict.fromkeys([*shot_ids, *(report.artifact_ids if report else [])])
                )[:20],
                data={
                    "source_task_id": task_id,
                    "report_id": fb.report_id,
                    "feedback_id": fb.feedback_id,
                    "feedback_text": fb.text,
                },
            )
            follow = await self.svc.submit_request(req)
            fb.follow_up_task_id = follow.task_id
            self.store.put("feedback", fb.feedback_id, fb, task_id=task_id)
        return fb

    def _feedback_route(self, fb: Feedback, report: Report | None):
        cfg = self.svc.gateway_cfg
        if cfg is None:
            return None
        words = set(fb.text.lower().replace(",", " ").replace(".", " ").split())
        for r in sorted(
            (r for r in cfg.routing.feedback_routes if r.enabled), key=lambda r: r.priority
        ):
            if r.requires_report_images and not ((report and report.has_images) or fb.artifact_ids):
                continue
            if r.keywords and not (words & {k.lower() for k in r.keywords}):
                continue
            return r
        return None

    # ---- deployments ----

    async def report_deployment(self, d: Deployment) -> Deployment:
        """Persist a deployment notice and post it to the report channel (delivery reported honestly)."""
        publish = getattr(self.svc.notifier, "publish_channel", None)
        if publish is None:
            d.delivery_error = (
                "Telegram is not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_REPORT_CHANNEL_ID)"
            )
        else:
            try:
                d.channel_message_id = await publish(deployment_html(d))
                d.delivered = True
            except Exception as e:
                d.delivery_error = f"{type(e).__name__}: {str(e)[:200]}"
                log.exception("deployment notice delivery failed")
        self.store.put("deployment", d.deployment_id, d, task_id=d.task_id, state=d.status)
        self.store.event(
            "deployment.reported",
            task_id=d.task_id,
            deployment_id=d.deployment_id,
            project=d.project,
            environment=d.environment,
            status=d.status,
            delivered=d.delivered,
        )
        return d

    def deployments(self, limit: int = 50) -> list[Deployment]:
        return [Deployment.model_validate(b) for b in self.store.list("deployment")][-limit:]

    # ---- human input ----

    async def request_human_input(
        self,
        question: str,
        task_id: str | None = None,
        options: list[str] = (),
        ttl_s: int | None = None,
        project: str | None = None,
        session_id: str | None = None,
    ) -> HumanInput:
        from app import formatting as fmt

        if task_id and task_id not in self.svc.tasks:
            raise CollaborationError("unknown_task", f"unknown task {task_id}")
        hi = HumanInput(
            task_id=task_id,
            question=question.strip(),
            options=list(options)[:6],
            project=project,
            session_id=session_id,
            expires_at=now()
            + timedelta(seconds=self.svc.s.action_ttl_s if ttl_s is None else ttl_s),
        )
        self.store.put("human_input", hi.request_id, hi, task_id=task_id, state=hi.status)
        self.store.event("human_input.requested", task_id=task_id, request_id=hi.request_id)
        anchor = self.svc.tasks.get(task_id) if task_id else None
        body = fmt.escape(hi.question)
        if hi.options:
            body += "\n" + fmt.bullet([fmt.escape(o) for o in hi.options])
        reply = (
            "Tap an option or reply to this message"
            if hi.options
            else "Reply to this message to answer"
        )
        if anchor is not None and not project:
            html = self.svc.msg("question", anchor, body, ref=hi.request_id, reply=reply)
        else:
            html = fmt.envelope(
                "question", body,
                project=project or (self.svc.project_of(anchor) if anchor else self.svc.default_project),
                task_id=task_id, session=session_id, ref=hi.request_id, reply=reply,
            )  # fmt: skip
        n = self.svc.notifier
        try:
            if hasattr(n, "send_question"):
                await n.send_question(hi, html)
            elif anchor is not None:
                await n.report(anchor, html, control=True)
            elif hasattr(n, "send_control"):
                await n.send_control(html)
        except Exception as e:
            log.exception("human input delivery failed")
            hi.status = "expired"  # nobody can answer a question that never arrived
            self.store.put("human_input", hi.request_id, hi, task_id=task_id, state=hi.status)
            raise CollaborationError(
                "delivery_failed",
                f"the question did not reach Telegram ({type(e).__name__}); try again later",
            ) from e
        return hi

    def get_human_input(self, request_id: str) -> HumanInput:
        body = self.store.get("human_input", request_id)
        if body is None:
            raise CollaborationError("invalid", f"unknown request {request_id}")
        hi = HumanInput.model_validate(body)
        if hi.status == "pending" and now() > hi.expires_at:
            hi.status = "expired"
            self.store.put("human_input", hi.request_id, hi, task_id=hi.task_id, state=hi.status)
        return hi

    def answer_human_input(
        self, request_id: str, text: str, responder: str, free: bool = False
    ) -> HumanInput:
        """free: a typed reply on Telegram, accepted even when the question offered options."""
        hi = self.get_human_input(request_id)
        if hi.status != "pending":
            raise CollaborationError("duplicate", f"request {request_id} is {hi.status}")
        if hi.options and text not in hi.options and not free:
            raise CollaborationError("invalid", f"answer must be one of {hi.options}")
        hi.status, hi.response, hi.responder, hi.answered_at = (
            "answered",
            text.strip(),
            responder,
            now(),
        )
        self.store.put("human_input", hi.request_id, hi, task_id=hi.task_id, state=hi.status)
        self.store.event(
            "human_input.answered", task_id=hi.task_id, request_id=request_id, by=responder
        )
        hub = getattr(self.svc, "channel", None)
        rec = self.store.get("channel_session", hi.session_id or "") or {}
        project = hi.project or rec.get("project")
        if hub is not None and project:  # the asker gets it without polling
            hub.send(
                project, f"💬 The operator answered your question {request_id}:\n{hi.response}\n\n"
                "(Continue the work that needed it. It needs no reply.)",
                session_id=hi.session_id, sender=responder, source="operator",
                extra={"kind": "answer", "human_input": request_id},
            )  # fmt: skip
        return hi

    # ---- handoffs ----

    def save_handoff(self, h: Handoff) -> None:
        self.store.put(
            "handoff", h.handoff_id, h, task_id=h.task_id, run_id=h.workflow_run_id, state=h.status
        )

    def handoffs_for(self, task_id: str) -> list[Handoff]:
        return [Handoff.model_validate(b) for b in self.store.list("handoff", task_id=task_id)]

    # ---- context ----

    def load_context(
        self,
        source_task_id: str,
        report_id: str | None,
        feedback_id: str | None,
        requester_task_id: str,
    ) -> dict:
        src = self.svc.tasks.get(source_task_id)
        if src is None:
            raise CollaborationError("unknown_task", f"unknown source task {source_task_id}")
        req_task = self.svc.tasks.get(requester_task_id)
        origin = (req_task.request or {}).get("parent_task_id") if req_task else None
        if (
            req_task is not None
            and origin != source_task_id
            and requester_task_id != source_task_id
        ):
            raise CollaborationError("forbidden", "context belongs to an unrelated task")
        reports = self.reports_for(source_task_id)
        report = (
            next((r for r in reports if r.report_id == report_id), None)
            if report_id
            else (max(reports, key=lambda r: r.created_at) if reports else None)
        )
        fb = (
            Feedback.model_validate(self.store.get("feedback", feedback_id))
            if feedback_id and self.store.get("feedback", feedback_id)
            else None
        )
        arts = self.svc._all_artifacts(src)
        shots = list(
            dict.fromkeys(
                [
                    *(fb.artifact_ids if fb else []),  # the operator's own screenshots first
                    *(
                        a.artifact_id
                        for a in arts
                        if a.kind == "screenshot"
                        and (report is None or a.artifact_id in report.artifact_ids)
                    ),
                ]
            )
        )
        history = [{"type": e["type"], "ts": e["ts"]} for e in self.store.events(source_task_id)][
            -20:
        ]
        return {
            "source_task_id": source_task_id,
            "objective": src.description,
            "constraints": list((src.request or {}).get("constraints", [])),
            "report_id": report.report_id if report else None,
            "report_text": report.text if report else "",
            "feedback": fb.text if fb else "",
            "screenshots": shots,
            "artifacts": [
                {"artifact_id": a.artifact_id, "filename": a.filename, "kind": a.kind}
                for a in arts
                if a.kind in ("screenshot", "diff", "report", "image")
            ],
            "history": history,
        }


def policy_requires_approval(rules, ids: list[str]) -> bool:
    return any(fnmatch.fnmatch(i, r.match) for r in rules for i in ids)


def jsonable(x: Any) -> Any:
    return json.loads(json.dumps(x, default=str))
