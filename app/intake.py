"""Normalized, domain-independent task request used by every interface (Telegram, REST, CLI)."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.models import Workflow

# Legacy Phase 1 workflow names map onto plugin task types.
LEGACY_WORKFLOWS: dict[Workflow, tuple[str | None, str | None]] = {
    Workflow.ask: ("general", "answer"),
    Workflow.code: ("software", "implement"),
    Workflow.delegate_review: ("software", "implement_reviewed"),
    Workflow.parallel: ("software", "parallel_implement"),
    Workflow.design_implement_review: ("unity", "feature"),
}


class TaskRequest(BaseModel):
    objective: str = Field(min_length=1, max_length=20_000)
    user_id: str = ""
    tenant_id: str | None = None
    conversation_id: str | None = None
    domain: str | None = None  # explicit plugin id, or None for classification
    task_type: str | None = None
    attachment_ids: list[str] = Field(default=[], max_length=20)
    artifact_ids: list[str] = Field(default=[], max_length=20)  # input artifact references
    data: dict[str, Any] = {}  # structured input (validated by the plugin's input schema)
    constraints: list[str] = Field(default=[], max_length=50)
    preferences: dict[str, str] = {}
    output_format: str | None = None
    mode: Literal["auto", "direct_tool", "single_agent", "workflow"] = "auto"
    tool: str | None = None  # direct tool execution
    tool_args: dict[str, Any] = {}
    workflow: dict[str, Any] | None = None  # explicit WorkflowDef; still validated against policy
    workflow_id: str | None = None  # a configured workflow (config/workflows) chosen explicitly
    agent: str | None = None  # explicit connector preference
    timeout_s: int | None = Field(None, gt=0, le=86_400)
    deadline: datetime | None = None
    require_approval: bool = False  # user wants every side effect approved
    idempotency_key: str | None = Field(None, max_length=200)
    source: str = "api"  # telegram | api | mcp | cli
    parent_task_id: str | None = None
    correlation_id: str | None = None

    @field_validator("objective")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("objective is empty")
        return v

    @classmethod
    def from_legacy(
        cls,
        description: str,
        workflow: Workflow,
        user_id: str = "",
        attachment_ids=(),
        agent: str | None = None,
    ) -> "TaskRequest":
        domain, task_type = LEGACY_WORKFLOWS[Workflow(workflow)]
        return cls(
            objective=description,
            user_id=user_id,
            domain=domain,
            task_type=task_type,
            attachment_ids=list(attachment_ids),
            agent=agent,
        )
