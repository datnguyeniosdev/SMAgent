"""Deliver work from Telegram into open Claude Code sessions, in two modes sharing one inbox.

push  `agw session` in a terminal: a stdio shim (app/channel_shim.py) holds an SSE stream and turns
      each message into a `notifications/claude/channel` event (Claude Code channels, research
      preview, >= 2.1.80); the session starts working at once. Answers go through the shim's `reply`.
pull  any session (desktop app, VS Code, terminal): `/loop 1m` calls the MCP tool `get_messages`
      and answers with `reply_message`. Works without channels, up to one loop interval late.

Each session has an operator switch (/on, /off, persisted per session id). Messages go to an open,
enabled, non-blocked session of the project (push preferred, newest first) and otherwise wait.
Answers are published as reports of the session's record-only task, so a Telegram reply to one is
routed back into the same session (see CollaborationService.submit_feedback).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.models import new_id, now

log = logging.getLogger(__name__)
PULL_TTL = timedelta(minutes=5)  # a pull session is open while it polled this recently (/loop 1m)
LEASE = timedelta(minutes=60)  # an unanswered handed-over message keeps its session busy this long
MAX_DELIVERIES = 2  # an unanswered message is handed out again once after its lease, then expires
OPEN_STATES = ("pending", "queued", "delivered")  # waiting, assigned to a session, in progress


def stop_claude(pid: int) -> bool:
    """SIGTERM the session's Claude Code process, only if that pid still is a `claude` process."""
    if pid <= 1:
        return False
    try:
        cmd = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True
        ).stdout
        if "claude" not in cmd:  # gone, or the pid was reused by something else
            return False
        os.kill(pid, signal.SIGTERM)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _norm(text: str) -> str:
    return re.sub(r"[^0-9a-z]+", "", text.lower())


# agent tools that run as a desktop app or inside an IDE (the rest are terminal CLIs)
APP_AGENTS = {"cursor", "windsurf", "vscode", "copilot", "cline", "roo", "kilo", "zed", "jetbrains",
              "junie", "kiro", "trae", "claude_desktop"}  # fmt: skip


@dataclass
class LiveSession:
    session_id: str
    project: str
    cwd: str
    task_id: str  # record-only task carrying this session's reports
    mode: str = "push"  # push (channel stream) | pull (get_messages)
    blocked: bool = False  # Claude Code drops channel events here (e.g. org policy)
    enabled: bool = True  # operator switch: /on, /off
    connected_at: datetime = field(default_factory=now)
    last_seen: datetime = field(default_factory=now)
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    num: int = 0  # short id shown as s<num> (/list, /send s1 ..., /off s1)
    pid: int = 0  # push: the Claude Code process (the shim's parent), for /close
    agent: str = ""  # which agent tool runs the session (claude_code, opencode, codex, ...)
    closing: bool = False  # pull: /close asked its loop to stop at the next round
    client: str = ""  # "app" (desktop app / IDE) or "cli", as the session reports it

    @property
    def kind(self) -> str:
        """app | cli: what the session says, else known from the agent; push sessions are terminals."""
        if self.client in ("app", "cli"):
            return self.client
        return "app" if self.agent in APP_AGENTS else "cli"

    @property
    def deliverable(self) -> bool:
        return self.enabled and not self.blocked and not self.closing


class ChannelHub:
    def __init__(self, svc):
        self.svc = svc
        self.sessions: dict[str, LiveSession] = {}
        self._stopping: set[str] = (
            set()
        )  # closed pull sessions: tell their loop to stop if it polls again
        # session queues live in memory: what they held when the last process stopped waits again
        for msg in svc.store.list("channel_msg", states=["queued"]):
            self._set(msg, "pending")

    # ---- sessions ----

    def connect(
        self,
        session_id: str,
        project: str,
        cwd: str,
        blocked: bool = False,
        mode: str = "push",
        pid: int = 0,
    ) -> LiveSession:
        rec = self.svc.store.get("channel_session", session_id) or {}
        task = self.svc.tasks.get(rec.get("task_id", ""))
        if task is None:  # first connection (a reconnect after a gateway restart reuses the task)
            name = cwd.rstrip("/").rsplit("/", 1)[-1] or project
            task = self.svc.register_external(
                f"Live session · {name}", "channel", project, session_id
            )
            task.request["data"]["channel_session"] = session_id
            self.svc.store.put(
                "task", task.task_id, task, task_id=task.task_id, state=task.state.value
            )
        s = LiveSession(session_id, project, cwd, task.task_id, mode=mode, blocked=blocked,
                        enabled=rec.get("enabled", True))  # fmt: skip
        s.num, s.pid = self._number("channel_snum", session_id), pid
        self.project_num(project)
        self._save(s)
        self.sessions[session_id] = s
        self._expire()
        self._flush_pending(project)
        self.svc.store.event("channel.connected", task_id=task.task_id, session=session_id,
                             project=project, mode=mode, blocked=blocked)  # fmt: skip
        log.info("%s session %s connected (%s, %s)", mode, session_id, project, cwd)
        return s

    def _save(self, s: LiveSession) -> None:
        body = {"project": s.project, "cwd": s.cwd, "task_id": s.task_id, "mode": s.mode,
                "enabled": s.enabled}  # fmt: skip
        self.svc.store.put("channel_session", s.session_id, body, task_id=s.task_id, state="open")

    def disconnect(self, s: LiveSession) -> None:
        if (
            self.sessions.get(s.session_id) is s
        ):  # a newer stream for the same id may have replaced it
            del self.sessions[s.session_id]
            self._release(s)
            self.svc.store.event("channel.disconnected", task_id=s.task_id, session=s.session_id)
            log.info("%s session %s disconnected", s.mode, s.session_id)

    def _release(self, s: LiveSession) -> None:
        """Messages a session took but never handed to Claude go back to the inbox."""
        while not s.queue.empty():
            msg = s.queue.get_nowait()
            self._set(msg, "pending")
        self._flush_pending(s.project)

    def _prune(self) -> None:
        cutoff = now() - PULL_TTL
        for s in [s for s in self.sessions.values() if s.mode == "pull" and s.last_seen < cutoff]:
            self.disconnect(s)

    def live(self) -> list[LiveSession]:
        self._prune()
        return sorted(self.sessions.values(), key=lambda s: s.connected_at)

    def _number(self, kind: str, key: str) -> int:
        """Stable short number per key (first come, first numbered; never reused)."""
        if rec := self.svc.store.get(kind, key):
            return rec["num"]
        num = 1 + max((r["num"] for r in self.svc.store.list(kind)), default=0)
        self.svc.store.put(kind, key, {"num": num, "key": key})
        return num

    def project_num(self, project: str) -> int:
        num = self._number("channel_pnum", _norm(project))
        self.svc.store.put(
            "channel_pnum", _norm(project), {"num": num, "key": _norm(project), "name": project}
        )
        return num

    def project_dir(self, project: str) -> str | None:
        """Folder of the project's most recent session (where /open p# starts a new one)."""
        dirs = [r["cwd"] for r in self.svc.store.list("channel_session")
                if r.get("cwd") and _norm(r["project"]) == _norm(project)]  # fmt: skip
        live = [
            s.cwd for s in self.sessions.values() if s.cwd and _norm(s.project) == _norm(project)
        ]
        return (live or dirs or [None])[-1]

    def projects(self) -> list[tuple[int, str]]:
        """(p-number, name) of every project a session ever opened for, by number."""
        return sorted(
            (r["num"], r["name"]) for r in self.svc.store.list("channel_pnum") if r.get("name")
        )

    def find(self, ref: str) -> LiveSession | None:
        """s<num>, session id, unique id prefix, or a project with exactly one open session."""
        live = self.live()
        if m := re.fullmatch(r"[sS](\d+)", ref.strip()):
            return next((s for s in live if s.num == int(m.group(1))), None)
        hits = [s for s in live if s.session_id == ref] or [
            s for s in live if s.session_id.startswith(ref)
        ]
        if not hits and (project := self.resolve_project(ref)):
            hits = [s for s in live if _norm(s.project) == _norm(project)]
        return hits[0] if len(hits) == 1 else None

    def close(self, s: LiveSession) -> str:
        """/close: push -> stop its Claude Code process; pull -> its loop stops at the next round."""
        self.svc.store.event("channel.close", task_id=s.task_id, session=s.session_id, mode=s.mode)
        if s.mode == "pull":  # gone from /list now; if its loop is still alive it is told to stop
            self._stopping.add(s.session_id)
            self.disconnect(s)  # also hands its unstarted work back
            return "removed; if its loop is still running it is told to stop at its next round"
        if not stop_claude(
            s.pid
        ):  # unknown or gone: its shim would just reconnect, so keep it listed
            return "could not stop its Claude Code process (close its window on the Mac)"
        self.disconnect(s)
        for p in [
            p for p in self.sessions.values() if p.mode == "pull" and p.cwd and p.cwd == s.cwd
        ]:
            self.disconnect(
                p
            )  # its loop ran in the process just stopped; a live poller re-registers
        return "stopped its Claude Code process"

    def set_enabled(self, s: LiveSession, on: bool) -> None:
        s.enabled = on
        self._save(s)
        self.svc.store.event("channel.switch", task_id=s.task_id, session=s.session_id, enabled=on)
        if on:
            self._flush_pending(s.project)
        else:
            self._release(s)  # its queued work moves to another enabled session, or waits

    def known_projects(self) -> set[str]:
        return {r["project"] for r in self.svc.store.list("channel_session")} | {
            s.project for s in self.sessions.values()
        }

    def resolve_project(self, ref: str) -> str | None:
        """p<num>, `2048`, `#2048_Shooter_K05` or the full name -> a known project (unique match only)."""
        if m := re.fullmatch(r"[pP](\d+)", ref.strip()):
            return next((name for num, name in self.projects() if num == int(m.group(1))), None)
        key = _norm(ref)
        projects = self.known_projects()
        exact = [p for p in projects if _norm(p) == key]
        if exact:
            return exact[0]
        partial = [p for p in projects if key and key in _norm(p)]
        return partial[0] if len(partial) == 1 else None

    def session_for(self, project: str) -> LiveSession | None:
        """Newest deliverable session of the project; push (instant) before pull."""
        self._prune()
        matches = [
            s
            for s in self.sessions.values()
            if _norm(s.project) == _norm(project) and s.deliverable
        ]
        return max(matches, key=lambda s: (s.mode == "push", s.connected_at)) if matches else None

    def undeliverable_reason(self, project: str) -> str:
        """Why nothing can take work for the project right now ("" when no session is open)."""
        mine = [s for s in self.live() if _norm(s.project) == _norm(project)]
        if any(s.blocked for s in mine) and not any(s.enabled and not s.blocked for s in mine):
            return (
                "the open session cannot receive pushed work (channels disabled by your org policy)"
            )
        if mine and not any(s.enabled for s in mine):
            return "its sessions are turned off (/on <session>)"
        return ""

    def blocked_for(self, project: str) -> bool:
        return any(s.blocked and _norm(s.project) == _norm(project) for s in self.live())

    # ---- messages ----

    def _flush_pending(self, project: str) -> None:
        target = self.session_for(project)
        if target is None:
            return
        waiting = self.svc.store.list("channel_msg", states=["pending"])
        for msg in sorted(waiting, key=lambda m: m.get("created_at", "")):  # oldest first
            if _norm(msg["project"]) == _norm(project):
                target.queue.put_nowait(msg)
                self._set(msg, "queued")

    def send(
        self,
        project: str,
        text: str,
        *,
        session_id: str | None = None,
        sender: str = "",
        source: str = "telegram",
        extra: dict | None = None,
    ) -> tuple[dict, LiveSession | None]:
        """Queue a message for a session (that one if it can take it, else the best of the project).
        extra: e.g. {"ask_id": ..., "kind": "request" | "answer"} for agent-to-agent messages."""
        target = self.sessions.get(session_id or "")
        if target is None or not target.deliverable:
            target = self.session_for(project)
        msg = {
            "message_id": new_id("chm"),
            "project": target.project if target else project,
            "text": text,
            "meta": {"source": source, "sender": sender, "project": project},
            "created_at": now().isoformat(),
        }
        msg["meta"]["message_id"] = msg["message_id"]
        msg.update(extra or {})
        if msg.get("kind"):
            msg["meta"]["kind"] = msg["kind"]
        msg["num"] = self._number("channel_mnum", msg["message_id"])  # short id m<num>
        self._set(msg, "queued" if target else "pending")
        if target:
            target.queue.put_nowait(msg)
        self.svc.store.event("channel.message", message_id=msg["message_id"],
                             delivered_to=target and target.session_id)  # fmt: skip
        return msg, target

    def _set(self, msg: dict, state: str) -> None:
        msg["state"] = state
        self.svc.store.put("channel_msg", msg["message_id"], msg, state=state)

    def delivered(self, msg: dict, session_id: str) -> None:
        """Handed to a session: in progress (the session is busy) until reply_message answers it."""
        msg["session_id"], msg["delivered_at"] = session_id, now().isoformat()
        # an answer to the session's own ask is information, not a job: it never keeps it busy
        self._set(msg, "done" if msg.get("kind") == "answer" else "delivered")

    def in_progress(self, session_id: str) -> list[dict]:
        cutoff = now() - LEASE
        return [
            m for m in self.svc.store.list("channel_msg", states=["delivered"])
            if m.get("session_id") == session_id and datetime.fromisoformat(m["delivered_at"]) > cutoff
        ]  # fmt: skip

    def _expire(self) -> None:
        """Delivered but unanswered past LEASE (the session lost it or died): back to the inbox,
        or after MAX_DELIVERIES expired with a note to the operator instead of lingering silently."""
        # ponytail: runs on pull rounds and connects only; a push-only gateway expires on reconnect
        cutoff, projects = now() - LEASE, set()
        for msg in self.svc.store.list("channel_msg", states=["delivered"]):
            if datetime.fromisoformat(msg["delivered_at"]) > cutoff:
                continue
            msg["deliveries"] = msg.get("deliveries", 1) + 1
            if msg["deliveries"] <= MAX_DELIVERIES:
                self._set(msg, "pending")
                projects.add(msg["project"])
            else:
                self._set(msg, "expired")
                self._tell_expired(msg)
            self.svc.store.event("channel.lease_expired", message_id=msg["message_id"],
                                 session=msg.get("session_id"), state=msg["state"])  # fmt: skip
        for project in projects:
            self._flush_pending(project)

    def _tell_expired(self, msg: dict) -> None:
        rec = self.svc.store.get("channel_session", msg.get("session_id", "")) or {}
        task = self.svc.tasks.get(rec.get("task_id", ""))
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if task is not None:
            html = (f"⌛ m{msg.get('num')} was never answered after {MAX_DELIVERIES} deliveries; "
                    "dropped. Send it again if it is still needed.")  # fmt: skip
            loop.create_task(self.svc._notify(task, html, "info"))

    def open_messages(self) -> list[dict]:
        """/queue: everything not answered or cancelled yet, oldest first."""
        msgs = [m for st in OPEN_STATES for m in self.svc.store.list("channel_msg", states=[st])]
        for m in msgs:
            m.setdefault("state", "pending")
        return sorted(msgs, key=lambda m: m.get("created_at", ""))

    def cancel(self, ref: str) -> dict | None:
        """m<num> or message id. Waiting work is dropped; in-progress work is only marked (the
        session's answer is then labelled cancelled; work already running in Claude is not stopped)."""
        ref = ref.strip()
        num = int(ref[1:]) if re.fullmatch(r"[mM]\d+", ref) else None
        msg = next(
            (m for m in self.open_messages() if m["message_id"] == ref or m.get("num") == num), None
        )
        if msg is None:
            return None
        for sess in self.sessions.values():  # drop it from any session's queue
            items = []
            while not sess.queue.empty():
                items.append(sess.queue.get_nowait())
            for m in items:
                if m["message_id"] != msg["message_id"]:
                    sess.queue.put_nowait(m)
        msg["cancelled_from"] = msg.get("state")
        self._set(msg, "cancelled")
        self.svc.store.event(
            "channel.cancelled", message_id=msg["message_id"], was=msg["cancelled_from"]
        )
        return msg

    def pull(
        self, project: str, session_id: str | None, cwd: str = "", limit: int = 1, agent: str = "",
        client: str = "",
    ):  # fmt: skip
        """get_messages: register/refresh a pull session and hand over what is queued for it.
        Nothing new while it still has an unanswered message (no duplicate or parallel jobs).
        Returns (session, new messages, messages still in progress)."""
        sid = session_id or f"pull_{_norm(project)[:40]}"
        if sid in self._stopping:  # /close: hand out nothing; the tool tells the loop to stop
            self._stopping.discard(sid)
            return LiveSession(sid, project, cwd, "", mode="pull", closing=True), [], []
        s = self.sessions.get(sid)
        if s is None or s.mode != "pull":
            s = self.connect(sid, project, cwd, mode="pull")
        s.last_seen = now()
        s.agent = agent or s.agent
        s.client = client if client in ("app", "cli") else s.client
        self._expire()
        busy = self.in_progress(s.session_id)
        out = []
        while s.enabled and not busy and not s.queue.empty() and len(out) < limit:
            msg = s.queue.get_nowait()
            self.delivered(msg, s.session_id)
            out.append(msg)
        return s, out, busy

    async def reply(
        self,
        session_id: str,
        text: str,
        message_id: str | None = None,
        images: list[tuple[str, bytes]] = (),
    ):
        """Claude's answer from a session -> a Telegram report on the session's task."""
        rec = self.svc.store.get("channel_session", session_id)
        if rec is None:
            raise KeyError(session_id)
        if s := self.sessions.get(session_id):
            s.last_seen = now()
        note = f"\n\n(answer to {message_id})" if message_id else ""
        msg = self.svc.store.get("channel_msg", message_id) if message_id else None
        if msg is not None and msg.get("kind") == "answer":  # answers need no reply
            self._set(msg, "done")
            return {"note": "that message was an answer to your own ask; nothing to reply"}
        if msg is not None and msg.get("ask_id") and msg.get("state") != "cancelled":
            self._set(
                msg, "done"
            )  # an agent's request: the answer goes back to that agent, not to Telegram
            by = f"s{s.num} ({s.project})" if s else rec["project"]
            return {"ask": self.svc.asks.answer(msg["ask_id"], text, by=by)}
        if msg is not None:
            if msg.get("state") == "cancelled":
                note = f"\n\n(answer to {message_id}, which was cancelled on Telegram)"
            else:
                self._set(msg, "done")  # frees the session for its next message
        body = text.strip() + note
        mode = rec.get("mode", "push")
        return await self.svc.collab.submit_report(
            rec["task_id"], body, f"claude_code ({mode} session)", images=images
        )
