"""The gateway as an MCP server for upstream clients (e.g. Claude Code via `--transport http`).

Thin facade: every tool calls the shared task service / collaboration service. No second
orchestration engine lives here. Served over Streamable HTTP at /mcp (bearer API_TOKEN).
"""

from __future__ import annotations

import base64
import binascii
import functools
import html
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from app.agent_registry import CapabilityError
from app.collaboration import CollaborationError
from app.intake import TaskRequest
from app.models import ART_TASK_TYPES
from app.storage import AttachmentError, load_image_paths
from app.workflow import StepState


class GatewayToolError(ToolError):
    """Raised by tools for caller mistakes; ToolError makes the SDK show the message to the agent
    (any other exception is reported only as "Error executing tool <name>")."""


def art_brief(
    subject: str, purpose: str, requirements: str, w: int, h: int, transparent: bool
) -> str:
    """One complete brief for the image model: what, what for, the exact output, the canvas."""
    background = (
        "transparent background (alpha channel)"
        if transparent
        else "background as the requirements say"
    )
    return (
        f"{subject.strip()}\n\n"
        f"Purpose: {purpose.strip()}\n"
        f"Output requirements: {requirements.strip()}\n"
        f"Canvas: {w}x{h} px, {background}. Deliver one finished image; no borders, frames, "
        "watermarks or text unless the requirements ask for them."
    )


def build_mcp_server(svc) -> MCPServer:
    server = MCPServer(
        "agent-gateway",
        instructions=(
            "Local orchestration gateway. Use delegate_task to start work, get_task_status / get_task_result "
            "to follow it, submit_report to report results with screenshots, and request_human_input / "
            "get_human_response to ask the operator via Telegram. Results distinguish agent-reported status "
            "from gateway-verified status."
        ),
    )

    def tool(fn):
        """Register fn as an MCP tool that refuses while the operator has switched it off (/mcp)."""

        @functools.wraps(fn)
        async def gated(*args, **kwargs):
            if not svc.switches.on(fn.__name__):
                raise GatewayToolError(
                    f"{fn.__name__} is switched off by the operator (Telegram /mcp); "
                    "continue without it or ask the operator to turn it on"
                )
            if isinstance(kwargs.get("project"), str):  # some models send "Stack &amp; Merge"
                kwargs["project"] = html.unescape(kwargs["project"]).strip()
            return await fn(*args, **kwargs)

        return server.tool()(gated)

    async def art_3d(prompt, purpose, requirements, project, session_id, image_path, fmt, texture):
        """generate_art(output="model3d"): Meshy, with the same approval and review as images."""
        if len(prompt.strip()) < 3 or len(purpose.strip()) < 3 or len(requirements.strip()) < 10:
            raise GatewayToolError(
                "describe the model: prompt (the object), purpose (where it is used) and requirements "
                "(style, proportions, materials/colors, level of detail, what must or must not appear)"
            )
        if fmt not in ("glb", "fbx", "obj", "usdz", "stl"):
            raise GatewayToolError("model_format must be glb, fbx, obj, usdz or stl")
        if svc.model3d_provider is None:
            raise GatewayToolError(
                "no 3D provider is on: set MESHY_API_KEY (/keys) and switch on 🧊 3D (Meshy) in /mcp"
            )
        brief = f"{prompt.strip()}. For: {purpose.strip()}. {requirements.strip()}"
        if len(brief) > 800:
            raise GatewayToolError(
                f"the 3D brief is {len(brief)} characters; Meshy takes 800: shorten it"
            )
        att_ids, ref = [], ""
        if image_path:
            from app.assets import inspect_image

            [(name, data)] = paths_or_error([image_path])
            kind = inspect_image(data).format
            if kind not in ("png", "jpeg", "webp"):
                raise GatewayToolError(f"{name}: the reference image must be PNG, JPEG or WebP")
            try:
                att = svc.attachments.save(data, name, f"image/{kind}", "mcp")
            except AttachmentError as e:
                raise GatewayToolError(str(e)) from e
            att_ids, ref = [att.attachment_id], att.attachment_id
        data = {"prompt": brief, "image_ref": ref, "texture": bool(texture), "format": fmt}
        data.update({k: v for k, v in (("project", project), ("session_id", session_id)) if v})
        try:
            task = await svc.submit_request(
                TaskRequest(
                    objective=f"3D: {brief[:200]}",
                    task_type="design_model3d",
                    data=data,
                    attachment_ids=att_ids,
                    user_id="mcp",
                    source="mcp",
                )  # fmt: skip
            )
        except ValueError as e:
            raise GatewayToolError(str(e)) from e
        return {
            "task_id": task.task_id,
            "state": task.state.value,
            "note": "the operator approves the cost, then reviews the model's preview on Telegram; with "
            'your session_id the decision arrives through get_messages as an "answer" message (no '
            "polling): accepted -> models[0].path in get_task_result is the file; changes_requested -> "
            "a new version is made and reviewed. 3D takes a few minutes.",
        }

    def task_or_error(task_id: str):
        task = svc.tasks.get(task_id)
        if task is None:
            raise GatewayToolError(f"unknown task {task_id}")
        return task

    @tool
    async def delegate_task(
        objective: str,
        task_type: str | None = None,
        domain: str | None = None,
        workflow_id: str | None = None,
        data: dict | None = None,
        constraints: list[str] | None = None,
        idempotency_key: str | None = None,
        project: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Start a task. Known task types use their configured workflow. Returns the task id and state.
        project / session_id label every Telegram message about the task; with session_id its end
        arrives through get_messages as an "answer" message (no polling; get_task_result has details)."""
        ident = {k: v for k, v in (("project", project), ("session_id", session_id)) if v}
        try:
            req = TaskRequest(
                objective=objective,
                task_type=task_type,
                domain=domain,
                workflow_id=workflow_id,
                data={**(data or {}), **ident},
                constraints=constraints or [],
                idempotency_key=idempotency_key,
                user_id="mcp",
                source="mcp",
            )
            task = await svc.submit_request(req)
        except ValueError as e:
            raise GatewayToolError(str(e)) from e
        return {"task_id": task.task_id, "state": task.state.value}

    @tool
    async def get_task_status(task_id: str) -> dict[str, Any]:
        """Task state, routing decision and per-step states (with attempts and errors)."""
        task = task_or_error(task_id)
        steps = svc.load_steps(task.run_id) if task.run_id else {}
        return {
            "task_id": task.task_id,
            "state": task.state.value,
            "domain": task.domain,
            "workflow": (task.request or {}).get("_routing", {}).get("workflow_id"),
            "workflow_version": (task.request or {}).get("_routing", {}).get("workflow_version"),
            "question": task.question,
            "steps": [
                {"id": r.step_id, "state": r.state.value, "attempts": r.attempt, "error": r.error}
                for r in steps.values()
            ],
            "pending_approvals": [a["approval_id"] for a in svc.pending_approvals(task_id)],
            "waiting": [
                r.step_id for r in steps.values() if r.state == StepState.waiting_for_approval
            ],
        }

    @tool
    async def get_task_result(task_id: str) -> dict[str, Any]:
        """Result envelope plus handoffs (with implementation prompts) and reports."""
        task = task_or_error(task_id)
        env = svc.envelope(task).model_dump(mode="json")
        env["handoffs"] = [h.model_dump(mode="json") for h in svc.collab.handoffs_for(task_id)]
        env["reports"] = [
            r.model_dump(mode="json", exclude={"telegram"}) for r in svc.collab.reports_for(task_id)
        ]
        env["images"] = [  # local files, so a caller can copy the result into its repo
            {"artifact_id": a.artifact_id, "path": str(a.path), "filename": a.filename,
             "width": a.meta.get("width"), "height": a.meta.get("height"),
             "validation": a.validation_status}
            for a in svc._all_artifacts(task)
            if (a.mime_type or "").startswith("image/") and a.validation_status != "failed"
        ]  # fmt: skip
        env["models"] = [  # 3D models (generate_art output=model3d)
            {"artifact_id": a.artifact_id, "path": str(a.path), "filename": a.filename}
            for a in svc._all_artifacts(task) if a.kind == "model3d"
        ]  # fmt: skip
        if (
            task.task_type in ART_TASK_TYPES
        ):  # generated art: only usable once the operator accepts it
            review = svc.art_review(task_id) or {"state": "pending" if task.done else "generating"}
            env["review"] = review
            if review.get("state") != "accepted":
                env["images"], env["models"] = [], []
        env["feedback"] = [
            {"feedback_id": f.feedback_id, "report_id": f.report_id, "text": f.text,
             "user_id": f.user_id, "created_at": f.created_at.isoformat()}
            for f in svc.collab.feedback_for(task_id)
        ]  # fmt: skip
        return env

    @tool
    async def list_capabilities() -> list[dict[str, Any]]:
        """Configured capabilities with live status, declaring agents and executors."""
        if svc.registry is None:
            return []
        out = []
        for cap in svc.registry.capabilities.values():
            m = svc.caps.get(f"capability:{cap.id}")
            out.append(
                {
                    "id": cap.id,
                    "description": cap.description,
                    "status": m.status.value if m else "unknown",
                    "agents": svc.registry.agents_for(cap.id),
                    "executors": [f"{e.type}:{e.ref}" for e in cap.executors],
                }
            )
        return out

    @tool
    async def get_capability_details(capability_id: str) -> dict[str, Any]:
        """Schemas, executor eligibility (with reasons) and status of one capability."""
        if svc.registry is None:
            raise GatewayToolError("no gateway configuration loaded")
        try:
            checks = svc.registry.candidates(capability_id)
        except CapabilityError as e:
            raise GatewayToolError(f"{e.code}: {e}") from e
        cap = svc.registry.capabilities[capability_id]
        m = svc.caps.get(f"capability:{capability_id}")
        return {
            "id": cap.id,
            "description": cap.description,
            "input_schema": cap.input_schema,
            "output_schema": cap.output_schema,
            "side_effects": cap.side_effects,
            "status": m.status.value if m else "unknown",
            "agents": svc.registry.agents_for(cap.id),
            "executors": [
                {"executor": c.executor_id, "eligible": c.ok, "reason": c.reason} for c in checks
            ],
        }

    @tool
    async def cancel_task(task_id: str) -> dict[str, Any]:
        """Cancel a task. 'unknown' means an in-flight side effect may already have happened."""
        task = task_or_error(task_id)
        if (task.created_by or "").isdigit():  # a Telegram user id: the operator started it
            raise GatewayToolError(
                "the operator started this task; only they can cancel it (/stop on Telegram)"
            )
        stopped = await svc.stop(task_id)
        return {"task_id": task_id, "stopped": stopped, "state": svc.tasks[task_id].state.value}

    @tool
    async def request_human_input(
        question: str,
        task_id: str | None = None,
        options: list[str] | None = None,
        ttl_s: int | None = None,
        project: str | None = None,
        session_id: str | None = None,
        secure: bool = False,
    ) -> dict[str, Any]:
        """Ask the operator a question via Telegram. Use it for every confirmation or decision (commit,
        push, merge, deploy, a choice), even when you also ask in the app: options = 2-4 short button
        labels; pass your session_id so the answer arrives through get_messages (kind "answer"), no
        polling; get_human_response(id) shows it too. secure=true for release, deploy, commit/push/merge
        and git pull: on Telegram it is approved only with the operator's passcode (answer "✅ Duyệt (đã
        xác minh passcode)"); "❌ Từ chối" refuses; a 🔒 locked answer means 3 wrong passcodes: it is
        not approved, ask the operator to confirm in your session. An approval in your session works too."""
        try:
            hi = await svc.collab.request_human_input(
                question, task_id, options or [], ttl_s, project, session_id, secure
            )
        except CollaborationError as e:
            raise GatewayToolError(str(e)) from e
        return {
            "request_id": hi.request_id,
            "status": hi.status,
            "expires_at": hi.expires_at.isoformat(),
        }

    @tool
    async def close_question(request_id: str, answer: str) -> dict[str, Any]:
        """The operator answered your request_human_input question in this session (app or terminal)
        instead of on Telegram: close the Telegram copy so it is not reminded or answered twice.
        answer: what they said, in a few words."""
        try:
            hi = await svc.collab.close_human_input(request_id, answer)
        except CollaborationError as e:
            raise GatewayToolError(str(e)) from e
        return {"request_id": hi.request_id, "status": hi.status}

    @tool
    async def get_human_response(request_id: str) -> dict[str, Any]:
        """Status (pending | answered | expired) and the answer, if any."""
        try:
            hi = svc.collab.get_human_input(request_id)
        except CollaborationError as e:
            raise GatewayToolError(str(e)) from e
        return {
            "request_id": hi.request_id,
            "status": hi.status,
            "response": hi.response,
            "responder": hi.responder,
        }

    @tool
    async def submit_report(
        text: str,
        task_id: str | None = None,
        project: str | None = None,
        session_id: str | None = None,
        agent_status: str | None = None,
        images: list[dict] | None = None,
        handoff_id: str | None = None,
        image_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        """Report work on a task: a short summary for Telegram, no logs or command output (title line,
        then at most 5 short bullets, at most 500 characters). Write it in polite Vietnamese
        ("gọi dạ bảo vâng"): the title starts with "Dạ,", "ạ" where natural.
        image_paths: absolute paths of local PNG/JPEG/WebP/GIF files (e.g. screenshots you saved).
        images: [{"name": "shot.png", "data_base64": "..."}], when there is no file.
        The gateway publishes the report to Telegram and records agent status separately from verification.
        Work not started through the gateway: omit task_id and pass project; a record-only task is created
        and its task_id returned. Telegram replies to the report appear in get_task_result(task_id).feedback."""
        if task_id is None:
            if not project:
                raise GatewayToolError("pass task_id, or project for work done outside the gateway")
            title = text.strip().splitlines()[0] if text.strip() else "report"
            task_id = svc.register_external(title, "mcp", project, session_id).task_id
        task_or_error(task_id)
        if agent_status not in (None, "completed", "partial", "blocked", "failed", "in_progress"):
            raise GatewayToolError(
                "agent_status must be completed|partial|blocked|failed|in_progress"
            )
        decoded = []
        for i, img in enumerate(images or []):
            try:
                decoded.append(
                    (
                        str(img.get("name") or f"screenshot_{i}.png")[:100],
                        base64.b64decode(img["data_base64"], validate=True),
                    )
                )
            except (KeyError, binascii.Error) as e:
                raise GatewayToolError(f"image {i}: invalid base64 payload") from e
        decoded += paths_or_error(image_paths)
        s = svc.channel.sessions.get(session_id or "")
        source = f"{s.agent} ({s.mode} session)" if s and s.agent else "mcp-caller"
        try:
            rep = await svc.collab.submit_report(
                task_id,
                text,
                source=source,
                agent_status=agent_status,
                images=decoded,
                handoff_id=handoff_id,
            )
        except CollaborationError as e:
            raise GatewayToolError(str(e)) from e
        return {
            "task_id": task_id,
            "report_id": rep.report_id,
            "verified_status": rep.verified_status,
            "artifact_ids": rep.artifact_ids,
            "pending_actions": rep.pending_actions,
        }

    @tool
    async def report_deployment(
        project: str,
        environment: str,
        status: str,
        version: str | None = None,
        build_number: str | None = None,
        commit: str | None = None,
        platforms: list[str] | None = None,
        targets: list[str] | None = None,
        url: str | None = None,
        notes: str | None = None,
        error: str | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Post a deployment notice to the Telegram report channel and record it.
        status: succeeded | failed | partial | dry_run | started | cancelled. Report the real outcome of the
        deploy command (its exit code), never an assumed one. For every real build (also one asked for
        via Telegram /send): notes = a short Vietnamese release note (what is new or fixed for testers),
        url = the link to the build (Firebase App Distribution, TestFlight, web or store page).
        Returns whether the channel message was delivered."""
        from pydantic import ValidationError

        from app.collaboration import Deployment

        try:
            d = Deployment(
                project=project,
                environment=environment,
                status=status,
                version=version,
                build_number=build_number,
                commit=commit,
                platforms=platforms or [],
                targets=targets or [],
                url=url,
                notes=notes,
                error=error,
                source="mcp",
                task_id=task_id,
                session_id=session_id,
            )
        except ValidationError as e:
            raise GatewayToolError(
                f"invalid deployment report: {e.errors()[0]['loc']}: {e.errors()[0]['msg']}"
            ) from e
        d = await svc.collab.report_deployment(d)
        return {
            "deployment_id": d.deployment_id,
            "delivered": d.delivered,
            "delivery_error": d.delivery_error,
            "channel_message_id": d.channel_message_id,
        }

    @tool
    async def generate_art(
        prompt: str,
        purpose: str,
        requirements: str,
        width: int = 1024,
        height: int = 1024,
        transparent: bool = False,
        project: str | None = None,
        session_id: str | None = None,
        output: str = "image",
        image_path: str | None = None,
        model_format: str = "glb",
        texture: bool = True,
    ) -> dict[str, Any]:
        """Generate game art / a design image with the configured image provider (fal.ai): icons,
        sprites, backgrounds, UI mockups, banners. Paid: the operator approves each one on Telegram.
        Describe the expected output precisely, the model only knows what you write:
        prompt = the subject; purpose = what the image is for (e.g. "in-game coin sprite, shown at
        64 px"); requirements = the exact output: style, palette, view/composition, background,
        what must or must not appear (e.g. "flat vector, gold #F5C542 with dark outline, front view,
        centered with 10% margin, solid #1B2A49 background, no text, no shadow").
        Returns a task_id at once; pass your session_id: the operator's decision arrives through
        get_messages as an "answer" message (no polling), get_task_result -> images[].path is the
        PNG to copy into the repo. width/height 16-3840; transparent=true also requires an alpha
        channel (flux models produce opaque images, so the check fails for them).
        output="model3d": a 3D model with Meshy instead (props, characters, items). prompt describes
        the object (max 800 characters with purpose and requirements); image_path: absolute path of
        a reference image (image-to-3D); model_format glb | fbx | obj | usdz | stl; texture=false for
        a bare mesh. The operator reviews its preview; get_task_result(...).models[].path is the file."""
        if output == "model3d":
            return await art_3d(
                prompt,
                purpose,
                requirements,
                project,
                session_id,
                image_path,
                model_format,
                texture,
            )
        if output != "image":
            raise GatewayToolError('output must be "image" or "model3d"')
        if len(prompt.strip()) < 3 or not (16 <= width <= 3840 and 16 <= height <= 3840):
            raise GatewayToolError("prompt needs 3+ characters; width and height must be 16-3840")
        if len(purpose.strip()) < 3 or len(requirements.strip()) < 10:
            raise GatewayToolError(
                "describe the expected output: purpose (what the image is for) and requirements "
                "(style, palette, view/composition, background, what must or must not appear)"
            )
        prompt = art_brief(prompt, purpose, requirements, width, height, transparent)
        image = svc.image_provider
        if image is None or not getattr(image, "available", True):
            raise GatewayToolError(
                "no image provider is on: fal is off in config/providers/models.yaml or FAL_KEY is unset"
            )
        data = {"prompt": prompt, "width": width, "height": height, "transparent": transparent}
        data.update({k: v for k, v in (("project", project), ("session_id", session_id)) if v})
        try:
            task = await svc.submit_request(
                TaskRequest(
                    objective=f"Art: {prompt[:200]}",
                    task_type="design_image",
                    data=data,
                    user_id="mcp",
                    source="mcp",
                )  # fmt: skip
            )
        except ValueError as e:
            raise GatewayToolError(str(e)) from e
        return {
            "task_id": task.task_id,
            "state": task.state.value,
            "note": "the operator approves the cost, then reviews the image on Telegram; with your "
            'session_id the decision arrives through get_messages as an "answer" message (no '
            "polling): accepted -> it lists the files (images[0].path in get_task_result); "
            "changes_requested -> a new version is made and reviewed.",
        }

    @tool
    async def get_messages(
        project: str,
        session_id: str | None = None,
        cwd: str | None = None,
        limit: int = 1,
        agent: str | None = None,
        client: str | None = None,
    ) -> dict[str, Any]:
        """Pull mode: fetch requests for this project: from the operator (Telegram /send, replies) and
        from other agents (ask_agent; meta.kind "request" -> answer with reply_message) plus answers to
        your own asks (meta.kind "answer" -> use them, no reply). agent: your tool, e.g. "claude_code";
        client: "app" when you run as a desktop app or in an IDE, "cli" in a terminal.
        Call it when the `agw wait` waiter wakes you (or from `/loop 1m`).
        Each message: message_id, text (the request), meta.
        Do each request, then answer it with reply_message(message_id=..., session_id=...).
        The session stays registered while it polls (5 min); the operator can switch it off (/off)."""
        sess, msgs, busy = svc.channel.pull(
            project, session_id, cwd or "", max(1, min(limit, 20)), agent or "", client or ""
        )
        if sess.closing:
            note = ("closed by the operator: do not restart the agw wait waiter, delete any recurring "
                    "get_messages job of this session (CronList, then CronDelete) and stop polling")  # fmt: skip
        elif not sess.enabled:
            note = "this session is switched off by the operator (/on to resume); do nothing"
        elif busy:
            note = ("busy: finish the in-progress request(s) and answer them with reply_message first; "
                    "no new work is handed out until then, so do nothing this round. If you no longer "
                    "have that request in mind (a fresh run), in_progress_messages has it: finish it now")  # fmt: skip
        else:
            note = "" if msgs else "no new requests; do nothing"
        # Polled every minute: only non-default fields, so an idle poll costs a few tokens.
        out: dict[str, Any] = {"session_id": sess.session_id, "messages": [slim(m) for m in msgs]}
        if not sess.enabled:
            out["enabled"] = False
        if sess.closing:
            out["closed"] = True
        if busy:
            out["busy"] = True
            out["in_progress_messages"] = [slim(m) for m in busy]
        if note:
            out["note"] = note
        return out

    @tool
    async def reply_message(
        text: str,
        session_id: str,
        message_id: str | None = None,
        image_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        """Pull mode: answer the operator on Telegram with a short summary, no logs (first line =
        title, then at most 5 short bullets: what changed, what was validated, what was not; at most
        500 characters), in
        polite Vietnamese: start with "Dạ,", "ạ" where natural.
        image_paths: absolute paths of local PNG/JPEG/WebP/GIF files to attach (e.g. screenshots).
        Their Telegram reply comes back through get_messages."""
        images = paths_or_error(image_paths)
        try:
            rep = await svc.channel.reply(session_id, text, message_id, images)
        except KeyError as e:
            raise GatewayToolError(f"unknown session {session_id}: call get_messages first") from e
        except PermissionError as e:
            raise GatewayToolError(str(e)) from e
        except CollaborationError as e:
            raise GatewayToolError(str(e)) from e
        out = reply_result(rep)
        s = svc.channel.sessions.get(session_id)
        if s and s.mode == "pull" and s.cwd and s.kind == "app" and s.agent in ("", "claude_code"):
            from app.waiter import command  # its waiter exited to wake it: the moment to restart it

            out["next"] = ("start your gateway waiter again now with the Bash tool, run_in_background "
                           f"true: `{command(s.project, s.cwd, s.client)}`")  # fmt: skip
        elif s and s.mode == "pull" and s.cwd and s.agent == "antigravity":
            from app.waiter import command  # Antigravity stopped it when the wake-up started a turn

            out["next"] = ("start your gateway waiter again now as a background terminal command you "
                           f"do not wait for: `{command(s.project, s.cwd, 'app', 'antigravity')}`")  # fmt: skip
        return out

    @tool
    async def list_agents() -> dict[str, Any]:
        """Agent sessions that are open now (any project, any agent tool): s# id, project, agent,
        mode (push/pull), whether it can take work, what it is busy with. Use before ask_agent."""
        hub = svc.channel
        agents = []
        for s in hub.live():
            a = {"id": f"s{s.num}", "name": s.title, "project": s.project, "agent": s.agent or None, "mode": s.mode,
                 "folder": s.cwd.rstrip("/").rsplit("/", 1)[-1]}  # fmt: skip
            if not s.deliverable:
                a["can_take_work"] = False
            if busy := [f"m{m.get('num')}" for m in hub.in_progress(s.session_id)]:
                a["busy_with"] = busy
            agents.append(a)
        return {"agents": agents}

    @tool
    async def review_project(
        project: str, mode: str, role: str = "qa", agent: str = "claude", model: str | None = None,
        evidence: str | None = None, ref: str | None = None, session_id: str | None = None,
    ) -> dict[str, Any]:  # fmt: skip
        """Independent review or research of a game/app with the game-review skill, by a fresh
        read-only agent (not you), in the project's folder. Use it whenever you need an evaluation,
        a second opinion, market or competitor research, a clone-vs-original check, or ideas.
        mode: benchmark (vs top store apps: research), parity (our clone vs the original),
        playtest (what is wrong in the build), ideation (scored ideas). role: qa, po or both.
        agent: claude, droid, codex, gemini, opencode or all (every installed CLI, in parallel;
        different models disagree, which is the point). evidence: a folder of fresh screenshots of
        the build (ref_*.png = the original); without it visual findings are unverified. ref: names
        or paths of the original/competitor apps. Takes minutes: pass your session_id and do other
        work; the result arrives through get_messages as an "answer" message (get_review(review_id)
        shows it too). The operator sees start and end on Telegram. Reports are read-only advice: turning
        findings into code or issues needs the operator's approval."""
        from app.reviews import ReviewError

        try:
            r = svc.reviews.start(
                project, mode, role, agent, model or "", evidence or "", ref or "", session_id or ""
            )
        except ReviewError as e:
            raise GatewayToolError(str(e)) from e
        return {k: r[k] for k in ("review_id", "project", "mode", "role", "agent", "state")}

    @tool
    async def get_review(review_id: str) -> dict[str, Any]:
        """State of a review_project run: running, done (files = report paths to read, then write
        the summary the skill describes) or failed (problems says why)."""
        r = svc.reviews.get(review_id)
        if r is None:
            raise GatewayToolError(f"unknown review {review_id}")
        return {k: r.get(k) for k in ("review_id", "project", "mode", "role", "agent", "state", "files",
                                      "problems", "started_at", "finished_at")}  # fmt: skip

    @tool
    async def ask_agent(
        to: str, request: str, session_id: str | None = None, project: str | None = None,
        wait_s: int = 600,
    ) -> dict[str, Any]:  # fmt: skip
        """Ask another agent to do something, or for information: to = s# (that session, see
        list_agents), agent:<tool> (e.g. agent:opencode: an open session of that agent tool, your
        project's first) or a project (p#, name, fragment: its best session). Pass your own session_id
        (from get_messages) so the answer reaches your inbox; it arrives there as an "answer"
        message (no reply needed), and get_ask(ask_id) shows it too. Write the request so it stands
        alone: what you need, why, and what the answer must contain. If the other agent cannot take
        it or does not answer within wait_s, the operator is asked on Telegram (resend, another
        session, open it, answer it themself, or cancel); keep doing other work meanwhile."""
        try:
            a = svc.asks.create(
                to, request, from_session=session_id, from_project=project, wait_s=wait_s
            )
        except ValueError as e:
            raise GatewayToolError(str(e)) from e
        return ask_view(a)

    @tool
    async def get_ask(ask_id: str) -> dict[str, Any]:
        """State of an ask: queued | delivered | escalated (the operator is deciding) | answered (with
        the answer) | cancelled."""
        a = svc.asks.get(ask_id)
        if a is None:
            raise GatewayToolError(f"unknown ask {ask_id}")
        return ask_view(a)

    return server


def paths_or_error(paths: list[str] | None) -> list[tuple[str, bytes]]:
    try:
        return load_image_paths(paths)
    except AttachmentError as e:
        raise GatewayToolError(str(e)) from e


def slim(m: dict) -> dict[str, Any]:
    """A message for get_messages, without meta keys that repeat the message id or the project."""
    meta = {k: v for k, v in (m.get("meta") or {}).items() if k not in ("message_id", "project")}
    return {"message_id": m["message_id"], "text": m["text"], **({"meta": meta} if meta else {})}


def reply_result(rep) -> dict[str, Any]:
    if isinstance(rep, dict):  # routed back to another agent (ask), or nothing to do
        if "ask" in rep:
            a = rep["ask"]
            return {"ask_id": a["ask_id"], "answered": a["state"] == "answered",
                    "note": "your answer went back to the agent that asked"}  # fmt: skip
        return rep
    return {"report_id": rep.report_id, "task_id": rep.task_id}


def ask_view(a: dict) -> dict[str, Any]:
    keys = ("ask_id", "state", "to_project", "to_session", "delivered_to", "deadline", "answer",
            "answered_by", "reason", "cancelled_by")  # fmt: skip
    return {k: a.get(k) for k in keys if a.get(k) is not None}


TOOL_NAMES = [
    "report_deployment",
    "delegate_task",
    "get_task_status",
    "get_task_result",
    "list_capabilities",
    "get_capability_details",
    "cancel_task",
    "request_human_input",
    "get_human_response",
    "close_question",
    "submit_report",
    "get_messages",
    "reply_message",
    "generate_art",
    "list_agents",
    "ask_agent",
    "get_ask",
    "review_project",
    "get_review",
]
