"""Operator on/off switches for the MCP tools agents call (Telegram /mcp), kept in the store: a change
applies to the next call, with no restart, and survives restarts. A switch never set uses its default
(generate_art: the fal provider's `enabled` in config/providers/models.yaml; everything else: on)."""

from __future__ import annotations

KIND = "switch"


class Switches:
    def __init__(self, store, defaults: dict[str, bool] | None = None):
        self.store = store
        self.defaults = defaults or {}

    def on(self, name: str) -> bool:
        rec = self.store.get(KIND, name)
        return self.defaults.get(name, True) if rec is None else bool(rec["on"])

    def set(self, name: str, on: bool) -> None:
        self.store.put(KIND, name, {"on": on})
        if on and name.startswith("art:"):  # a provider is only usable through the tool: turn it on too
            self.store.put(KIND, "generate_art", {"on": True})
