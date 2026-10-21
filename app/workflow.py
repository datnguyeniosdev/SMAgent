"""Workflow definitions, graph validation, step state machine and shared result contracts."""

import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.models import new_id, now

_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_REF = re.compile(r"^\$(input|task|steps|context)(\.[A-Za-z0-9_\-]+)*$")
Executor = Literal["tool", "agent", "plugin", "validator", "internal", "approval", "capability"]


class StepState(StrEnum):
    pending = "pending"
    ready = "ready"
    running = "running"
    waiting_for_approval = "waiting_for_approval"
    retry_waiting = "retry_waiting"
    succeeded = "succeeded"
    failed = "failed"
    skipped = "skipped"
    cancelled = "cancelled"
    timed_out = "timed_out"
    unknown = "unknown"


STEP_TERMINAL = {
    StepState.succeeded,
    StepState.failed,
    StepState.skipped,
    StepState.cancelled,
    StepState.timed_out,
    StepState.unknown,
}
STEP_BAD = {StepState.failed, StepState.cancelled, StepState.timed_out, StepState.unknown}
STEP_TRANSITIONS: dict[StepState, set[StepState]] = {
    StepState.pending: {StepState.ready, StepState.skipped, StepState.cancelled},
    StepState.ready: {
        StepState.running,
        StepState.waiting_for_approval,
        StepState.skipped,
        StepState.cancelled,
    },
    StepState.waiting_for_approval: {StepState.running, StepState.cancelled, StepState.ready},
    StepState.running: {
        StepState.succeeded,
        StepState.failed,
        StepState.timed_out,
        StepState.unknown,
        StepState.cancelled,
        StepState.retry_waiting,
        StepState.ready,
    },  # running -> ready only via recovery of idempotent steps
    StepState.retry_waiting: {StepState.running, StepState.cancelled, StepState.ready},
    **{s: set() for s in STEP_TERMINAL},
}


class InvalidWorkflow(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


class RetryPolicy(BaseModel):
    max_attempts: int = Field(1, ge=1, le=10)
    delay_s: float = Field(0.0, ge=0, le=300)
    backoff: float = Field(2.0, ge=1, le=10)
    max_elapsed_s: float | None = None
    retry_on: list[str] = ["timeout", "rate_limit", "network", "transient", "provider"]


class Condition(BaseModel):
    ref: str  # e.g. "$steps.check.output.ok"
    op: Literal["eq", "ne", "truthy", "falsy", "in", "gt", "lt"] = "truthy"
    value: Any = None


class StepDef(BaseModel):
    id: str
    executor: Executor
    ref: str = ""  # tool name | agent role | plugin op | validator id | internal op | capability id
    agent: str | None = None  # capability steps: logical agent that must declare the capability
    title: str = ""
    depends_on: list[str] = []
    condition: Condition | None = None
    inputs: dict[str, Any] = {}
    output_schema: dict | None = None
    required_capabilities: list[str] = []
    permissions: list[str] = []
    timeout_s: float = Field(300, gt=0, le=7200)
    retry: RetryPolicy = RetryPolicy()
    idempotent: bool | None = None  # None: derived from the executor (tool side effects)
    requires_approval: bool = False
    on_failure: Literal["fail_fast", "continue"] = "fail_fast"
    compensate_with: str | None = None  # id of a step in WorkflowDef.compensations
    mandatory_gate: bool = True  # validator steps: a failure blocks success

    @field_validator("id")
    @classmethod
    def _id(cls, v):
        if not _ID.match(v):
            raise ValueError(f"invalid step id {v!r}")
        return v


class Criterion(BaseModel):
    kind: Literal["step_succeeded", "output_equals", "artifact_exists", "gate_passed"]
    step: str
    path: str = ""
    value: Any = None
    artifact_kind: str | None = None


class WorkflowDef(BaseModel):
    id: str
    version: str = "1"
    description: str = ""
    domain: str = "general"
    input_schema: dict | None = None
    output_schema: dict | None = None
    steps: list[StepDef]
    compensations: list[StepDef] = []
    success_criteria: list[Criterion] = []
    outputs: dict[str, str] = {}  # name -> mapping ref
    failure_policy: Literal["fail_fast", "continue"] = "fail_fast"
    allow_partial: bool = False
    final_report: bool = (
        True  # false: a successful run posts no end-of-workflow message (its own report suffices)
    )
    max_concurrency: int = Field(4, ge=1, le=16)
    timeout_s: float = Field(3600, gt=0)
    constraints: list[str] = []  # copied from the request; must not be dropped


def refs_in(value: Any) -> list[str]:
    if isinstance(value, str) and value.startswith("$"):
        return [value]
    if isinstance(value, dict):
        return [r for v in value.values() for r in refs_in(v)]
    if isinstance(value, list):
        return [r for v in value for r in refs_in(v)]
    return []


def ancestors(steps: dict[str, StepDef], sid: str) -> set[str]:
    seen, stack = set(), list(steps[sid].depends_on)
    while stack:
        d = stack.pop()
        if d in seen or d not in steps:
            continue
        seen.add(d)
        stack.extend(steps[d].depends_on)
    return seen


def find_cycle(steps: dict[str, StepDef]) -> list[str] | None:
    state: dict[str, int] = {}
    path: list[str] = []

    def visit(s: str) -> list[str] | None:
        state[s] = 1
        path.append(s)
        for d in steps[s].depends_on:
            if d not in steps:
                continue
            if state.get(d) == 1:
                return path[path.index(d) :] + [d]
            if d not in state and (c := visit(d)):
                return c
        path.pop()
        state[s] = 2
        return None

    for s in steps:
        if s not in state and (c := visit(s)):
            return c
    return None


def validate_structure(wf: WorkflowDef) -> list[str]:
    """Structural checks that need no runtime registry."""
    errors: list[str] = []
    steps = {s.id: s for s in wf.steps}
    if not wf.steps:
        errors.append("workflow has no steps")
    if len(steps) != len(wf.steps):
        errors.append("duplicate step ids")
    comp_ids = {c.id for c in wf.compensations}
    for s in wf.steps:
        for d in s.depends_on:
            if d not in steps:
                errors.append(f"step {s.id}: unknown dependency {d!r}")
            if d == s.id:
                errors.append(f"step {s.id}: depends on itself")
        if s.compensate_with and s.compensate_with not in comp_ids:
            errors.append(f"step {s.id}: unknown compensation {s.compensate_with!r}")
        if s.executor != "approval" and not s.ref:
            errors.append(f"step {s.id}: executor {s.executor} needs a ref")
    if (cycle := find_cycle(steps)) is not None:
        errors.append(f"dependency cycle: {' -> '.join(cycle)}")
        return errors
    for s in wf.steps:
        anc = ancestors(steps, s.id)
        refs = refs_in(s.inputs) + ([s.condition.ref] if s.condition else [])
        for r in refs:
            if not _REF.match(r):
                errors.append(f"step {s.id}: invalid mapping {r!r}")
                continue
            parts = r.split(".")
            if parts[0] == "$steps":
                if len(parts) < 3 or parts[2] not in ("output", "artifacts", "state"):
                    errors.append(
                        f"step {s.id}: mapping {r!r} must be $steps.<id>.output|artifacts|state"
                    )
                elif parts[1] not in anc:
                    errors.append(
                        f"step {s.id}: mapping {r!r} reads a step that is not a dependency"
                    )
    for name, r in wf.outputs.items():
        parts = r.split(".")
        if not _REF.match(r) or (
            parts[0] == "$steps" and (len(parts) < 2 or parts[1] not in steps)
        ):
            errors.append(f"output {name}: invalid mapping {r!r}")
    for c in wf.success_criteria:
        if c.step not in steps:
            errors.append(f"criterion {c.kind}: unknown step {c.step!r}")
    return errors


# ---------- runtime records ----------


class StepAttempt(BaseModel):
    attempt: int
    state: StepState
    started_at: datetime
    finished_at: datetime | None = None
    error: str | None = None
    error_category: str | None = None


class StepRecord(BaseModel):
    run_id: str
    step_id: str
    state: StepState = StepState.pending
    attempt: int = 0
    attempts: list[StepAttempt] = []
    executor_ref: str | None = None  # e.g. provider/tool actually used
    output: Any = None
    artifact_ids: list[str] = []
    error: str | None = None
    error_category: str | None = None
    approval_id: str | None = None
    approved_by: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    validation: list[dict] = []

    def transition(self, new: StepState) -> None:
        if new not in STEP_TRANSITIONS[self.state]:
            raise ValueError(f"step {self.step_id}: {self.state} -> {new}")
        self.state = new
        if new == StepState.running and self.started_at is None:
            self.started_at = now()
        if new in STEP_TERMINAL:
            self.finished_at = now()

    @property
    def duration_s(self) -> float | None:
        if self.started_at and self.finished_at:
            return round((self.finished_at - self.started_at).total_seconds(), 3)
        return None


class WorkflowRun(BaseModel):
    run_id: str = Field(default_factory=lambda: new_id("run"))
    task_id: str
    workflow: WorkflowDef
    inputs: dict[str, Any] = {}
    state: Literal[
        "running", "waiting", "succeeded", "failed", "cancelled", "unknown", "partial"
    ] = "running"
    created_at: datetime = Field(default_factory=now)
    finished_at: datetime | None = None
    outputs: dict[str, Any] = {}
    gate_results: list[dict] = []
    compensation: list[dict] = []
    error: str | None = None


# ---------- shared quality-gate and result contracts ----------


class GateResult(BaseModel):
    validator_id: str
    status: Literal["passed", "failed", "unknown", "skipped"]
    evidence: list[str] = []  # artifact ids, record ids, file paths (workspace-relative)
    findings: list[dict] = []  # {"severity": ..., "message": ...}
    metrics: dict[str, Any] = {}
    executed_at: datetime = Field(default_factory=now)
    error: str | None = None


class ResultEnvelope(BaseModel):
    task_id: str
    domain: str | None
    operation: str | None
    status: str
    summary: str
    artifacts: list[dict] = []
    validations: list[GateResult] = []
    evidence: list[dict] = []  # per step: executor, state, duration, attempts
    failed_steps: list[str] = []
    skipped_steps: list[str] = []
    limitations: list[str] = []
    next_actions: list[str] = []
    routing: dict[str, Any] = {}
