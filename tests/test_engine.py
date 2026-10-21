"""Workflow engine semantics with a minimal in-memory host and deterministic executors."""

import asyncio
import time

import pytest

from app.engine import StepError, StepOutput, WorkflowEngine
from app.models import Task
from app.store import Store
from app.workflow import (
    Condition,
    Criterion,
    InvalidWorkflow,
    RetryPolicy,
    StepDef,
    StepRecord,
    StepState,
    WorkflowDef,
    WorkflowRun,
    validate_structure,
)


class Host:
    def __init__(self, approve=True, side_effect_refs=()):
        self.store = Store(":memory:")
        self.events = []
        self.approve = approve
        self.side_effect_refs = set(side_effect_refs)
        self.approvals = 0

    def is_idempotent(self, step):
        return step.ref not in self.side_effect_refs and step.idempotent is not False

    def needs_approval(self, step):
        return step.requires_approval or step.executor == "approval"

    def artifact_ok(self, task, artifact_id):
        return artifact_id.startswith("art_ok")

    async def request_approval(self, task, run, step, rec):
        self.approvals += 1
        rec.approved_by = "tester"
        return self.approve

    async def publish(self, type_, task, run, step_id=None, **data):
        self.events.append((type_, step_id, data))


class Ops:
    """Deterministic internal executors keyed by step.ref."""

    def __init__(self):
        self.calls: dict[str, int] = {}
        self.spans: dict[str, tuple[float, float]] = {}
        self.inflight = 0
        self.max_inflight = 0

    async def run(self, task, run, step, inputs):
        self.calls[step.ref] = self.calls.get(step.ref, 0) + 1
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        start = time.monotonic()
        try:
            kind = step.ref.split(":")[0]
            if kind == "sleep":
                await asyncio.sleep(float(step.ref.split(":")[1]))
                return StepOutput({"slept": True, **inputs})
            if kind == "double":
                return StepOutput({"value": inputs["x"] * 2})
            if kind == "flaky":  # fails until the Nth call
                if self.calls[step.ref] < int(step.ref.split(":")[1]):
                    raise StepError("transient failure", "transient", retryable=True)
                return StepOutput({"ok": True})
            if kind == "fail":
                raise StepError("boom", "transient", retryable=True)
            if kind == "badout":
                return StepOutput({"nope": 1})
            if kind == "artifact":
                return StepOutput({"ok": 1}, [step.ref.split(":")[1]])
            if kind == "crash":
                raise RuntimeError("bug")
            return StepOutput(inputs)
        finally:
            self.inflight -= 1
            self.spans[step.id] = (start, time.monotonic())


def make(steps, **kw):
    return WorkflowDef(id="t", steps=steps, **kw)


async def execute(wf, host=None, ops=None, inputs=None, recs=None):
    host = host or Host()
    ops = ops or Ops()
    engine = WorkflowEngine(
        host, {k: ops.run for k in ("tool", "plugin", "internal", "agent", "validator", "approval")}
    )
    task = Task(title="t", description="objective")
    run = WorkflowRun(task_id=task.task_id, workflow=wf, inputs=inputs or {})
    await engine.execute(task, run, recs)
    states = {
        r.step_id: r
        for r in (StepRecord.model_validate(b) for b in host.store.list("step", run_id=run.run_id))
    }
    return run, states, host, ops


def S(id, ref="echo", **kw):
    return StepDef(id=id, executor="internal", ref=ref, **kw)


# ---------- validation ----------


def test_graph_validation_errors():
    assert "dependency cycle: a -> b -> a" in "; ".join(
        validate_structure(make([S("a", depends_on=["b"]), S("b", depends_on=["a"])]))
    ) or any(
        "cycle" in e
        for e in validate_structure(make([S("a", depends_on=["b"]), S("b", depends_on=["a"])]))
    )
    errs = validate_structure(
        make([S("a", depends_on=["zzz"]), S("b", inputs={"x": "$steps.a.output.v"})])
    )
    assert any("unknown dependency 'zzz'" in e for e in errs)
    assert any("not a dependency" in e for e in errs)
    errs = validate_structure(
        make([S("a", inputs={"x": "$bogus.q"}), S("b", compensate_with="nope")])
    )
    assert any("invalid mapping" in e for e in errs) and any(
        "unknown compensation" in e for e in errs
    )
    with pytest.raises(ValueError):
        S("Bad-Id")
    assert validate_structure(make([])) == ["workflow has no steps"]


# ---------- data flow and ordering ----------


async def test_sequential_mapping_and_outputs():
    wf = make(
        [
            S("one", "double", inputs={"x": "$input.n"}),
            S("two", "double", depends_on=["one"], inputs={"x": "$steps.one.output.value"}),
        ],
        outputs={"final": "$steps.two.output.value"},
    )
    run, st, host, _ = await execute(wf, inputs={"n": 3})
    assert run.state == "succeeded" and run.outputs == {"final": 12}
    order = [e[1] for e in host.events if e[0] == "step.started"]
    assert order == ["one", "two"]
    types = [e[0] for e in host.events]
    assert types[0] == "workflow.started" and types[-1] == "workflow.completed"


async def test_parallel_overlap_and_blocked_dependent():
    wf = make([S("a", "sleep:0.2"), S("b", "sleep:0.2"), S("agg", "echo", depends_on=["a", "b"])])
    run, st, _, ops = await execute(wf)
    a, b, agg = ops.spans["a"], ops.spans["b"], ops.spans["agg"]
    assert a[0] < b[1] and b[0] < a[1]  # independent steps overlapped
    assert agg[0] >= max(a[1], b[1])  # dependent waited for both
    assert run.state == "succeeded"


async def test_concurrency_limit():
    wf = make([S(f"s{i}", "sleep:0.05") for i in range(6)], max_concurrency=2)
    _, _, _, ops = await execute(wf)
    assert ops.max_inflight == 2


async def test_condition_branching():
    wf = make(
        [
            S("check", "double", inputs={"x": 1}),
            S(
                "yes",
                depends_on=["check"],
                condition=Condition(ref="$steps.check.output.value", op="eq", value=2),
            ),
            S(
                "no",
                depends_on=["check"],
                condition=Condition(ref="$steps.check.output.value", op="eq", value=3),
            ),
            S("after_no", depends_on=["no"]),
        ]
    )
    run, st, _, _ = await execute(wf)
    assert st["yes"].state == StepState.succeeded
    assert st["no"].state == StepState.skipped and st["after_no"].state == StepState.skipped
    assert run.state == "succeeded"


# ---------- contracts ----------


async def test_output_contract_and_artifact_check():
    wf = make([S("x", "badout", output_schema={"type": "object", "required": ["value"]})])
    run, st, _, _ = await execute(wf)
    assert st["x"].state == StepState.failed and st["x"].error_category == "invalid_output"
    run, st, _, _ = await execute(make([S("y", "artifact:art_missing")]))
    assert st["y"].error_category == "missing_artifact" and run.state == "failed"
    run, st, _, _ = await execute(
        make(
            [S("z", "artifact:art_ok1")],
            success_criteria=[Criterion(kind="artifact_exists", step="z")],
        )
    )
    assert run.state == "succeeded"


# ---------- retry / failure policy ----------


async def test_retry_bounded_with_backoff_history():
    wf = make([S("f", "flaky:3", retry=RetryPolicy(max_attempts=3, delay_s=0.01))])
    run, st, host, ops = await execute(wf)
    assert run.state == "succeeded" and st["f"].attempt == 3 and ops.calls["flaky:3"] == 3
    assert [a.state for a in st["f"].attempts] == [
        StepState.retry_waiting,
        StepState.retry_waiting,
        StepState.succeeded,
    ]
    assert sum(e[0] == "step.retry_scheduled" for e in host.events) == 2
    run, st, _, ops = await execute(make([S("f", "fail", retry=RetryPolicy(max_attempts=2))]))
    assert st["f"].state == StepState.failed and ops.calls["fail"] == 2


async def test_non_idempotent_never_blindly_retried():
    host = Host(side_effect_refs={"fail"})
    run, st, _, ops = await execute(
        make([S("pay", "fail", retry=RetryPolicy(max_attempts=5))]), host=host
    )
    assert ops.calls["fail"] == 1  # failure injection: executed exactly once
    assert "not retried: step has side effects" in st["pay"].error


async def test_fail_fast_vs_continue_and_partial():
    steps = [S("bad", "fail"), S("slow", "sleep:0.3"), S("dep", depends_on=["bad"])]
    run, st, _, _ = await execute(make(steps))
    assert run.state == "failed" and st["slow"].state == StepState.cancelled
    run, st, _, _ = await execute(
        make(
            [S("bad", "fail", on_failure="continue"), S("ok"), S("dep", depends_on=["bad"])],
            failure_policy="continue",
            allow_partial=True,
        )
    )
    assert st["ok"].state == StepState.succeeded and st["dep"].state == StepState.skipped
    assert run.state == "partial"


async def test_timeouts_idempotent_vs_side_effect():
    run, st, _, _ = await execute(make([S("t", "sleep:1", timeout_s=0.05)]))
    assert st["t"].state == StepState.timed_out
    run, st, _, _ = await execute(
        make([S("t", "sleep:1", timeout_s=0.05)]), host=Host(side_effect_refs={"sleep:1"})
    )
    assert st["t"].state == StepState.unknown and run.state == "unknown"


async def test_executor_crash_is_recorded():
    run, st, _, _ = await execute(make([S("c", "crash")]))
    assert st["c"].state == StepState.failed and st["c"].error_category == "crash"
    run, st, _, _ = await execute(make([S("c", "crash")]), host=Host(side_effect_refs={"crash"}))
    assert st["c"].state == StepState.unknown


async def test_compensation_runs_and_is_recorded():
    wf = make(
        [S("reserve", compensate_with="release"), S("charge", "fail", depends_on=["reserve"])],
        compensations=[S("release", "echo", inputs={"undo": "$steps.reserve.state"})],
    )
    run, _, host, _ = await execute(wf)
    assert run.state == "failed"
    assert run.compensation == [
        {
            "for_step": "reserve",
            "compensation": "release",
            "status": "succeeded",
            "output": {"undo": "succeeded"},
        }
    ]


# ---------- cancellation / approvals ----------


async def test_cancellation_propagates_and_side_effects_unknown():
    host = Host(side_effect_refs={"sleep:5"})
    ops = Ops()
    engine = WorkflowEngine(host, {"internal": ops.run})
    task = Task(title="t", description="d")
    wf = make([S("idem", "sleep:4"), S("write", "sleep:5"), S("later", depends_on=["idem"])])
    run = WorkflowRun(task_id=task.task_id, workflow=wf)
    t = asyncio.create_task(engine.execute(task, run))
    await asyncio.sleep(0.1)
    t.cancel()
    await asyncio.wait_for(t, 5)
    st = {
        r.step_id: r.state
        for r in (StepRecord.model_validate(b) for b in host.store.list("step", run_id=run.run_id))
    }
    assert st == {
        "idem": StepState.cancelled,
        "write": StepState.unknown,
        "later": StepState.cancelled,
    }
    assert run.state == "unknown"  # an in-flight side effect may still have completed


async def test_approval_checkpoint_granted_and_denied():
    wf = make(
        [
            S("prep"),
            StepDef(id="gate", executor="approval", depends_on=["prep"]),
            S("act", depends_on=["gate"]),
        ]
    )
    run, st, host, _ = await execute(wf)
    assert run.state == "succeeded" and st["gate"].approved_by == "tester" and host.approvals == 1
    assert [e[0] for e in host.events if e[0].startswith("approval")] == [
        "approval.requested",
        "approval.granted",
    ]
    run, st, _, _ = await execute(wf, host=Host(approve=False))
    assert st["gate"].state == StepState.cancelled and st["act"].state == StepState.cancelled
    assert run.state == "cancelled"


# ---------- recovery ----------


async def test_recovery_resumes_idempotent_and_marks_side_effects_unknown():
    host, ops = Host(side_effect_refs={"write"}), Ops()
    engine = WorkflowEngine(host, {"internal": ops.run})
    task = Task(title="t", description="d")
    wf = make(
        [
            S("done"),
            S("idem", depends_on=["done"]),
            S("write", "write", depends_on=["done"]),
            S("after", depends_on=["idem"]),
        ]
    )
    run = WorkflowRun(task_id=task.task_id, workflow=wf)
    # Simulated crash state: 'done' finished, two steps were in flight.
    recs = {s.id: StepRecord(run_id=run.run_id, step_id=s.id) for s in wf.steps}
    recs["done"].state, recs["done"].output = StepState.succeeded, {"v": 1}
    recs["idem"].state = recs["write"].state = StepState.running
    notes = engine.prepare_recovery(run, recs)
    assert any("will re-run" in n for n in notes) and any("not replayed" in n for n in notes)
    await engine.execute(task, run, recs)
    assert "echo" in ops.calls and "write" not in ops.calls  # side effect never replayed
    assert recs["done"].attempt == 0  # completed step not re-executed
    assert recs["idem"].state == StepState.succeeded and recs["write"].state == StepState.unknown
    assert run.state == "unknown"


def test_invalid_workflow_error_type():
    e = InvalidWorkflow(["a", "b"])
    assert e.errors == ["a", "b"] and "a; b" in str(e)
