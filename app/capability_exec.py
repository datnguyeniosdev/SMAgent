"""Executors for config-driven `capability` steps and the collaboration internal ops.

A capability step validates its input against the capability schema, asks the
AgentCapabilityRegistry for the best eligible executor, runs it, validates the output, and
falls back to the next eligible executor only for availability/transport failures.
"""

from __future__ import annotations

import asyncio
import hashlib
import json

import jsonschema

from app.agent_registry import CapabilityError, executor_id
from app.collaboration import (
    DESIGN_SPEC_SCHEMA,
    CollaborationError,
    Handoff,
    build_implementation_prompt,
)
from app.connectors.base import AgentRequest, ConnectorError
from app.engine import StepError, StepOutput
from app.models import Attachment, now
from app.workflow import StepDef
from app.workspace_agents import IMPLEMENTER_SYSTEM

FALLBACK_CATEGORIES = {
    "unavailable",
    "auth",
    "network",
    "mcp_connection",
    "rate_limit",
    "timeout",
    "capability_unavailable",
    "missing_credentials",
    "mcp_disconnected",
}
MAX_EXECUTOR_TRIES = 3


async def exec_capability(svc, task, run, step: StepDef, inputs: dict) -> StepOutput:
    reg = svc.registry
    if reg is None:
        raise StepError("capability steps need a gateway configuration", "unsupported")
    try:
        cap = reg._capability(step.ref)
        jsonschema.validate(inputs, cap.input_schema)
    except CapabilityError as e:
        raise StepError(str(e), e.code) from e
    except jsonschema.ValidationError as e:
        raise StepError(
            f"invalid input for capability {step.ref}: {e.message}", "invalid_input"
        ) from e
    tried: list[dict] = []
    excluded: set[str] = set()
    last: StepError | None = None
    for _ in range(MAX_EXECUTOR_TRIES):
        try:
            choice = reg.select(step.ref, step.agent, exclude=excluded)
        except CapabilityError as e:
            if last is not None:
                break
            raise StepError(f"{e} ({'; '.join(e.details)})", e.code) from e
        spec = choice.spec
        eid = executor_id(spec)
        try:
            out = await _run(svc, task, run, step, cap, spec, inputs)
        except StepError as e:
            tried.append({"executor": eid, "error": str(e)[:200], "category": e.category})
            if e.category in FALLBACK_CATEGORIES and e.outcome != "unknown":
                excluded.add(eid)
                last = e
                continue
            e.data = {**(e.data or {}), "executor_attempts": tried}
            raise
        if cap.output_schema is not None:
            try:
                jsonschema.validate(out.data, cap.output_schema)
            except jsonschema.ValidationError as e:
                raise StepError(
                    f"executor {eid} returned invalid output for {cap.id}: {e.message}",
                    "invalid_output",
                    data={"executor_attempts": tried},
                ) from e
        svc.caps.record_execution(f"capability:{cap.id}", True, f"{eid} in task {task.task_id}")
        tried.append({"executor": eid, "status": "succeeded"})
        if isinstance(out.data, dict):
            out.data = {**out.data, "_executor": eid, "_executor_attempts": tried}
        out.executor_ref = f"capability:{cap.id}→{eid}"
        return out
    svc.caps.record_execution(f"capability:{step.ref}", False, str(last))
    raise StepError(
        f"all eligible executors failed for {step.ref}: {last}",
        last.category if last else "unsupported",
        retryable=bool(last and last.retryable),
        data={"executor_attempts": tried},
    )


async def _run(svc, task, run, step, cap, spec, inputs) -> StepOutput:
    synthetic = StepDef(
        id=step.id,
        executor="tool",
        ref=spec.ref,
        permissions=spec.permissions,
        timeout_s=step.timeout_s,
    )
    if spec.type == "tool":
        return await svc._exec_tool(task, run, synthetic, inputs)
    if spec.type == "plugin":
        return await svc._exec_plugin(
            task, run, synthetic.model_copy(update={"executor": "plugin"}), inputs
        )
    if spec.type == "internal":
        return await svc._exec_internal(
            task, run, synthetic.model_copy(update={"executor": "internal"}), inputs
        )
    if spec.type == "provider":
        return await exec_provider(svc, task, step, cap, spec, inputs)
    if spec.type == "runtime":
        return await exec_runtime(svc, task, run, step, spec, inputs)
    raise StepError(f"unsupported executor type {spec.type}", "unsupported")


def _image_inputs(svc, task, ids: list[str]) -> list[tuple[Attachment, bytes]]:
    out = []
    for ref in ids or []:
        name, mime, data = svc.read_input(task, ref)
        att = Attachment(
            attachment_id=ref,
            filename=name,
            mime_type=mime,
            size_bytes=len(data),
            sha256="-",
            storage_path=".",
            source="artifact",
            task_id=task.task_id,
        )
        out.append((att, data))
    return out


async def exec_provider(svc, task, step, cap, spec, inputs) -> StepOutput:
    conn = svc.router.connectors.get(spec.ref)
    if conn is None:
        raise StepError(f"provider {spec.ref} is not loaded", "unavailable")
    template = spec.options.get("prompt_template")
    data_inputs = {k: v for k, v in inputs.items() if k != "image_artifact_ids"}
    prompt = (
        template.format_map(_SafeDict(data_inputs))
        if template
        else json.dumps(data_inputs, indent=2, default=str)
    )
    structured = spec.operation == "generate_structured"
    req = AgentRequest(
        task_id=task.task_id,
        prompt=prompt,
        system=spec.options.get("system"),
        attachments=_image_inputs(svc, task, inputs.get("image_artifact_ids", [])),
        json_schema=(cap.output_schema or {"type": "object"}) if structured else None,
    )
    holder = svc._child(task, cap.id, f"{cap.id} via {spec.ref}")
    try:
        result = await svc._call(holder, conn, req, cap.id)
    except ConnectorError as e:
        raise StepError(f"{spec.ref}: {e}", e.category, e.retryable, e.outcome) from e
    holder.result = result.text[:2000]
    svc._set_state(holder, svc_state("succeeded"))
    data = result.structured if structured else {"text": result.text}
    art = svc.artifacts.add(
        task.task_id,
        "report",
        f"{step.id}.json" if structured else f"{step.id}.md",
        json.dumps(data, indent=2) if structured else result.text,
        step_id=step.id,
        producer=f"provider:{spec.ref}:{result.model}",
    )
    if structured and not isinstance(data, dict):
        raise StepError(f"{spec.ref} returned non-object JSON", "invalid_output")
    return StepOutput(data, [art.artifact_id], f"provider:{spec.ref}")


async def exec_runtime(svc, task, run, step, spec, inputs) -> StepOutput:
    prompt = str(inputs.get("prompt") or "").strip()
    if not prompt:
        raise StepError("handoff needs a non-empty prompt", "invalid_input")
    h = Handoff(
        task_id=task.task_id,
        workflow_run_id=run.run_id,
        target=spec.ref,
        mode=spec.operation,
        prompt=prompt,
        prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
        context_refs=list(inputs.get("context_refs") or []),
        status="pending_manual_continuation" if spec.operation == "return_to_caller" else "running",
    )
    svc.collab.save_handoff(h)
    svc.store.event(
        "handoff.created",
        task_id=task.task_id,
        run_id=run.run_id,
        handoff_id=h.handoff_id,
        target=h.target,
        mode=h.mode,
    )
    if spec.operation == "return_to_caller":
        return StepOutput(
            {
                "handoff_id": h.handoff_id,
                "mode": h.mode,
                "status": h.status,
                "target": h.target,
                "prompt_sha256": h.prompt_sha256,
                "next_action": "caller runs the prompt and calls submit_report with this handoff_id",
            },
            [],
            f"runtime:{spec.ref}.return_to_caller",
        )
    conn = svc.router.connectors.get(spec.ref)
    if conn is None:
        h.status = "failed"
        svc.collab.save_handoff(h)
        raise StepError(f"runtime {spec.ref} is not loaded", "unavailable")
    ws_path = await svc.task_workspace(task)
    holder = svc._child(task, "implementer", f"handoff {h.handoff_id} → {spec.ref}")
    req = AgentRequest(
        task_id=holder.task_id,
        prompt=prompt,
        system=IMPLEMENTER_SYSTEM,
        workspace=ws_path,
        read_only=False,
    )  # a NEW execution: remote session resume is not claimed
    try:
        result = await svc._call(holder, conn, req, "implementer")
    except asyncio.CancelledError:
        h.status, h.result_summary, h.updated_at = (
            "unknown",
            "cancelled while the runtime was executing",
            now(),
        )
        svc.collab.save_handoff(h)  # edits may already have happened: never "cancelled cleanly"
        raise
    except ConnectorError as e:
        h.status = "unknown" if e.outcome == "unknown" else "failed"
        h.result_summary, h.updated_at = str(e)[:500], now()
        svc.collab.save_handoff(h)
        raise StepError(f"{spec.ref}: {e}", e.category, e.retryable, e.outcome) from e
    svc._set_state(holder, svc_state("succeeded"))
    h.status, h.session_ref, h.result_summary, h.updated_at = (
        "succeeded",
        result.session_id,
        result.text[:1000],
        now(),
    )
    arts = []
    ws = svc._worktrees.get(task.task_id)
    if ws is not None:
        diff, files = await svc.workspaces.diff(ws)
        arts.append(
            svc.artifacts.add(
                task.task_id,
                "diff",
                f"{h.handoff_id}.diff",
                diff,
                step_id=step.id,
                producer=f"runtime:{spec.ref}",
                files=files,
            ).artifact_id
        )
    h.artifact_ids = arts
    svc.collab.save_handoff(h)
    return StepOutput(
        {
            "handoff_id": h.handoff_id,
            "mode": h.mode,
            "status": h.status,
            "session_ref": h.session_ref,
            "agent_summary": h.result_summary,
            "verified": False,
            "runtime_evidence": result.metadata,  # e.g. OpenCode files_changed + permission decisions
        },
        arts,
        f"runtime:{spec.ref}",
    )


# ---------------------------------------------------------------- internal ops


async def internal_op(svc, task, run, step: StepDef, inputs: dict) -> StepOutput | None:
    try:
        if step.ref == "context.load":
            ctx = svc.collab.load_context(
                inputs["source_task_id"],
                inputs.get("report_id"),
                inputs.get("feedback_id"),
                task.task_id,
            )
            return StepOutput(ctx, [], "internal:context.load")
        if step.ref == "prompt.build":
            try:
                jsonschema.validate(inputs.get("design_spec"), DESIGN_SPEC_SCHEMA)
            except jsonschema.ValidationError as e:
                raise StepError(
                    f"design specification invalid: {e.message}", "invalid_input"
                ) from e
            known = []
            for a in (
                inputs.get("artifacts") or []
            ):  # only artifacts that really exist for this task
                if svc.artifact_ok(task, a.get("artifact_id", "")):
                    known.append(a)
            out = build_implementation_prompt(
                objective=inputs["objective"],
                feedback=inputs.get("feedback", ""),
                design_spec=inputs["design_spec"],
                constraints=list(inputs.get("constraints") or []),
                artifacts=known,
                validation_requirements=list(inputs.get("validation_requirements") or []),
                task_id=task.task_id,
            )
            art = svc.artifacts.add(
                task.task_id,
                "prompt",
                "implementation_prompt.md",
                out["prompt"],
                step_id=step.id,
                producer="internal:prompt.build",
            )
            return StepOutput(out, [art.artifact_id], "internal:prompt.build")
        if step.ref == "report.publish":
            arts = [a for a in (inputs.get("artifact_ids") or []) if isinstance(a, str)]
            rep = await svc.collab.submit_report(
                task.task_id,
                str(inputs.get("text") or ""),
                source="gateway",
                agent_status=inputs.get("agent_status"),
                artifact_ids=arts,
                handoff_id=None,
                workflow_run_id=run.run_id,
            )
            return StepOutput(
                {"report_id": rep.report_id, "verified_status": rep.verified_status},
                [],
                "internal:report.publish",
            )
    except CollaborationError as e:
        raise StepError(str(e), e.code) from e
    except KeyError as e:
        raise StepError(f"missing input {e}", "invalid_input") from e
    return None


class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"

    def __getitem__(self, key):
        v = dict.get(self, key, None)
        if v is None:
            return self.__missing__(key)
        return json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v


def svc_state(name: str):
    from app.models import TaskState

    return TaskState(name)
