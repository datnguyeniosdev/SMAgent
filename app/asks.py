"""Agent-to-agent requests (ask_agent) with the operator as the fallback.

Agent A asks agent B (a session s#, or a project): the request lands in B's inbox like any Telegram
request, labelled as coming from A. B answers with reply_message; the answer goes back to A's inbox
as an "answer" message (and to get_ask). Nothing of this is posted to Telegram unless it goes wrong.

The watchdog (every 30 s) escalates to the operator when B cannot take the request (no open, enabled,
unblocked session) for UNREACHABLE, or has not answered by the deadline. The escalation shows B's state
and offers: resend, send to another session, open B's project, answer it myself, cancel.

States: queued (no session can take it yet) | delivered (B has it) | escalated | answered | cancelled.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from app.models import new_id, now

log = logging.getLogger(__name__)
UNREACHABLE = timedelta(minutes=2)  # nobody can take it this long -> ask the operator
DEFAULT_WAIT_S = 600
OPEN_STATES = ["queued", "delivered", "escalated"]


class AskError(ValueError):
    pass


class AskService:
    def __init__(self, svc):
        self.svc = svc

    @property
    def hub(self):
        return self.svc.channel

    def get(self, ask_id: str) -> dict | None:
        return self.svc.store.get("agent_ask", ask_id)

    def _save(self, a: dict) -> dict:
        self.svc.store.put("agent_ask", a["ask_id"], a, state=a["state"])
        return a

    def open_asks(self) -> list[dict]:
        return [a for st in OPEN_STATES for a in self.svc.store.list("agent_ask", states=[st])]

    def _label(self, session_id: str | None, project: str | None) -> str:
        s = self.hub.sessions.get(session_id or "")
        return f"s{s.num} ({s.project})" if s else (project or "an agent")

    # ---- asking ----

    def create(
        self, to: str, request: str, from_session: str | None = None, from_project: str | None = None,
        wait_s: int = DEFAULT_WAIT_S,
    ) -> dict:  # fmt: skip
        """to: s# (that session) or a project (p#, name, fragment: its best session)."""
        target = self.hub.find(to) if re.fullmatch(r"[sS]\d+", to.strip()) else None
        project = target.project if target else self.hub.resolve_project(to)
        if project is None:
            raise AskError(f"unknown agent or project {to!r} (see list_agents)")
        if not request.strip():
            raise AskError("empty request")
        me = self.hub.sessions.get(from_session or "")
        a = {
            "ask_id": new_id("ask"),
            "from_session": from_session,
            "from_project": me.project if me else from_project,
            "to": to,
            "to_session": target.session_id if target else None,
            "to_project": project,
            "request": request.strip()[:4000],
            "created_at": now().isoformat(),
            "deadline": (now() + timedelta(seconds=max(60, min(wait_s, 86400)))).isoformat(),
            "state": "queued",
        }
        self._deliver(a)
        self.svc.store.event("ask.created", ask_id=a["ask_id"], to=project, by=a["from_project"])
        return a

    def _deliver(self, a: dict, session_id: str | None = None) -> None:
        sender = self._label(a["from_session"], a["from_project"])
        text = (
            f"🤝 Request from agent {sender} (ask {a['ask_id']}):\n{a['request']}\n\n"
            "Do it, then answer with reply_message (pass this message_id): the answer goes back "
            "to that agent."
        )
        msg, target = self.hub.send(
            a["to_project"], text, session_id=session_id or a["to_session"], sender=sender,
            source="agent", extra={"ask_id": a["ask_id"], "kind": "request"},
        )  # fmt: skip
        a.update(message_id=msg["message_id"], state="delivered" if target else "queued",
                 delivered_to=target.session_id if target else None, sent_at=now().isoformat())  # fmt: skip
        self._save(a)

    def answer(self, ask_id: str, text: str, by: str) -> dict:
        """B's reply (or the operator's): back to A, as information that needs no reply."""
        a = self.get(ask_id)
        if a is None:
            raise AskError(f"unknown ask {ask_id}")
        if a["state"] in ("answered", "cancelled"):
            return a  # late duplicate (e.g. B answers after the operator did): keep the first
        a.update(
            state="answered",
            answer=text.strip()[:8000],
            answered_by=by,
            answered_at=now().isoformat(),
        )
        self._save(a)
        if a.get("from_project"):
            self.hub.send(
                a["from_project"],
                f"🤝 Answer from {by} to your ask {ask_id}:\n{a['answer']}\n\n"
                "(Use it to continue the work that needed it. It needs no reply.)",
                session_id=a.get("from_session"), sender=by, source="agent",
                extra={"ask_id": ask_id, "kind": "answer"},
            )  # fmt: skip
        self.svc.store.event("ask.answered", ask_id=ask_id, by=by)
        return a

    def cancel(self, ask_id: str, by: str = "operator") -> dict:
        a = self.get(ask_id)
        if a is None or a["state"] in ("answered", "cancelled"):
            raise AskError("nothing to cancel")
        if a.get("message_id"):
            self.hub.cancel(a["message_id"])
        a.update(state="cancelled", cancelled_by=by)
        self._save(a)
        if a.get("from_project"):
            self.hub.send(a["from_project"], f"🤝 Your ask {ask_id} was cancelled by the {by}; continue without it.",
                          session_id=a.get("from_session"), sender=by, source="agent",
                          extra={"ask_id": ask_id, "kind": "answer"})  # fmt: skip
        return a

    def resend(self, ask_id: str, to_session: str | None = None) -> dict:
        """Operator: deliver again (to the same target, or another session), with a fresh deadline."""
        a = self.get(ask_id)
        if a is None or a["state"] in ("answered", "cancelled"):
            raise AskError("this ask is already closed")
        if a.get("message_id"):
            self.hub.cancel(a["message_id"])
        if to_session:
            s = self.hub.sessions.get(to_session)
            if s is None:
                raise AskError("that session is no longer open")
            a.update(to_session=s.session_id, to_project=s.project)
        wait = datetime.fromisoformat(a["deadline"]) - datetime.fromisoformat(a["created_at"])
        a["deadline"] = (now() + wait).isoformat()
        a.pop("escalated_at", None)
        self._deliver(a, session_id=to_session)
        return a

    # ---- watchdog ----

    def why_stuck(self, a: dict) -> str | None:
        """None while things are fine; otherwise what the operator should know about B."""
        hub = self.hub
        late = now() > datetime.fromisoformat(a["deadline"])
        target = hub.sessions.get(a.get("delivered_to") or "")
        if a["state"] == "delivered" and target is not None and target.deliverable:
            if not late:
                return None
            busy = [
                m
                for m in hub.in_progress(target.session_id)
                if m["message_id"] != a.get("message_id")
            ]
            seen = int((now() - target.last_seen).total_seconds() // 60)
            doing = f"; still busy with m{busy[0].get('num')}" if busy else ""
            since = a["sent_at"][11:16]
            return f"s{target.num} has had it since {since} UTC, no answer (last seen {seen} min ago{doing})"
        best = hub.session_for(a["to_project"])
        if best is not None:  # someone can take it now (e.g. a session opened): deliver there
            if a["state"] == "queued" or target is None or not target.deliverable:
                self.resend(a["ask_id"], to_session=best.session_id)
            return None
        reason = (
            hub.undeliverable_reason(a["to_project"]) or f"no open session for {a['to_project']}"
        )
        waited = now() - datetime.fromisoformat(a.get("sent_at") or a["created_at"])
        return reason if (waited > UNREACHABLE or late) else None

    async def check(self) -> list[str]:
        """One watchdog round; returns the ask ids escalated now."""
        escalated = []
        for a in self.open_asks():
            if a["state"] == "escalated":
                continue  # waiting for the operator's button
            try:
                reason = self.why_stuck(a)
            except AskError:
                continue
            if reason:
                await self.escalate(a, reason)
                escalated.append(a["ask_id"])
        return escalated

    async def escalate(self, a: dict, reason: str) -> None:
        a.update(state="escalated", escalated_at=now().isoformat(), reason=reason)
        self._save(a)
        self.svc.store.event("ask.escalated", ask_id=a["ask_id"], reason=reason)
        send = getattr(self.svc.notifier, "send_ask_escalation", None)
        if send is None:
            log.warning(
                "ask %s needs the operator but Telegram is not configured: %s", a["ask_id"], reason
            )
            return
        try:
            await send(a)
        except Exception:
            log.exception("escalation of %s could not be sent", a["ask_id"])
