"""Capability-based routing. Never downgrades silently: no match -> UnsupportedCapability."""

from app.connectors.base import AgentConnector, Cap, UnsupportedCapability


class Router:
    def __init__(self, connectors: list[AgentConnector]):
        self.connectors = {c.name: c for c in connectors}
        self.switches = None  # the orchestrator's: the operator turns API agents on/off (/agents)

    def on(self, name: str) -> bool:
        return self.switches is None or self.switches.on(f"api:{name}")

    def route(
        self,
        required: set[Cap],
        preference: list[str] | None = None,
        explicit: str | None = None,
        exclude: set[str] = frozenset(),
    ) -> AgentConnector:
        if explicit:
            c = self.connectors.get(explicit)
            if c is None:
                raise UnsupportedCapability(f"unknown agent {explicit!r}")
            if not c.available:
                raise UnsupportedCapability(f"agent {explicit!r} is not configured")
            if not self.on(explicit):
                raise UnsupportedCapability(f"agent {explicit!r} is switched off by the operator")
            missing = required - c.caps
            if missing:
                raise UnsupportedCapability(
                    f"agent {explicit!r} lacks: {', '.join(sorted(missing))}"
                )
            return c
        order = list(preference or []) + sorted(self.connectors)
        seen = set()
        for name in order:
            if name in seen or name in exclude or name not in self.connectors:
                continue
            seen.add(name)
            c = self.connectors[name]
            if c.available and self.on(name) and required <= c.caps:
                return c
        raise UnsupportedCapability(f"no available agent supports: {', '.join(sorted(required))}")

    def describe(self) -> list[dict]:
        return [
            {
                "name": c.name,
                "available": c.available,
                "on": self.on(c.name),
                "verification": c.verification,
                "capabilities": sorted(c.caps),
            }
            for c in self.connectors.values()
        ]
