"""The gateway as an MCP server for upstream clients (e.g. Claude Code via `--transport http`).

Thin facade: every tool calls the shared task service / collaboration service. No second
orchestration engine lives here. Served over Streamable HTTP at /mcp (bearer API_TOKEN).
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

from mcp.server.mcpserver import MCPServer

from app.agent_registry import CapabilityError
from app.collaboration import CollaborationError
from app.intake import TaskRequest
from app.workflow import StepState


class GatewayToolError(Exception):
    pass


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

    def task_or_error(task_id: str):
        task = svc.tasks.get(task_id)
        if task is None:
            raise GatewayToolError(f"unknown task {task_id}")
        return task

    @server.tool()
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
        project / session_id label every Telegram message about the task."""
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

    @server.tool()
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

    @server.tool()
    async def get_task_result(task_id: str) -> dict[str, Any]:
        """Result envelope plus handoffs (with implementation prompts) and reports."""
        task = task_or_error(task_id)
        env = svc.envelope(task).model_dump(mode="json")
        env["handoffs"] = [h.model_dump(mode="json") for h in svc.collab.handoffs_for(task_id)]
        env["reports"] = [
            r.model_dump(mode="json", exclude={"telegram"}) for r in svc.collab.reports_for(task_id)
        ]
        env["feedback"] = [
            {"feedback_id": f.feedback_id, "report_id": f.report_id, "text": f.text,
             "user_id": f.user_id, "created_at": f.created_at.isoformat()}
            for f in svc.collab.feedback_for(task_id)
        ]  # fmt: skip
        return env

    @server.tool()
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

    @server.tool()
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

    @server.tool()
    async def cancel_task(task_id: str) -> dict[str, Any]:
        """Cancel a task. 'unknown' means an in-flight side effect may already have happened."""
        task_or_error(task_id)
        stopped = await svc.stop(task_id)
        return {"task_id": task_id, "stopped": stopped, "state": svc.tasks[task_id].state.value}

    @server.tool()
    async def request_human_input(
        question: str,
        task_id: str | None = None,
        options: list[str] | None = None,
        ttl_s: int | None = None,
        project: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Ask the operator a question via Telegram. Poll get_human_response with the returned id."""
        try:
            hi = await svc.collab.request_human_input(
                question, task_id, options or [], ttl_s, project, session_id
            )
        except CollaborationError as e:
            raise GatewayToolError(str(e)) from e
        return {
            "request_id": hi.request_id,
            "status": hi.status,
            "expires_at": hi.expires_at.isoformat(),
        }

    @server.tool()
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

    @server.tool()
    async def submit_report(
        text: str,
        task_id: str | None = None,
        project: str | None = None,
        session_id: str | None = None,
        agent_status: str | None = None,
        images: list[dict] | None = None,
        handoff_id: str | None = None,
    ) -> dict[str, Any]:
        """Report work on a task. images: [{"name": "shot.png", "data_base64": "..."}] (PNG/JPEG/WebP/GIF).
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
        try:
            rep = await svc.collab.submit_report(
                task_id,
                text,
                source="mcp-caller",
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

    @server.tool()
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
        deploy command (its exit code), never an assumed one. Returns whether the channel message was delivered."""
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

    return server


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
    "submit_report",
]
