"""Central capability registry: one manifest per agent, provider, tool, MCP server and plugin.

Status ladder (only validated/operational/degraded are routable):
declared -> discovered -> configured -> validated -> operational
                                     \\-> degraded (recent real failure)  -> unavailable
"operational" requires evidence from a real successful execution in this process
(or a free real health check for HTTP providers), never just configuration.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel

from app.models import now


class CapStatus(StrEnum):
    declared = "declared"
    discovered = "discovered"
    configured = "configured"
    validated = "validated"
    operational = "operational"
    degraded = "degraded"
    unavailable = "unavailable"


ROUTABLE = {CapStatus.validated, CapStatus.operational, CapStatus.degraded}


class CapabilityManifest(BaseModel):
    id: str  # stable, e.g. "tool:data.transform", "agent:claude_code", "plugin:data"
    kind: Literal[
        "agent",
        "runtime",
        "provider",
        "capability",
        "tool",
        "mcp_server",
        "plugin",
        "image_provider",
    ]
    name: str
    description: str = ""
    category: str = ""
    operations: list[str] = []
    provides: list[str] = []  # abstract capabilities, e.g. "model.text", "workspace.write"
    input_schema: dict | None = None
    output_schema: dict | None = None
    backend: str = "local"
    artifact_types: list[str] = []
    permissions: list[str] = []
    side_effects: bool = False
    timeout_s: float | None = None
    config_requirements: list[str] = []
    version: str | None = None
    status: CapStatus = CapStatus.declared
    evidence: str = ""
    checked_at: datetime | None = None
    last_error: str | None = None


HealthFn = Callable[[], Awaitable[tuple[CapStatus, str]]]


class CapabilityRegistry:
    def __init__(self):
        self._items: dict[str, CapabilityManifest] = {}
        self._health: dict[str, HealthFn] = {}

    def register(self, m: CapabilityManifest, health: HealthFn | None = None) -> CapabilityManifest:
        if not m.id or ":" not in m.id:
            raise ValueError(f"capability id must be '<kind>:<name>': {m.id!r}")
        self._items[m.id] = m
        if health:
            self._health[m.id] = health
        return m

    def get(self, cap_id: str) -> CapabilityManifest | None:
        return self._items.get(cap_id)

    def list(self, kind: str | None = None) -> list[CapabilityManifest]:
        return sorted(
            (m for m in self._items.values() if kind is None or m.kind == kind), key=lambda m: m.id
        )

    def set_status(self, cap_id: str, status: CapStatus, evidence: str) -> None:
        m = self._items.get(cap_id)
        if m is None:
            return
        m.status, m.evidence, m.checked_at = status, evidence[:500], now()
        if status in (CapStatus.degraded, CapStatus.unavailable):
            m.last_error = evidence[:500]

    def routable(self, cap_id: str) -> bool:
        m = self._items.get(cap_id)
        return m is not None and m.status in ROUTABLE

    def providers_of(self, capability: str) -> list[CapabilityManifest]:
        """Routable manifests providing an abstract capability, operational first."""
        order = {CapStatus.operational: 0, CapStatus.validated: 1, CapStatus.degraded: 2}
        found = [
            m for m in self._items.values() if capability in m.provides and m.status in ROUTABLE
        ]
        return sorted(found, key=lambda m: (order[m.status], m.id))

    def record_execution(self, cap_id: str, ok: bool, detail: str) -> None:
        """Real execution evidence moves a capability to operational or degraded."""
        m = self._items.get(cap_id)
        if m is None:
            return
        if ok:
            self.set_status(cap_id, CapStatus.operational, f"real execution succeeded: {detail}")
        elif m.status in ROUTABLE:
            self.set_status(cap_id, CapStatus.degraded, f"real execution failed: {detail}")

    async def check_health(self) -> dict[str, str]:
        results = {}
        for cap_id, fn in self._health.items():
            try:
                status, evidence = await fn()
            except Exception as e:  # a broken health check never takes the gateway down
                status, evidence = CapStatus.unavailable, f"health check error: {type(e).__name__}"
            m = self._items[cap_id]
            # Never downgrade real operational evidence to "validated" from a static check.
            if not (m.status == CapStatus.operational and status == CapStatus.validated):
                self.set_status(cap_id, status, evidence)
            results[cap_id] = m.status
        return results

    def summary(self) -> list[dict[str, Any]]:
        return [
            m.model_dump(mode="json", exclude={"input_schema", "output_schema"})
            for m in self.list()
        ]
