"""Tool registry, policy, executor and execution records.

Lifecycle: discovered -> ready (registered, schema valid) -> [approval_required] -> running
-> succeeded | failed | timed_out | cancelled | unknown.
A tool call from a model is only a request: it runs here, through the registry, or not at all.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol

import jsonschema
from pydantic import BaseModel, Field

from app.models import new_id, now

log = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 20_000


class ToolStatus(StrEnum):
    approval_required = "approval_required"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    timed_out = "timed_out"
    cancelled = "cancelled"
    unknown = "unknown"  # side effects may have happened; outcome not established


TOOL_TERMINAL = {
    ToolStatus.succeeded,
    ToolStatus.failed,
    ToolStatus.timed_out,
    ToolStatus.cancelled,
    ToolStatus.unknown,
}
_TOOL_TRANSITIONS = {
    None: {ToolStatus.approval_required, ToolStatus.running, ToolStatus.failed},
    ToolStatus.approval_required: {ToolStatus.running, ToolStatus.cancelled},
    ToolStatus.running: TOOL_TERMINAL,
}


class ToolError(Exception):
    """Raised by handlers for an expected, reportable failure."""

    def __init__(self, message: str, category: str = "tool_error"):
        super().__init__(message)
        self.category = category


@dataclass
class ToolOutput:
    text: str = ""
    data: Any = None
    artifact_ids: list[str] = field(default_factory=list)


@dataclass
class ToolContext:
    task_id: str
    workspace: Path | None
    permissions: frozenset[str]  # e.g. workspace:read, workspace:write, process:execute
    actor: str = ""
    approved: frozenset[str] = frozenset()  # tool names the user approved for this context
    artifacts: Any = None  # ArtifactStore
    attachments: Any = None  # AttachmentStore
    read_input: Callable[[str], tuple[str, str, bytes]] | None = None  # task-isolated input refs
    step_id: str | None = None


Handler = Callable[[ToolContext, dict], Awaitable[ToolOutput]]


@dataclass
class ToolDefinition:
    name: str  # stable id, e.g. "workspace.read_file", "mcp.claude_code.Glob"
    description: str
    input_schema: dict
    handler: Handler
    permissions: frozenset[str]
    risk: Literal["read", "write", "execute", "external"] = "read"
    environment: str = "local"  # local | mcp:<server> | provider:<name>
    timeout_s: float = 60
    requires_approval: bool = False
    side_effects: bool = False
    output_schema: dict | None = None
    source: str = "builtin"

    def public(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "permissions": sorted(self.permissions),
            "risk": self.risk,
            "environment": self.environment,
            "timeout_s": self.timeout_s,
            "requires_approval": self.requires_approval,
            "side_effects": self.side_effects,
            "source": self.source,
        }


class ToolExecutionRecord(BaseModel):
    record_id: str = Field(default_factory=lambda: new_id("run"))
    call_id: str | None = None  # idempotency key (e.g. model tool_call id)
    tool: str
    task_id: str
    status: ToolStatus | None = None
    input_keys: list[str] = []  # argument names only; values may be sensitive
    created_at: datetime = Field(default_factory=now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    output: str = ""
    data: Any = None
    error: str | None = None
    error_category: str | None = None
    artifact_ids: list[str] = []

    def transition(self, new: ToolStatus) -> None:
        if new not in _TOOL_TRANSITIONS.get(self.status, set()):
            raise ValueError(f"tool record {self.record_id}: {self.status} -> {new}")
        self.status = new
        if new == ToolStatus.running:
            self.started_at = now()
        if new in TOOL_TERMINAL:
            self.finished_at = now()

    @property
    def done(self) -> bool:
        return self.status in TOOL_TERMINAL

    def as_model_result(self) -> str:
        """Compact JSON returned to a model as the tool result."""
        body = {"status": self.status, "output": self.output[:8000]}
        if self.error:
            body["error"] = self.error
        if self.artifact_ids:
            body["artifact_ids"] = self.artifact_ids
        return json.dumps(body, default=str)


class ToolEventPublisher(Protocol):
    async def tool_event(self, record: ToolExecutionRecord, event: str) -> None: ...


@dataclass
class Decision:
    allowed: bool
    approval_required: bool = False
    reason: str = ""


class ToolPolicyEngine:
    def check(self, tool: ToolDefinition, ctx: ToolContext) -> Decision:
        missing = tool.permissions - ctx.permissions
        if missing:
            return Decision(False, reason=f"missing permissions: {', '.join(sorted(missing))}")
        if ("workspace:read" in tool.permissions or "workspace:write" in tool.permissions) and (
            ctx.workspace is None
        ):
            return Decision(False, reason="no authorized workspace for this task")
        if tool.requires_approval and tool.name not in ctx.approved:
            return Decision(False, approval_required=True, reason="user approval required")
        return Decision(True)


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolDefinition] = {}
        self.discovery_notes: list[str] = []  # tools seen but not enabled, failed servers, ...

    def register(self, tool: ToolDefinition) -> None:
        jsonschema.Draft202012Validator.check_schema(tool.input_schema)
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def list(self) -> list[ToolDefinition]:
        return sorted(self._tools.values(), key=lambda t: t.name)


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        policy: ToolPolicyEngine | None = None,
        publisher: ToolEventPublisher | None = None,
        max_concurrent: int = 4,
    ):
        self.registry = registry
        self.policy = policy or ToolPolicyEngine()
        self.publisher = publisher
        self.records: dict[str, ToolExecutionRecord] = {}
        self._by_call: dict[str, ToolExecutionRecord] = {}
        self._sem = asyncio.Semaphore(max_concurrent)

    async def _publish(self, rec: ToolExecutionRecord, event: str) -> None:
        if self.publisher is None:
            return
        try:  # delivery failure must not change the tool outcome
            await self.publisher.tool_event(rec, event)
        except Exception:
            log.exception("tool event delivery failed for %s", rec.record_id)

    def _fail(self, rec: ToolExecutionRecord, category: str, message: str) -> ToolExecutionRecord:
        rec.error, rec.error_category = message, category
        rec.transition(ToolStatus.failed)
        return rec

    async def execute(
        self, name: str, args: dict, ctx: ToolContext, call_id: str | None = None
    ) -> ToolExecutionRecord:
        if call_id and call_id in self._by_call:  # idempotent: never run the same call twice
            return self._by_call[call_id]
        rec = ToolExecutionRecord(
            tool=name,
            task_id=ctx.task_id,
            call_id=call_id,
            input_keys=sorted(args) if isinstance(args, dict) else [],
        )
        self.records[rec.record_id] = rec
        if call_id:
            self._by_call[call_id] = rec
        tool = self.registry.get(name)
        if tool is None:
            known = ", ".join(t.name for t in self.registry.list())
            return self._fail(rec, "unknown_tool", f"unknown tool {name!r}; available: {known}")
        if not isinstance(args, dict):
            return self._fail(rec, "invalid_input", "arguments must be a JSON object")
        try:
            jsonschema.validate(args, tool.input_schema)
        except jsonschema.ValidationError as e:
            return self._fail(rec, "invalid_input", f"invalid input: {e.message}")
        decision = self.policy.check(tool, ctx)
        if decision.approval_required:
            rec.error, rec.error_category = decision.reason, "approval_required"
            rec.transition(ToolStatus.approval_required)
            await self._publish(rec, "approval_required")
            return rec
        if not decision.allowed:
            return self._fail(rec, "forbidden", decision.reason)
        return await self._run(tool, rec, args, ctx)

    async def resume_approved(
        self, record_id: str, args: dict, ctx: ToolContext
    ) -> ToolExecutionRecord:
        rec = self.records[record_id]
        if rec.status != ToolStatus.approval_required:
            raise ValueError(f"record is {rec.status}, not awaiting approval")
        rec.error = rec.error_category = None
        return await self._run(self.registry.get(rec.tool), rec, args, ctx)

    async def _run(self, tool, rec, args, ctx) -> ToolExecutionRecord:
        rec.transition(ToolStatus.running)
        await self._publish(rec, "started")
        try:
            async with self._sem:
                out = await asyncio.wait_for(tool.handler(ctx, args), tool.timeout_s)
            if tool.output_schema is not None:
                jsonschema.validate(out.data, tool.output_schema)
        except TimeoutError:
            rec.error = f"timed out after {tool.timeout_s}s"
            rec.error_category = "timeout"
            # A side-effecting tool may have completed part of its work.
            rec.transition(ToolStatus.unknown if tool.side_effects else ToolStatus.timed_out)
        except asyncio.CancelledError:
            rec.error, rec.error_category = "cancelled", "cancelled"
            rec.transition(ToolStatus.unknown if tool.side_effects else ToolStatus.cancelled)
            await self._publish(rec, "finished")
            raise
        except ToolError as e:
            rec.error, rec.error_category = str(e), e.category
            rec.transition(ToolStatus.failed)
        except jsonschema.ValidationError as e:
            rec.error, rec.error_category = f"invalid output: {e.message}", "invalid_output"
            rec.transition(ToolStatus.unknown if tool.side_effects else ToolStatus.failed)
        except Exception as e:
            log.exception("tool %s crashed", tool.name)
            rec.error, rec.error_category = f"{type(e).__name__}: {e}"[:500], "crash"
            rec.transition(ToolStatus.unknown if tool.side_effects else ToolStatus.failed)
        else:
            text = out.text
            if len(text) > MAX_OUTPUT_CHARS:
                text = text[:MAX_OUTPUT_CHARS] + f"\n…[truncated {len(out.text)} chars]"
            rec.output, rec.data, rec.artifact_ids = text, out.data, out.artifact_ids
            rec.transition(ToolStatus.succeeded)
        await self._publish(rec, "finished")
        return rec

    def for_task(self, task_id: str) -> list[ToolExecutionRecord]:
        return [r for r in self.records.values() if r.task_id == task_id]
