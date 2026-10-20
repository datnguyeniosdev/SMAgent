"""Quick Actions: short opaque tokens bound to task/agent/session with expiry,
single use, and allowed-state checks. Tokens fit Telegram's 64-byte callback_data."""

import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from app.models import TaskState


class ActionError(Exception):
    pass


@dataclass
class Action:
    token: str
    kind: str  # approve|reject|retry|stop|view_report|review_diff|...
    task_id: str
    label: str
    allowed_states: frozenset[TaskState]
    expires_at: float
    agent: str | None = None
    session_id: str | None = None
    group: str | None = None  # actions sharing a group are consumed together
    payload: dict[str, Any] = field(default_factory=dict)
    used_by: int | str | None = None


class ActionRegistry:
    def __init__(self, ttl_s: int = 3600, clock=time.monotonic):
        self.ttl_s = ttl_s
        self.clock = clock
        self._actions: dict[str, Action] = {}

    def create(
        self,
        kind: str,
        task_id: str,
        label: str,
        allowed_states: set[TaskState],
        group: str | None = None,
        **kw,
    ) -> Action:
        token = secrets.token_urlsafe(9)  # 12 chars
        a = Action(
            token=token,
            kind=kind,
            task_id=task_id,
            label=label,
            allowed_states=frozenset(allowed_states),
            expires_at=self.clock() + self.ttl_s,
            group=group,
            **kw,
        )
        self._actions[token] = a
        return a

    def get(self, token: str) -> Action | None:
        return self._actions.get(token)

    def consume(self, token: str, actor: int | str, current_state: TaskState) -> Action:
        """Validate and mark used. Synchronous (no await) so check-and-set is atomic in asyncio."""
        a = self._actions.get(token)
        if a is None:
            raise ActionError("unknown action")
        if a.used_by is not None:
            raise ActionError("action already used")
        if self.clock() > a.expires_at:
            raise ActionError("action expired")
        if current_state not in a.allowed_states:
            raise ActionError(f"task is {current_state}, action not applicable")
        for other in self._actions.values():
            if a.group and other.group == a.group:
                other.used_by = actor
        a.used_by = actor
        return a

    def prune(self) -> None:
        now = self.clock()
        for t in [t for t, a in self._actions.items() if a.expires_at < now]:
            del self._actions[t]
