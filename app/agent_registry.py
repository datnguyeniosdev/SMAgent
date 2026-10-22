"""Logical agents, capabilities and executor selection (config-driven).

Agent      = a logical worker/role that declares capabilities (config/agents).
Capability = a typed ability with input/output schemas (config/capabilities).
Executor   = what actually performs it: a registered tool, a model provider, an agent
             runtime, a plugin operation or an internal op. A capability may have several;
             the router picks the first *eligible* one by priority (and agent preference).
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass

from app.capabilities import ROUTABLE, CapabilityManifest, CapStatus
from app.gateway_config import INTERNAL_OPS, AgentDef, CapabilityDef, ExecutorSpec, GatewayConfig


class CapabilityError(Exception):
    """Structured routing/execution error. `code` is stable for API callers."""

    CODES = {
        "unknown_agent",
        "agent_disabled",
        "unknown_capability",
        "capability_disabled",
        "capability_unavailable",
        "missing_credentials",
        "mcp_disconnected",
        "invalid_input",
        "invalid_output",
        "forbidden",
    }

    def __init__(self, code: str, message: str, details: list[str] | None = None):
        assert code in self.CODES, code
        super().__init__(message)
        self.code = code
        self.details = details or []

    def as_dict(self) -> dict:
        return {"error": self.code, "message": str(self), "details": self.details}


@dataclass
class ExecutorCheck:
    spec: ExecutorSpec
    ok: bool
    reason: str
    code: str | None = None

    @property
    def executor_id(self) -> str:
        return executor_id(self.spec)


def executor_id(spec: ExecutorSpec) -> str:
    suffix = f".{spec.operation}" if spec.operation and spec.type == "runtime" else ""
    model = f"@{spec.model}" if spec.model else ""
    return f"{spec.type}:{spec.ref}{model}{suffix}"


class AgentCapabilityRegistry:
    def __init__(self, cfg: GatewayConfig, svc):
        self.cfg = cfg
        self.svc = svc  # Orchestrator: tools, caps, router, plugins, settings

    @property
    def agents(self) -> dict[str, AgentDef]:
        return self.cfg.agents

    @property
    def capabilities(self) -> dict[str, CapabilityDef]:
        return self.cfg.capabilities

    def agents_for(self, capability_id: str) -> list[str]:
        return [a.id for a in self.agents.values() if capability_id in a.capabilities]

    # ---------- eligibility ----------

    def check(self, spec: ExecutorSpec) -> ExecutorCheck:
        svc, pol = self.svc, self.cfg.policies
        missing_env = [e for e in spec.requires_env if not os.environ.get(e)]
        if missing_env:
            return ExecutorCheck(
                spec, False, f"missing environment: {missing_env}", "missing_credentials"
            )
        granted = set(svc.s.tool_permissions)
        if not set(spec.permissions) <= granted:
            return ExecutorCheck(
                spec,
                False,
                f"permissions not granted: {sorted(set(spec.permissions) - granted)}",
                "forbidden",
            )
        if spec.type == "tool":
            if pol.tool_allowlist is not None and not any(
                fnmatch.fnmatch(spec.ref, p) for p in pol.tool_allowlist
            ):
                return ExecutorCheck(
                    spec, False, "tool not in policies.tool_allowlist", "forbidden"
                )
            if svc.tools.get(spec.ref) is None:
                code = (
                    "mcp_disconnected" if spec.ref.startswith("mcp.") else "capability_unavailable"
                )
                return ExecutorCheck(spec, False, f"tool {spec.ref} is not registered", code)
            if not svc.caps.routable(f"tool:{spec.ref}"):
                return ExecutorCheck(
                    spec, False, f"tool {spec.ref} is not available", "capability_unavailable"
                )
        elif spec.type == "provider":
            if pol.provider_allowlist is not None and spec.ref not in pol.provider_allowlist:
                return ExecutorCheck(
                    spec, False, "provider not in policies.provider_allowlist", "forbidden"
                )
            pdef = self.cfg.providers.get(spec.ref)
            if pdef is None or not pdef.enabled:
                return ExecutorCheck(
                    spec,
                    False,
                    f"provider {spec.ref} not configured/enabled",
                    "capability_unavailable",
                )
            if not os.environ.get(pdef.api_key_env):
                return ExecutorCheck(
                    spec, False, f"{pdef.api_key_env} is not set", "missing_credentials"
                )
            if not svc.router.on(spec.ref):
                return ExecutorCheck(
                    spec,
                    False,
                    f"provider {spec.ref} is switched off (/agents)",
                    "capability_unavailable",
                )
            m = svc.caps.get(f"provider:{spec.ref}")
            if spec.model is None and (m is None or m.status not in ROUTABLE):
                return ExecutorCheck(
                    spec,
                    False,
                    f"provider {spec.ref} is {m.status if m else 'unknown'}: "
                    f"{m.evidence if m else ''}",
                    "capability_unavailable",
                )
        elif spec.type == "runtime":
            if spec.operation == "return_to_caller":
                return ExecutorCheck(spec, True, "manual continuation is always possible")
            m = svc.caps.get(f"runtime:{spec.ref}")
            if m is None or m.status not in ROUTABLE:
                return ExecutorCheck(
                    spec,
                    False,
                    f"runtime {spec.ref} is {m.status if m else 'unknown'}: "
                    f"{m.evidence if m else ''}",
                    "capability_unavailable",
                )
        elif spec.type == "plugin":
            try:
                svc.plugins.operation(spec.ref)
            except Exception as e:
                return ExecutorCheck(spec, False, str(e), "capability_unavailable")
        elif spec.type == "internal" and spec.ref not in INTERNAL_OPS:
            return ExecutorCheck(
                spec, False, f"unknown internal op {spec.ref}", "capability_unavailable"
            )
        return ExecutorCheck(spec, True, "eligible")

    def candidates(
        self, capability_id: str, agent_id: str | None = None, plan: list[str] = ()
    ) -> list[ExecutorCheck]:
        """plan: the project's executors for this role (catalog.plan), tried before everything else."""
        cap = self._capability(capability_id)
        agent = self._agent(agent_id, capability_id) if agent_id else None
        pref = agent.executor_preference if agent else []

        def key(spec: ExecutorSpec):
            eid = f"{spec.type}:{spec.ref}"
            return (pref.index(eid) if eid in pref else len(pref), spec.priority)

        planned = [s for e in plan if (s := self._planned(cap, e)) is not None]
        rest = [
            s
            for s in sorted(cap.executors, key=key)
            if executor_id(s) not in {executor_id(p) for p in planned}
        ]
        return [self.check(s) for s in planned + rest]

    def _planned(self, cap: CapabilityDef, entry: str) -> ExecutorSpec | None:
        """A plan entry as an executor of this capability. A provider (any model) borrows the
        capability's own provider executor (operation, prompts); a runtime must already be one of
        its executors. None when the capability cannot use it."""
        kind, _, ref = entry.partition(":")
        pid, _, model = ref.partition("@")
        if kind == "provider":
            base = next((s for s in cap.executors if s.type == "provider"), None)
            return base.model_copy(update={"ref": pid, "model": model or None}) if base else None
        return next((s for s in cap.executors if s.type == kind and s.ref == pid), None)

    def select(
        self, capability_id: str, agent_id: str | None = None, exclude: set[str] = frozenset(),
        plan: list[str] = (),
    ) -> ExecutorCheck:  # fmt: skip
        checks = [
            c
            for c in self.candidates(capability_id, agent_id, plan)
            if c.executor_id not in exclude
        ]
        for c in checks:
            if c.ok:
                return c
        codes = {c.code for c in checks if c.code}
        code = next(
            (k for k in ("missing_credentials", "mcp_disconnected", "forbidden") if k in codes),
            "capability_unavailable",
        )
        raise CapabilityError(
            code,
            f"no eligible executor for capability {capability_id!r}",
            [f"{c.executor_id}: {c.reason}" for c in checks],
        )

    def _capability(self, capability_id: str) -> CapabilityDef:
        cap = self.capabilities.get(capability_id)
        if cap is None:
            raise CapabilityError("unknown_capability", f"unknown capability {capability_id!r}")
        if not cap.enabled:
            raise CapabilityError(
                "capability_disabled", f"capability {capability_id!r} is disabled"
            )
        return cap

    def _agent(self, agent_id: str, capability_id: str) -> AgentDef:
        agent = self.agents.get(agent_id)
        if agent is None:
            raise CapabilityError("unknown_agent", f"unknown agent {agent_id!r}")
        if not agent.enabled:
            raise CapabilityError("agent_disabled", f"agent {agent_id!r} is disabled")
        if capability_id not in agent.capabilities:
            raise CapabilityError(
                "unknown_capability", f"agent {agent_id!r} does not declare {capability_id!r}"
            )
        return agent

    # ---------- status in the central capability registry ----------

    def publish_status(self) -> None:
        caps = self.svc.caps
        for cap in self.capabilities.values():
            if not cap.enabled:
                status, evidence = CapStatus.unavailable, "disabled in configuration"
            else:
                checks = [self.check(s) for s in cap.executors]
                ok = [c for c in checks if c.ok]
                status = CapStatus.validated if ok else CapStatus.unavailable
                evidence = "; ".join(f"{c.executor_id}: {c.reason}" for c in checks)
            prev = caps.get(f"capability:{cap.id}")
            if (
                prev is not None
                and prev.status == CapStatus.operational
                and status == CapStatus.validated
            ):
                status = CapStatus.operational  # keep real execution evidence
            caps.register(
                CapabilityManifest(
                    id=f"capability:{cap.id}",
                    kind="capability",
                    name=cap.id,
                    description=cap.description,
                    input_schema=cap.input_schema,
                    output_schema=cap.output_schema,
                    operations=[executor_id(s) for s in cap.executors],
                    side_effects=cap.side_effects,
                    timeout_s=cap.timeout_s,
                    status=status,
                    evidence=evidence[:500],
                    config_requirements=sorted({e for s in cap.executors for e in s.requires_env}),
                )
            )
        for agent in self.agents.values():
            usable = [c for c in agent.capabilities if caps.routable(f"capability:{c}")]
            if not agent.enabled:
                status, ev = CapStatus.unavailable, "disabled in configuration"
            elif len(usable) == len(agent.capabilities):
                status, ev = CapStatus.validated, "all capabilities have an eligible executor"
            elif usable:
                status, ev = (
                    CapStatus.degraded,
                    f"usable: {usable}; unavailable: {sorted(set(agent.capabilities) - set(usable))}",
                )
            else:
                status, ev = CapStatus.unavailable, "no capability has an eligible executor"
            caps.register(
                CapabilityManifest(
                    id=f"agent:{agent.id}",
                    kind="agent",
                    name=agent.name or agent.id,
                    description=agent.description,
                    provides=[f"capability:{c}" for c in agent.capabilities],
                    operations=agent.capabilities,
                    status=status,
                    evidence=ev,
                )
            )
