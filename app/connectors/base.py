"""Connector contracts. Model providers, coding runtimes and image providers are
separate protocols so each keeps its native semantics."""

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol

from app.models import Attachment

Verification = Literal["verified", "partially_verified", "mocked", "unavailable"]


class Cap(StrEnum):
    text = "text"
    image_input = "image_input"
    file_input = "file_input"
    image_generation = "image_generation"
    structured_output = "structured_output"
    tool_calling = "tool_calling"
    workspace_read = "workspace_read"
    workspace_write = "workspace_write"
    terminal = "terminal"
    session_resume = "session_resume"
    cancel = "cancel"


class ConnectorError(Exception):
    def __init__(
        self,
        message: str,
        category: str = "provider",
        retryable: bool = False,
        outcome: Literal["failed", "unknown"] = "failed",
    ):
        super().__init__(message)
        self.category = category  # auth|rate_limit|timeout|unsupported|provider|cancelled
        self.retryable = retryable
        self.outcome = outcome


class UnsupportedCapability(ConnectorError):
    def __init__(self, message: str):
        super().__init__(message, category="unsupported")


@dataclass
class AgentRequest:
    task_id: str
    prompt: str
    system: str | None = None
    attachments: list[tuple[Attachment, bytes]] = field(default_factory=list)
    workspace: Path | None = None
    read_only: bool = True
    resume_session: str | None = None
    json_schema: dict[str, Any] | None = None
    tools: list[dict[str, Any]] | None = None  # provider-native tool definitions


@dataclass
class AgentResult:
    text: str
    provider: str
    model: str | None
    session_id: str | None = None
    structured: Any = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


class AgentConnector(Protocol):
    name: str
    caps: frozenset[Cap]
    verification: Verification
    available: bool

    async def execute(self, request: AgentRequest) -> AgentResult: ...

    async def cancel(self, task_id: str) -> bool:
        """True only if an in-flight run was actually signalled to stop."""
        ...


class ImageGenerationProvider(Protocol):
    name: str
    model: str
    verification: Verification

    async def generate(self, prompt: str, size: str, transparent: bool) -> bytes: ...
