"""Model tool-calling loop over the Chat Completions wire format.

A model tool call is a request: every call is validated against the registry,
checked by policy and executed by ToolExecutor before its result is returned to
the model with role="tool". Bounded by iterations, wall-clock time and tool calls.
"""

import asyncio
import json
import re
import time
from dataclasses import dataclass, field

from app.connectors.base import Cap, UnsupportedCapability
from app.tools import ToolContext, ToolExecutionRecord, ToolExecutor, ToolStatus

_SAFE = re.compile(r"[^a-zA-Z0-9_-]")


def wire_name(tool_name: str) -> str:
    """Function names must match ^[a-zA-Z0-9_-]{1,64}$ on the wire."""
    return _SAFE.sub("__", tool_name)[:64]


@dataclass
class LoopLimits:
    max_iterations: int = 6
    max_seconds: float = 300
    max_tool_calls: int = 12


@dataclass
class LoopResult:
    status: str  # succeeded | budget_exhausted | approval_required
    text: str
    records: list[ToolExecutionRecord] = field(default_factory=list)
    iterations: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    model: str | None = None


async def run_tool_loop(
    provider,
    executor: ToolExecutor,
    ctx: ToolContext,
    prompt: str,
    tool_names: list[str],
    system: str | None = None,
    limits: LoopLimits | None = None,
) -> LoopResult:
    limits = limits or LoopLimits()
    if Cap.tool_calling not in provider.caps:
        raise UnsupportedCapability(f"{provider.name} does not support tool calling")
    by_wire = {}
    tools = []
    for name in tool_names:
        t = executor.registry.get(name)
        if t is None:
            raise UnsupportedCapability(f"tool {name!r} is not registered")
        by_wire[wire_name(name)] = name
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": wire_name(name),
                    "description": t.description[:1000],
                    "parameters": t.input_schema,
                },
            }
        )
    messages = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": prompt}
    ]
    out = LoopResult("budget_exhausted", "")
    deadline = time.monotonic() + limits.max_seconds
    calls = 0
    while out.iterations < limits.max_iterations:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        out.iterations += 1
        msg, raw = await asyncio.wait_for(
            provider.chat({"messages": messages, "tools": tools}), remaining
        )
        usage = raw.get("usage") or {}
        out.input_tokens += usage.get("prompt_tokens") or 0
        out.output_tokens += usage.get("completion_tokens") or 0
        out.model = raw.get("model", out.model)
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            out.status, out.text = "succeeded", msg.get("content") or ""
            return out
        messages.append(
            {"role": "assistant", "content": msg.get("content"), "tool_calls": tool_calls}
        )
        for call in tool_calls:
            calls += 1
            fn = call.get("function") or {}
            call_id = call.get("id") or f"call_{calls}"
            if calls > limits.max_tool_calls:
                content = json.dumps({"status": "failed", "error": "tool call budget exhausted"})
            else:
                name = by_wire.get(fn.get("name", ""), fn.get("name", ""))
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = None  # executor reports invalid_input
                rec = await executor.execute(
                    name, args if args is not None else "<invalid json>", ctx, call_id=call_id
                )
                out.records.append(rec)
                if rec.status == ToolStatus.approval_required:
                    out.status = "approval_required"
                    out.text = f"tool {name} needs user approval"
                    return out
                content = rec.as_model_result()
            messages.append({"role": "tool", "tool_call_id": call_id, "content": content})
    return out
