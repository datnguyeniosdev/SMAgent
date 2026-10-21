"""Workflow engine: validated DAG scheduling with bounded concurrency, output contracts,
retries, timeouts, cancellation, approval checkpoints, compensation and recovery.

Executors are injected (tool / agent / plugin / validator / internal / approval) so the
engine stays domain-independent. Every state change is persisted before continuing.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import jsonschema

from app.models import Task, now
from app.store import Store
from app.workflow import (
    STEP_BAD,
    STEP_TERMINAL,
    Condition,
    StepAttempt,
    StepDef,
    StepRecord,
    StepState,
    WorkflowRun,
)

log = logging.getLogger(__name__)


class StepError(Exception):
    def __init__(
        self,
        message: str,
        category: str = "step_error",
        retryable: bool = False,
        outcome: str = "failed",  # failed | timed_out | unknown
        data: Any = None,  # e.g. a failed GateResult, kept as the step's output for evidence
    ):
        super().__init__(message)
        self.category = category
        self.retryable = retryable
        self.outcome = outcome
        self.data = data


@dataclass
class StepOutput:
    data: Any = None
    artifact_ids: list[str] = field(default_factory=list)
    executor_ref: str | None = None


ExecutorFn = Callable[[Task, WorkflowRun, StepDef, dict], Awaitable[StepOutput]]


class EngineHost(Protocol):
    store: Store

    def is_idempotent(self, step: StepDef) -> bool: ...
    def needs_approval(self, step: StepDef) -> bool: ...
    def artifact_ok(self, task: Task, artifact_id: str) -> bool: ...
    async def request_approval(
        self, task: Task, run: WorkflowRun, step: StepDef, rec: StepRecord
    ) -> bool: ...
    async def publish(
        self, type_: str, task: Task, run: WorkflowRun, step_id: str | None = None, **data: Any
    ) -> None: ...


_MISSING = object()


def lookup(obj: Any, path: list[str]) -> Any:
    for p in path:
        if isinstance(obj, dict) and p in obj:
            obj = obj[p]
        elif isinstance(obj, list) and p.isdigit() and int(p) < len(obj):
            obj = obj[int(p)]
        else:
            return _MISSING
    return obj


class WorkflowEngine:
    def __init__(self, host: EngineHost, executors: dict[str, ExecutorFn]):
        self.host = host
        self.executors = executors

    # ---------- data flow ----------

    def resolve(self, value: Any, task: Task, run: WorkflowRun, recs: dict[str, StepRecord]) -> Any:
        if isinstance(value, dict):
            return {k: self.resolve(v, task, run, recs) for k, v in value.items()}
        if isinstance(value, list):
            return [self.resolve(v, task, run, recs) for v in value]
        if not (isinstance(value, str) and value.startswith("$")):
            return value
        parts = value.split(".")
        if parts[0] == "$input":
            got = lookup(run.inputs, parts[1:])
        elif parts[0] == "$context":
            got = lookup(getattr(self.host, "context_for", lambda t: {})(task), parts[1:])
        elif parts[0] == "$task":
            got = lookup(
                {
                    "task_id": task.task_id,
                    "objective": task.description,
                    "attachment_ids": task.attachment_ids,
                },
                parts[1:],
            )
        else:
            rec = recs[parts[1]]
            if rec.state != StepState.succeeded:
                raise StepError(
                    f"mapping {value}: step {parts[1]} is {rec.state}", "invalid_mapping"
                )
            root = {"output": rec.output, "artifacts": rec.artifact_ids, "state": rec.state.value}
            got = lookup(root, parts[2:])
        if got is _MISSING:
            raise StepError(f"mapping {value} resolved to nothing", "invalid_mapping")
        return got

    def condition_holds(self, c: Condition, task, run, recs) -> bool:
        try:
            v = self.resolve(c.ref, task, run, recs)
        except StepError:
            return False
        return {
            "truthy": lambda: bool(v),
            "falsy": lambda: not v,
            "eq": lambda: v == c.value,
            "ne": lambda: v != c.value,
            "in": lambda: v in (c.value or []),
            "gt": lambda: v > c.value,
            "lt": lambda: v < c.value,
        }[c.op]()

    # ---------- persistence ----------

    def _save(self, run: WorkflowRun, rec: StepRecord) -> None:
        self.host.store.put(
            "step",
            f"{run.run_id}:{rec.step_id}",
            rec,
            task_id=run.task_id,
            run_id=run.run_id,
            state=rec.state.value,
        )

    def _save_run(self, run: WorkflowRun) -> None:
        self.host.store.put(
            "run", run.run_id, run, task_id=run.task_id, run_id=run.run_id, state=run.state
        )

    def _move(self, run, rec: StepRecord, state: StepState, error=None, category=None) -> None:
        rec.transition(state)
        if error is not None:
            rec.error, rec.error_category = error, category
        self._save(run, rec)

    # ---------- recovery ----------

    def prepare_recovery(self, run: WorkflowRun, recs: dict[str, StepRecord]) -> list[str]:
        """Decide per interrupted step: safe to resume, or unknown (needs reconciliation)."""
        notes = []
        steps = {s.id: s for s in run.workflow.steps}
        for rec in recs.values():
            step = steps[rec.step_id]
            if rec.state in (StepState.running, StepState.retry_waiting):
                if self.host.is_idempotent(step):
                    self._move(run, rec, StepState.ready)
                    notes.append(f"{rec.step_id}: idempotent, will re-run")
                else:
                    rec.transition(StepState.unknown)
                    rec.error = "interrupted by restart; non-idempotent step may have completed"
                    rec.error_category = "interrupted"
                    self._save(run, rec)
                    notes.append(f"{rec.step_id}: non-idempotent, marked unknown (not replayed)")
            elif rec.state == StepState.waiting_for_approval:
                self._move(run, rec, StepState.ready)
                notes.append(f"{rec.step_id}: approval will be requested again")
        return notes

    # ---------- execution ----------

    async def execute(
        self, task: Task, run: WorkflowRun, recs: dict[str, StepRecord] | None = None
    ) -> WorkflowRun:
        wf = run.workflow
        steps = {s.id: s for s in wf.steps}
        recs = recs or {}
        for s in wf.steps:
            if s.id not in recs:
                recs[s.id] = StepRecord(run_id=run.run_id, step_id=s.id)
                self._save(run, recs[s.id])
        run.state = "running"
        self._save_run(run)
        await self.host.publish("workflow.started", task, run, workflow=wf.id, version=wf.version)
        running: dict[asyncio.Task, str] = {}
        stop_scheduling = False
        cancelled = False
        try:
            async with asyncio.timeout(wf.timeout_s):
                while True:
                    if not stop_scheduling:
                        self._advance(task, run, steps, recs)
                    ready = [
                        s
                        for s in wf.steps
                        if recs[s.id].state == StepState.ready and s.id not in running.values()
                    ]
                    # Backpressure: never more than max_concurrency steps in flight.
                    while ready and len(running) < wf.max_concurrency and not stop_scheduling:
                        s = ready.pop(0)
                        await self.host.publish("step.scheduled", task, run, s.id)
                        t = asyncio.create_task(self._run_step(task, run, s, recs))
                        running[t] = s.id
                    if not running:
                        break
                    done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                    for t in done:
                        sid = running.pop(t)
                        rec = recs[sid]
                        if rec.state in STEP_BAD and self._fail_fast(wf, steps[sid]):
                            stop_scheduling = True
                    if stop_scheduling and running:
                        for t in running:
                            t.cancel()
                        await asyncio.gather(*running, return_exceptions=True)
                        running.clear()
                        break
        except TimeoutError:
            run.error = f"workflow timed out after {wf.timeout_s}s"
            await self._cancel_running(running)
        except asyncio.CancelledError:
            cancelled = True
            await self.host.publish("cancellation.requested", task, run)
            await self._cancel_running(running)
        # Anything never started is cancelled (fail-fast / cancel) or skipped.
        for rec in recs.values():
            if rec.state in (StepState.pending, StepState.ready):
                self._move(
                    run,
                    rec,
                    StepState.cancelled if (cancelled or stop_scheduling) else StepState.skipped,
                    error=rec.error or "not executed",
                )
        await self._finish(task, run, steps, recs, cancelled)
        return run

    def _fail_fast(self, wf, step: StepDef) -> bool:
        return wf.failure_policy == "fail_fast" and step.on_failure == "fail_fast"

    async def _cancel_running(self, running: dict[asyncio.Task, str]) -> None:
        for t in running:
            t.cancel()
        await asyncio.gather(*running, return_exceptions=True)
        running.clear()

    def _advance(self, task, run, steps, recs) -> None:
        """pending -> ready / skipped based on dependencies and conditions (deterministic order)."""
        changed = True
        while changed:
            changed = False
            for s in run.workflow.steps:
                rec = recs[s.id]
                if rec.state != StepState.pending:
                    continue
                deps = [recs[d] for d in s.depends_on]
                bad = [d for d in deps if d.state in STEP_BAD]
                skipped = [d for d in deps if d.state == StepState.skipped]
                if bad or skipped:
                    why = (
                        f"dependency {bad[0].step_id} {bad[0].state}"
                        if bad
                        else f"dependency {skipped[0].step_id} skipped"
                    )
                    self._move(run, rec, StepState.skipped, why, "dependency")
                    changed = True
                elif all(d.state == StepState.succeeded for d in deps):
                    if s.condition and not self.condition_holds(s.condition, task, run, recs):
                        self._move(run, rec, StepState.skipped, "condition false", "condition")
                    else:
                        self._move(run, rec, StepState.ready)
                    changed = True

    async def _run_step(self, task: Task, run: WorkflowRun, step: StepDef, recs) -> None:
        rec = recs[step.id]
        idempotent = self.host.is_idempotent(step)
        try:
            if self.host.needs_approval(step) and not rec.approved_by:
                self._move(run, rec, StepState.waiting_for_approval)
                await self.host.publish("approval.requested", task, run, step.id)
                ok = await self.host.request_approval(task, run, step, rec)
                if not ok:
                    await self.host.publish("approval.denied", task, run, step.id)
                    self._move(
                        run,
                        rec,
                        StepState.cancelled,
                        rec.error or "approval denied",
                        rec.error_category or "approval_denied",
                    )
                    return
                await self.host.publish("approval.granted", task, run, step.id, by=rec.approved_by)
                self._move(run, rec, StepState.ready)
            started = time.monotonic()
            delay = step.retry.delay_s
            while True:
                rec.attempt += 1
                self._move(run, rec, StepState.running)
                rec.attempts.append(
                    StepAttempt(attempt=rec.attempt, state=StepState.running, started_at=now())
                )
                self._save(run, rec)
                await self.host.publish("step.started", task, run, step.id, attempt=rec.attempt)
                try:
                    inputs = self.resolve(step.inputs, task, run, recs)
                    fn = self.executors.get(step.executor)
                    if fn is None:
                        raise StepError(f"no executor {step.executor!r}", "unsupported")
                    try:
                        out = await asyncio.wait_for(fn(task, run, step, inputs), step.timeout_s)
                    except TimeoutError as e:
                        raise StepError(
                            f"timed out after {step.timeout_s}s",
                            "timeout",
                            retryable=True,
                            outcome="timed_out",
                        ) from e
                    self._validate_output(task, step, out)
                except Exception as raw:  # CancelledError is BaseException and passes through
                    e = (
                        raw
                        if isinstance(raw, StepError)
                        else StepError(
                            f"executor crashed: {type(raw).__name__}: {raw}"[:500],
                            "crash",
                            outcome="failed" if idempotent else "unknown",
                        )
                    )
                    if not isinstance(raw, StepError):
                        log.exception("step %s crashed", step.id)
                    final = self._classify(e, idempotent)
                    att = rec.attempts[-1]
                    att.finished_at, att.error, att.error_category = now(), str(e), e.category
                    can_retry = (
                        final != StepState.unknown
                        and idempotent
                        and (e.retryable or e.category in step.retry.retry_on)
                        and rec.attempt < step.retry.max_attempts
                        and (
                            step.retry.max_elapsed_s is None
                            or time.monotonic() - started + delay < step.retry.max_elapsed_s
                        )
                    )
                    if can_retry:
                        att.state = StepState.retry_waiting
                        self._move(run, rec, StepState.retry_waiting, str(e), e.category)
                        await self.host.publish(
                            "step.retry_scheduled",
                            task,
                            run,
                            step.id,
                            attempt=rec.attempt,
                            delay_s=delay,
                            error=str(e)[:300],
                        )
                        await asyncio.sleep(delay)
                        delay *= step.retry.backoff
                        continue
                    if (
                        not idempotent
                        and rec.attempt < step.retry.max_attempts
                        and (e.retryable or e.category in step.retry.retry_on)
                    ):
                        e = StepError(
                            f"{e} (not retried: step has side effects)",
                            e.category,
                            outcome=e.outcome,
                            data=e.data,
                        )
                    att.state = final
                    if e.data is not None:
                        rec.output = e.data
                    self._move(run, rec, final, str(e), e.category)
                    await self.host.publish(
                        "step.failed",
                        task,
                        run,
                        step.id,
                        state=final.value,
                        error=str(e)[:500],
                        category=e.category,
                    )
                    return
                rec.output, rec.artifact_ids, rec.executor_ref = (
                    out.data,
                    out.artifact_ids,
                    out.executor_ref,
                )
                rec.attempts[-1].finished_at = now()
                rec.attempts[-1].state = StepState.succeeded
                rec.error = rec.error_category = None
                self._move(run, rec, StepState.succeeded)
                for art in out.artifact_ids:
                    await self.host.publish(
                        "artifact.produced", task, run, step.id, artifact_id=art
                    )
                await self.host.publish(
                    "step.completed",
                    task,
                    run,
                    step.id,
                    duration_s=rec.duration_s,
                    executor=out.executor_ref,
                )
                return
        except asyncio.CancelledError:
            if rec.state not in STEP_TERMINAL:
                # A side-effecting operation that was in flight may still complete.
                state = (
                    StepState.unknown
                    if rec.state == StepState.running and not idempotent
                    else StepState.cancelled
                )
                if rec.attempts and rec.attempts[-1].finished_at is None:
                    rec.attempts[-1].finished_at, rec.attempts[-1].state = now(), state
                self._move(run, rec, state, "cancelled", "cancelled")
            raise

    def _classify(self, e: StepError, idempotent: bool) -> StepState:
        if e.outcome == "cancelled":
            return StepState.cancelled
        if e.outcome == "unknown":
            return StepState.unknown
        if e.outcome == "timed_out":
            return StepState.timed_out if idempotent else StepState.unknown
        return StepState.failed

    def _validate_output(self, task: Task, step: StepDef, out: StepOutput) -> None:
        if step.output_schema is not None:
            try:
                jsonschema.validate(out.data, step.output_schema)
            except jsonschema.ValidationError as e:
                raise StepError(f"output contract violated: {e.message}", "invalid_output") from e
        for art in out.artifact_ids:
            if not self.host.artifact_ok(task, art):
                raise StepError(f"artifact {art} missing or not owned by task", "missing_artifact")

    async def _finish(self, task, run: WorkflowRun, steps, recs, cancelled: bool) -> None:
        wf = run.workflow
        states = {r.step_id: r.state for r in recs.values()}
        failed = [s for s, st in states.items() if st in STEP_BAD]
        gate_fail = [
            s.id
            for s in wf.steps
            if s.executor == "validator"
            and s.mandatory_gate
            and states[s.id] != StepState.succeeded
        ]
        criteria_ok, run.gate_results = self._criteria(task, run, recs)
        denied = any(r.error_category in ("approval_denied", "expired") for r in recs.values())
        if any(st == StepState.unknown for st in states.values()):
            run.state = "unknown"
        elif cancelled or denied:
            run.state = "cancelled"
        elif not failed and not gate_fail and criteria_ok:
            run.state = "succeeded"
        elif wf.allow_partial and any(st == StepState.succeeded for st in states.values()):
            run.state = "partial"
        else:
            run.state = "failed"
        if run.state in ("failed", "unknown", "cancelled", "partial"):
            await self._compensate(task, run, steps, recs)
        outputs = {}
        for name, ref in wf.outputs.items():
            try:
                outputs[name] = self.resolve(ref, task, run, recs)
            except StepError:
                outputs[name] = None
        run.outputs = outputs
        if run.state == "failed" and not run.error:
            bad = failed or gate_fail
            run.error = (
                f"steps not successful: {', '.join(bad)}" if bad else "success criteria not met"
            )
        run.finished_at = now()
        self._save_run(run)
        await self.host.publish("workflow.completed", task, run, state=run.state, error=run.error)

    def _criteria(self, task, run, recs) -> tuple[bool, list[dict]]:
        results, ok = [], True
        for c in run.workflow.success_criteria:
            rec = recs[c.step]
            if c.kind in ("step_succeeded", "gate_passed"):
                passed = rec.state == StepState.succeeded
            elif c.kind == "output_equals":
                passed = (
                    rec.state == StepState.succeeded
                    and lookup(rec.output, [p for p in c.path.split(".") if p]) == c.value
                )
            else:  # artifact_exists
                passed = rec.state == StepState.succeeded and any(
                    self.host.artifact_ok(task, a) for a in rec.artifact_ids
                )
            ok &= passed
            results.append({"criterion": c.model_dump(), "passed": passed})
        return ok, results

    async def _compensate(self, task, run: WorkflowRun, steps, recs) -> None:
        comps = {c.id: c for c in run.workflow.compensations}
        done = sorted(
            (
                r
                for r in recs.values()
                if r.state == StepState.succeeded and steps[r.step_id].compensate_with
            ),
            key=lambda r: r.finished_at,
            reverse=True,
        )
        for rec in done:
            comp = comps[steps[rec.step_id].compensate_with]
            entry = {"for_step": rec.step_id, "compensation": comp.id}
            try:
                inputs = self.resolve(comp.inputs, task, run, recs)
                out = await asyncio.wait_for(
                    self.executors[comp.executor](task, run, comp, inputs), comp.timeout_s
                )
                entry.update(status="succeeded", output=out.data)
            except Exception as e:  # record, never hide
                entry.update(status="failed", error=f"{type(e).__name__}: {e}"[:300])
            run.compensation.append(entry)
            await self.host.publish("compensation.executed", task, run, rec.step_id, **entry)
