"""Normalized domain models. All state is in-memory for the MVP (lost on restart)."""

import secrets
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


def now() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(5)}"


class TaskState(StrEnum):
    created = "created"
    queued = "queued"
    planning = "planning"
    running = "running"
    waiting_for_user = "waiting_for_user"
    waiting_for_approval = "waiting_for_approval"
    validating = "validating"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"
    unknown = "unknown"  # outcome could not be established (e.g. runtime killed mid-edit)
    expired = "expired"  # an approval or human answer was not given in time


TERMINAL = {
    TaskState.succeeded,
    TaskState.failed,
    TaskState.cancelled,
    TaskState.unknown,
    TaskState.expired,
}
_WAITING = {TaskState.waiting_for_user, TaskState.waiting_for_approval}
TRANSITIONS: dict[TaskState, set[TaskState]] = {
    TaskState.created: {TaskState.queued, TaskState.waiting_for_approval, TaskState.cancelled},
    TaskState.queued: {
        TaskState.planning,
        TaskState.running,
        TaskState.cancelled,
        TaskState.failed,
    },
    TaskState.planning: _WAITING | {TaskState.running} | TERMINAL,
    TaskState.running: _WAITING | {TaskState.validating} | TERMINAL,
    TaskState.waiting_for_user: {TaskState.planning, TaskState.running} | TERMINAL,
    TaskState.waiting_for_approval: {TaskState.running} | TERMINAL,
    TaskState.validating: {TaskState.running} | TERMINAL,
    **{s: set() for s in TERMINAL},
}


class InvalidTransition(Exception):
    pass


class Workflow(StrEnum):
    ask = "ask"  # single model call, text answer
    code = "code"  # Agent A implements in isolated worktree + QA
    delegate_review = "delegate_review"  # code + independent review + fix loop
    parallel = "parallel"  # two implementers, separate worktrees, compared
    design_implement_review = "design_implement_review"  # Agent B -> A -> QA -> C


class Attachment(BaseModel):
    attachment_id: str = Field(default_factory=lambda: new_id("att"))
    filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    storage_path: Path
    source: str  # telegram | api | generated
    created_at: datetime = Field(default_factory=now)
    task_id: str | None = None
    message_id: str | None = None

    @property
    def is_image(self) -> bool:
        return self.mime_type.startswith("image/")


class Message(BaseModel):
    message_id: str = Field(default_factory=lambda: new_id("msg"))
    task_id: str | None = None
    source: str
    target: str | None = None
    content: str = ""
    content_format: Literal["text", "markdown", "html", "json"] = "text"
    attachments: list[str] = []
    actions: list[str] = []
    created_at: datetime = Field(default_factory=now)
    correlation_id: str = Field(default_factory=lambda: new_id("cor"))


class Artifact(BaseModel):
    artifact_id: str = Field(default_factory=lambda: new_id("art"))
    task_id: str
    kind: str  # report | diff | log | prompt | image | manifest | design_spec | review
    filename: str
    path: Path
    sha256: str
    size_bytes: int
    created_at: datetime = Field(default_factory=now)
    mime_type: str | None = None
    step_id: str | None = None  # producing workflow step
    producer: str | None = None  # agent/tool/provider that produced it
    validation_status: Literal["unchecked", "passed", "failed"] = "unchecked"
    meta: dict[str, Any] = {}


class ReviewFinding(BaseModel):
    severity: Literal["critical", "high", "medium", "low", "info"]
    description: str
    location: str = ""  # file:line or artifact id
    reproduction: str = ""
    evidence: str = ""
    remediation: str = ""
    verification_status: Literal["verified", "unverified"] = "unverified"


class UsageRecord(BaseModel):
    task_id: str
    provider: str
    model: str | None
    started_at: datetime
    ended_at: datetime
    status: str
    error_category: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None  # provider-reported estimate only
    retry_count: int = 0

    @property
    def duration_ms(self) -> int:
        return int((self.ended_at - self.started_at).total_seconds() * 1000)


class AgentSession(BaseModel):
    session_id: str = Field(default_factory=lambda: new_id("ses"))
    connector: str
    native_session_id: str | None = None  # e.g. Claude Code session UUID
    task_id: str
    role: str
    workspace: Path | None = None
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class Task(BaseModel):
    task_id: str = Field(default_factory=lambda: new_id("tsk"))
    title: str
    description: str
    workflow: Workflow = Workflow.ask  # legacy alias; domain/task_type drive planning
    domain: str | None = None
    task_type: str | None = None
    request: dict[str, Any] = {}  # normalized TaskRequest (app.intake)
    run_id: str | None = None  # workflow run executing this task
    question: str | None = None  # pending clarification question
    role: str | None = None  # implementer | designer | reviewer for child steps
    agent: str | None = None  # explicit connector choice
    state: TaskState = TaskState.created
    parent_id: str | None = None
    depends_on: list[str] = []
    correlation_id: str = Field(default_factory=lambda: new_id("cor"))
    attempt: int = 1
    created_by: str = ""
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)
    attachment_ids: list[str] = []
    artifact_ids: list[str] = []
    session_ids: list[str] = []
    findings: list[ReviewFinding] = []
    usage: list[UsageRecord] = []
    provider: str | None = None
    model: str | None = None
    result: str | None = None
    error: str | None = None
    error_category: str | None = None
    delivery_failures: int = 0
    history: list[tuple[datetime, TaskState]] = []

    def transition(self, new: TaskState) -> None:
        if new == self.state:
            return
        if new not in TRANSITIONS[self.state]:
            raise InvalidTransition(f"{self.task_id}: {self.state} -> {new}")
        self.state = new
        self.updated_at = now()
        self.history.append((self.updated_at, new))

    @property
    def done(self) -> bool:
        return self.state in TERMINAL


class AgentProfile(BaseModel):
    role: str
    label: str
    description: str
    preference: list[str]
