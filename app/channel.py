"""Deliver work from Telegram into open Claude Code sessions, in two modes sharing one inbox.

push  `agw session` in a terminal: a stdio shim (app/channel_shim.py) holds an SSE stream and turns
      each message into a `notifications/claude/channel` event (Claude Code channels, research
      preview, >= 2.1.80); the session starts working at once. Answers go through the shim's `reply`.
pull  any session (desktop app, VS Code, terminal): the `agw wait` waiter (app/waiter.py) wakes it to
      call the MCP tool `get_messages`, answered with `reply_message`; a 3-hourly cron is the safety net.

Each session has an operator switch (/on, /off, persisted per session id). Messages go to an open,
enabled, non-blocked session of the project (push preferred, newest first) and otherwise wait.
Routing (docs/ARCHITECTURE.md): work for a named session (/send s#, a reply to its report) stays
in that session, one job after another. Project work goes to the project's main session, which only
routes it with `dispatch`: to the child session of that lane (same lane: in order) or to a new child
in its own copy-on-write copy of the project (other lanes: in parallel). Answers to a session's own questions skip the
line: they are what its current job waits for.
Answers are published as reports of the session's record-only task, so a Telegram reply to one is
routed back into the same session (see CollaborationService.submit_feedback).
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html import escape as escape_html
from pathlib import Path

from app.formatting import truncate
from app.models import new_id, now

log = logging.getLogger(__name__)
PULL_TTL = timedelta(
    minutes=5
)  # a pull session is open while it (or its waiter) polled this recently
LEASE = timedelta(minutes=60)  # an unanswered handed-over message keeps its session busy this long
CHILD_IDLE = timedelta(minutes=30)  # a child session with nothing to do this long is closed
BUSY = timedelta(minutes=30)  # one still_working call keeps a session busy this long
START_WAIT = timedelta(
    minutes=10
)  # a child or temporary router that has not connected by then failed
# a free session that leaves work untaken this long is not pulling
QUEUE_WAIT = timedelta(minutes=10)
MAX_DELIVERIES = 2  # an unanswered message is handed out again once after its lease, then expires
# waiting, assigned to a session, in progress, held because its session failed (operator decides)
OPEN_STATES = ("pending", "queued", "delivered", "stalled")


def claude_alive(pid: int) -> bool:
    """That pid is a running `claude` process (not gone, not reused by something else)."""
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True,
                             timeout=5).stdout  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return False
    return "claude" in cmd


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


def _lane_name(lane: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", lane.lower()).strip("-")[:30] or "task"


# agent tools that run as a desktop app or inside an IDE (the rest are terminal CLIs)
APP_AGENTS = {"cursor", "windsurf", "vscode", "copilot", "cline", "roo", "kilo", "zed", "jetbrains",
              "junie", "kiro", "trae", "claude_desktop"}  # fmt: skip


# display names of the agent tools in session titles (ClaudeCode(App)-Coinlets)
TOOL_TITLES = {"claude_code": "ClaudeCode", "codex": "Codex", "opencode": "OpenCode",
               "antigravity": "Antigravity", "gemini": "Gemini", "droid": "Droid",
               "cursor": "Cursor", "claude_desktop": "ClaudeDesktop"}  # fmt: skip


def _conversation(sid: str) -> bool:
    """A conversation's own session (#Main: folder session id + "_" + 4 hex), not a folder's."""
    return bool(re.fullmatch(r"pull_.+_[0-9a-f]{6}c?_[0-9a-f]{4}", sid))


def short_project(name: str) -> str:
    """'Coinlets: Stack & Merge (formerly "Tini Coin") (TCN)' -> 'Coinlets'; 'Glazed Gems (GMA)' ->
    'GlazedGems': the name before a subtitle or code, without spaces."""
    head = re.split(r"[:(—–]| - ", name, maxsplit=1)[0]
    return re.sub(r"[^0-9A-Za-z]+", "", head.title() if head.islower() else head) or name


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
    claude_pid: int = (
        0  # the Claude process it runs in, as its waiter or shim reports it (health only)
    )
    agent: str = ""  # which agent tool runs the session (claude_code, opencode, codex, ...)
    closing: bool = False  # pull: /close asked its loop to stop at the next round
    client: str = ""  # "app" (desktop app / IDE) or "cli", as the session reports it
    stalled: str = ""  # why its agent stopped (rate limit, API error, no answer): no new work
    last_work: datetime = field(default_factory=now)  # last message handed to it or answered
    silent_at: datetime | None = (
        None  # it left a gateway offer unanswered then (not asked for a while)
    )
    busy_until: datetime | None = (
        None  # still_working: on long work, never idle or stuck before this
    )

    @property
    def kind(self) -> str:
        """app | cli: what the session says, else known from the agent; push sessions are terminals."""
        if self.client in ("app", "cli"):
            return self.client
        return "app" if self.agent in APP_AGENTS else "cli"

    @property
    def title(self) -> str:
        """Tool(App|CLI)-Project, e.g. ClaudeCode(App)-Coinlets, Codex(App)-GlazedGems."""
        agent = self.agent or ("claude_code" if self.mode == "push" else "")
        tool = TOOL_TITLES.get(agent) or (
            agent.replace("_", " ").title().replace(" ", "") or "Agent"
        )
        return f"{tool}({'App' if self.kind == 'app' else 'CLI'})-{short_project(self.project)}"

    @property
    def deliverable(self) -> bool:
        return self.enabled and not self.blocked and not self.closing and not self.stalled


class ChannelHub:
    def __init__(self, svc):
        self.svc = svc
        self.sessions: dict[str, LiveSession] = {}
        self._bg: set[asyncio.Task] = set()  # central lane openings in flight (kept referenced)
        self._bids: dict[str, dict] = {}  # message_id -> {session_id: bid} while its offers are out
        self._bid_ev: dict[str, asyncio.Event] = {}
        # messages whose lane session is being opened right now (in memory: a restart forgets them, so
        # they are routed again): nobody routes them meanwhile (seen in prod: a slow worktree let every
        # flush open another session, nine for one request)
        self._opening: set[str] = set()
        self._reserved: set[str] = set()  # sessions an offer placement is giving work to right now
        self._lane_locks: dict[str, asyncio.Lock] = {}  # one opening at a time per project lane
        from app.project_registry import ProjectRegistry

        self.registry = ProjectRegistry(svc.store)  # stable project ids, aliases, routing mode
        self.connections: dict[str, str] = {}  # MCP connection -> the agent session it speaks for
        self._stopping: set[str] = (
            set()
        )  # closed pull sessions: tell their loop to stop if it polls again
        self._main_asked: set[str] = set()  # projects whose offline main was put to the operator
        from app.launcher import open_in_terminal

        self.launch = open_in_terminal  # (folder, agent, model): opens a child; tests replace it
        # session queues live in memory: what they held when the last process stopped waits again
        for msg in svc.store.list("channel_msg", states=["queued"]):
            self._set(msg, "cancelled" if msg.get("kind") == "offer" else "pending")  # offers lapse

    # ---- sessions ----

    def _ops(self, html: str) -> None:
        """A system notice (sessions coming and going, routing, lanes): the notice channel when one is
        set (TELEGRAM_NOTICE_CHAT_ID), else the operator's group."""
        self._notify("send_notice", html)

    def _label(self, s: LiveSession) -> str:
        from html import escape

        return f"<b>s{s.num}</b> {escape(s.title)}"

    def connect(
        self,
        session_id: str,
        project: str,
        cwd: str,
        blocked: bool = False,
        mode: str = "push",
        pid: int = 0,
        notify: bool = True,
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
        s.claude_pid = pid
        self.project_num(project)
        if (c := self.child(s)) and c.get("launched_at"):  # it came: no longer "starting"
            self.svc.store.put("channel_child", s.cwd, {**c, "launched_at": None})
        self._save(s)
        self.sessions[session_id] = s
        main = self.svc.store.get("channel_main", _norm(project)) or {}
        if main.get("pending_new") and not self.child(s):  # the operator asked for a new main
            self.set_main(s)
        elif mode == "push":  # (a pull session is known once its agent is: see pull)
            self._maybe_gate(s)
            self.main_for(project)
        self._expire()
        self._flush_pending(project)
        self.svc.store.event("channel.connected", task_id=task.task_id, session=session_id,
                             project=project, mode=mode, blocked=blocked)  # fmt: skip
        log.info("%s session %s connected (%s, %s)", mode, session_id, project, cwd)
        if notify:
            self._ops(f"🔌 {self._label(s)} connected")
        return s

    def _project_id(self, s: LiveSession) -> str:
        if s.mode == "pull" or s.mode == "push":
            self.registry.note_root(s.project, s.cwd)
        return self.registry.ensure(s.project).project_id

    @staticmethod
    def health(s: LiveSession) -> str:
        """Session health, apart from task and delivery state: ok | stalled | blocked | off | closing."""
        if s.closing:
            return "closing"
        if s.stalled:
            return "stalled"
        if s.blocked:
            return "blocked"
        return "ok" if s.enabled else "off"

    def _save(self, s: LiveSession) -> None:
        body = {"project": s.project, "cwd": s.cwd, "task_id": s.task_id, "mode": s.mode,
                "enabled": s.enabled, "agent": s.agent, "client": s.client,
                "project_id": self._project_id(s),
                "agent_id": s.agent or ("claude_code" if s.mode == "push" else ""),
                "claude_pid": s.claude_pid,
                "health": self.health(s)}  # fmt: skip
        self.svc.store.put("channel_session", s.session_id, body, task_id=s.task_id, state="open")

    def disconnect(self, s: LiveSession, why: str = "") -> None:
        if (
            self.sessions.get(s.session_id) is s
        ):  # a newer stream for the same id may have replaced it
            del self.sessions[s.session_id]
            rec = self.svc.store.get("channel_main", _norm(s.project)) or {}
            if rec.get("pinned") and rec.get("session_id") == s.session_id:
                # a #Main or /main is the main only while it is connected: then another is chosen
                # (automatically, or the next #Main / /main)
                self.svc.store.put(
                    "channel_main", _norm(s.project), {"project": rec.get("project") or s.project}
                )
            self._release(s)
            self._ops(f"⚪ {self._label(s)} left" + (f": {why}" if why else ""))
            self.svc.store.event("channel.disconnected", task_id=s.task_id, session=s.session_id)
            log.info("%s session %s disconnected", s.mode, s.session_id)

    def _release(self, s: LiveSession) -> None:
        """Messages a session took but never handed to Claude go back to the inbox."""
        while not s.queue.empty():
            msg = s.queue.get_nowait()
            self._set(msg, "cancelled" if msg.get("kind") == "offer" else "pending")  # offers lapse
        self._flush_pending(s.project)

    def _prune(self) -> None:
        self._close_idle_children()
        cutoff = now() - PULL_TTL
        for s in [s for s in self.sessions.values() if s.mode == "pull" and s.last_seen < cutoff]:
            if self.in_progress(s.session_id):  # still working on a message within its lease
                continue
            how = ("open the thread in Codex desktop" if s.agent == "codex"
                   else "send any message in that session (it restarts its waiter)")  # fmt: skip
            self.disconnect(s, f"not seen for {int(PULL_TTL.total_seconds() // 60)} min (app restarted or "
                            f"session closed). To reconnect, {how}.")  # fmt: skip

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
        """Folder of the project's most recent session (where /open p# starts a new one). A folder
        that no longer exists (renamed) is skipped; then the project folder whose README names it."""
        dirs = [r["cwd"] for r in self.svc.store.list("channel_session")
                if r.get("cwd") and _norm(r["project"]) == _norm(project)]  # fmt: skip
        live = [
            s.cwd for s in self.sessions.values() if s.cwd and _norm(s.project) == _norm(project)
        ]
        found = [d for d in live + dirs if Path(d).is_dir()]
        if found:
            return (([d for d in live if Path(d).is_dir()]) or found)[-1]
        from app.launcher import project_dirs
        from app.projects import detect_project, projects_root

        renamed = [
            d for d in project_dirs(projects_root()) if _norm(detect_project(d)) == _norm(project)
        ]
        return str(renamed[0]) if renamed else None

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

    def session_record(self, num: int) -> tuple[str, dict] | None:
        """(session id, stored record) of s<num> even when it is not live (its waiter stopped)."""
        for r in self.svc.store.list("channel_snum"):
            if r.get("num") == num and (rec := self.svc.store.get("channel_session", r["key"])):
                return r["key"], rec
        return None

    def close(self, s: LiveSession) -> str:
        """/close: push -> stop its Claude Code process; pull -> its loop stops at the next round.
        Closing the main lets the project's next session become the main. A child's (or the
        temporary router's) Terminal window is closed with it."""
        if self.main_for(s.project) is s and not self.pinned(s.project):  # a pinned main stays
            self.svc.store.put("channel_main", _norm(s.project), {"project": s.project})
        if wid := (self.child(s) or {}).get("window") or self._router_window(s):
            from app.launcher import close_terminal_window

            self._background(lambda: close_terminal_window(wid))
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
        """/on, /off. A window started with `agw session` is a push session plus its pull loop in the
        same folder: both switch together, or /off would just move the work to the twin."""
        twins = [s] + [x for x in self.sessions.values()
                       if x is not s and s.cwd and x.cwd == s.cwd and x.mode != s.mode]  # fmt: skip
        for x in twins:
            x.enabled = on
            self._save(x)
            self.svc.store.event(
                "channel.switch", task_id=x.task_id, session=x.session_id, enabled=on
            )
        if on:
            self._flush_pending(s.project)
        else:
            for x in twins:
                self._release(x)  # its queued work moves to another enabled session, or waits

    def forget_project(self, name: str) -> str | None:
        """Drop a project name nothing uses any more (an old name after a rename, a test project):
        its p-number, main and model records and its old session records. None when done, else why not.
        Its child folders stay on disk (/children prune). A session opening for it brings it back."""
        if any(_norm(s.project) == _norm(name) for s in self.sessions.values()):
            return "it still has an open session (/close it first)"
        if any(_norm(m.get("project") or "") == _norm(name) for m in self.open_messages()):
            return "it still has open requests (/queue, /cancel them first)"
        store = self.svc.store
        for kind in ("channel_pnum", "channel_main", "channel_model"):
            store.delete(kind, _norm(name))
        for sid, r in store.items("channel_session"):
            if _norm(r.get("project") or "") == _norm(name):
                store.delete("channel_session", sid)
        return None

    def known_projects(self) -> set[str]:
        return {r["project"] for r in self.svc.store.list("channel_session")} | {
            s.project for s in self.sessions.values()
        }

    def resolve_project(self, ref: str) -> str | None:
        """p<num>, `2048`, `#2048_Shooter_K05` or the full name -> a known project (unique match only)."""
        if m := re.fullmatch(r"[pP](\d+)", ref.strip()):
            return next((name for num, name in self.projects() if num == int(m.group(1))), None)
        if (p := self.registry.resolve(ref)) and p.name in self.known_projects():
            return p.name  # by id or an earlier name: the project's current name
        key = _norm(ref)
        projects = self.known_projects()
        exact = [p for p in projects if _norm(p) == key]
        if exact:
            return exact[0]
        partial = [p for p in projects if key and key in _norm(p)]
        return partial[0] if len(partial) == 1 else None

    def session_for(
        self, project: str, exclude: str | None = None, agent: str | None = None
    ) -> LiveSession | None:
        """An idle session of the project before a busy one, then push (instant) before pull, then the
        newest. exclude: the asking session (an agent's request never goes back to itself)."""
        self._prune()
        matches = [
            s
            for s in self.sessions.values()
            if _norm(s.project) == _norm(project)
            and s.deliverable
            and s.session_id != exclude
            and (agent is None or _norm(s.agent) == _norm(agent))
        ]

        def rank(s: LiveSession):
            idle = s.queue.empty() and not self.in_progress(s.session_id)
            return idle, s.mode == "push", s.connected_at

        return max(matches, key=rank) if matches else None

    def undeliverable_reason(self, project: str) -> str:
        """Why nothing can take work for the project right now ("" when no session is open)."""
        mine = [s for s in self.live() if _norm(s.project) == _norm(project)]
        if any(s.blocked for s in mine) and not any(s.enabled and not s.blocked for s in mine):
            return (
                "the open session cannot receive pushed work (channels disabled by your org policy)"
            )
        if mine and not any(s.enabled for s in mine):
            return "its sessions are turned off (/on <session>)"
        folder = self.project_dir(project)
        if not mine and folder:
            from app.launcher import app_sessions

            if app := app_sessions(Path(folder)):
                return (f"it is open in Claude desktop ('{app[0]['title'][:60]}') but not connected (its "
                        "waiter stopped); send any message in that session to reconnect it")  # fmt: skip
        return ""

    def blocked_for(self, project: str) -> bool:
        return any(s.blocked and _norm(s.project) == _norm(project) for s in self.live())

    # ---- messages ----

    def _flush_pending(self, project: str) -> None:
        waiting = self.svc.store.list("channel_msg", states=["pending"])
        for msg in sorted(waiting, key=lambda m: m.get("created_at", "")):  # oldest first
            if _norm(msg["project"]) != _norm(project):
                continue
            target = self._route(msg, exclude=msg.get("from_session"))
            if target is not None:
                self._enqueue(target, msg)
                self._assigned(msg, target)
                if (msg.get("meta") or {}).get("source") == "telegram":
                    self._ops(f"📬 m{msg.get('num')} delivered to {self._label(target)}")

    def _idle(self, s: LiveSession) -> bool:
        """No work queued (an unread offer is not work) and none in progress."""
        return all(m.get("kind") == "offer" for m in s.queue._queue) and not self.in_progress(
            s.session_id
        )

    def _enqueue(self, s: LiveSession, msg: dict) -> None:
        """Into a session's queue; an answer (its current job waits for it) or routing work for the
        main session (a quick dispatch) goes first."""
        msg["queued_at"], msg["queued_for"] = now().isoformat(), s.session_id
        s.queue.put_nowait(msg)
        first = lambda m: m.get("kind") in ("answer", "offer") or m.get("route")  # noqa: E731
        if first(msg):  # behind earlier answers and routing work, before the rest
            q = s.queue._queue  # ponytail: asyncio.Queue internals, like _next
            q.pop()
            q.insert(next((i for i, m in enumerate(q) if not first(m)), len(q)), msg)
        self._set(msg, "queued")

    def _take(self, s: LiveSession, msg: dict) -> None:
        s.queue._queue.remove(msg)  # ponytail: asyncio.Queue internals

    def _never_started(self) -> None:
        """Nothing waits forever for a session that never came: work held for a child that did not
        connect within START_WAIT goes to the operator as stuck (move or cancel), and a temporary
        router that did not connect, or got stuck itself, is given up and the operator is asked
        (make an open session the main, open a new one, or wait)."""
        late = (now() - START_WAIT).isoformat()
        for msg in self.svc.store.list("channel_msg", states=["pending"]):
            if msg.get("wait_cwd") and msg.get("waiting_since", "") < late:
                path = msg.pop("wait_cwd")
                self._stall([msg], "", f"its child session in {Path(path).name} did not start within "
                                       f"{int(START_WAIT.total_seconds() // 60)} min")  # fmt: skip
        for rec in self.svc.store.list("channel_main"):
            cwd = rec.get("router_cwd")
            if not cwd or rec.get("router_since", "") >= late:
                continue
            if any(x.cwd == cwd and x.deliverable for x in self.sessions.values()):
                continue
            for x in [x for x in self.sessions.values() if x.cwd == cwd]:
                self.close(x)  # it connected but is stuck too
            self.svc.store.put("channel_main", _norm(rec["project"]), {**{k: v for k, v in rec.items()
                               if k not in ("router_cwd", "routed", "router_since", "router_window")},
                               "router_failed": True})  # fmt: skip
            self._ops(f"🧭 the temporary router of <b>{escape_html(rec['project'])}</b> did not work; "
                      "asking you what to do")  # fmt: skip
            self._main_asked.add(_norm(rec["project"]))
            self._notify("send_main_offline", rec["project"])

    def _untaken(self) -> None:
        """Routing work the main leaves untaken for QUEUE_WAIT (busy on something else, or not
        pulling): the main counts as stuck and a temporary router takes over. Other work a session
        leaves untaken for QUEUE_WAIT once it is free (counted from its last answer, not from when the
        work was queued behind its job) goes to the operator as stuck."""
        late = (now() - QUEUE_WAIT).isoformat()
        for s in [x for x in self.sessions.values() if x.mode == "pull"]:
            busy = bool(self.in_progress(s.session_id))
            free_since = s.last_work.isoformat()
            for msg in list(s.queue._queue):
                if msg.get("route") and msg.get("queued_at", "") < late:  # the main is not routing
                    self.stall(
                        s.session_id,
                        f"routing work untaken for {int(QUEUE_WAIT.total_seconds() // 60)} min",
                    )
                    break
                waiting_since = max(msg.get("queued_at", ""), free_since)
                if (
                    not busy
                    and msg.get("kind") not in ("answer", "offer")
                    and not msg.get("route")
                    and waiting_since < late
                ):
                    self._take(s, msg)
                    self._stall([msg], s.session_id,
                                f"not picked up within {int(QUEUE_WAIT.total_seconds() // 60)} min")  # fmt: skip

    # ---- main session and children: docs/ARCHITECTURE.md ----

    def child(self, s: LiveSession) -> dict | None:
        """The child record of a session opened by dispatch (lane, branch, worktree), else None."""
        return self.svc.store.get("channel_child", s.cwd) if s.cwd else None

    def adopt(self, s: LiveSession, lane: str) -> dict:
        """A conversation in the project's own folder (a former main) becomes a worker of `lane`:
        it gets that lane's work like a child, but it is never closed for idling or pruned, and
        nothing is fetched back (it works in the project itself)."""
        if self.main_for(s.project) is s:
            self.svc.store.put("channel_main", _norm(s.project), {"project": s.project})
        root = s.cwd.split("/.claude/worktrees/")[0]
        rec = {"project": s.project, "lane": _lane_name(lane), "branch": "", "repo": root,
               "path": s.cwd, "agent": s.agent or "claude_code", "provider": "adopted",
               "created_at": now().isoformat()}  # fmt: skip
        self.svc.store.put("channel_child", s.cwd, rec)
        return rec

    def set_main(self, s: LiveSession, flush: bool = True, pin: bool | None = None) -> None:
        """/main s# (pin=True: the operator's choice, never replaced automatically), or the first
        session of a project: it routes the project's work. pin=None keeps the project's pin.
        A 🧭 child made the main (#Main in it) leaves its lane: it is no longer a child, and new
        lanes are still copied from the project repo, not from its copy."""
        pin = self.pinned(s.project) if pin is None else pin
        rec = {"project": s.project, "session_id": s.session_id, "cwd": s.cwd, "agent": s.agent,
               "kind": s.kind, "pinned": pin}  # fmt: skip
        if c := self.child(s):
            self.svc.store.delete("channel_child", s.cwd)
            rec["repo"] = c.get("repo")
        elif (old := self.svc.store.get("channel_main", _norm(s.project)) or {}).get(
            "cwd"
        ) == s.cwd:
            rec["repo"] = old.get("repo")  # the same detached folder: keep where lanes come from
        self.svc.store.put(
            "channel_main", _norm(s.project), {k: v for k, v in rec.items() if v is not None}
        )
        self._main_asked.discard(_norm(s.project))
        if flush:  # not from main_for, which may run inside a flush (a message queued twice)
            self._flush_pending(s.project)

    def _pinned_sid(self, project: str) -> str | None:
        rec = self.svc.store.get("channel_main", _norm(project)) or {}
        return rec.get("session_id") if rec.get("pinned") else None

    def outranked(self, s: LiveSession) -> LiveSession | None:
        """The pinned main a session may not take over: a live desktop-app main is kept against a
        session of another kind (two #Main: the desktop app wins)."""
        cur = self.sessions.get(self._pinned_sid(s.project) or "")
        if cur is not None and cur is not s and cur.kind == "app" and s.kind != "app":
            return cur
        return None

    def claim_main(self, project: str, agent: str, cwd: str, client: str, instance: str):
        """#Main typed in a conversation (`agw main`): it leaves the folder's shared session for its
        own and becomes the project's pinned 👑 main until it disconnects or is removed. Returns
        (session, None), or (None, why) when a desktop-app main keeps the place."""
        sid = self.pull_id(project, agent, cwd, client, instance)
        probe = LiveSession(sid, project, cwd, "", mode="pull", agent=agent, client=client)
        if cur := self.outranked(probe):
            return None, f"{self._label(cur)} (desktop app) is the main: a desktop-app main is kept"
        s, _, _ = self.pull(project, sid, cwd, 0, agent, client)
        lane = (self.child(s) or {}).get("lane")
        self.set_main(s, pin=True)
        left = f" (left 🧭 lane {escape_html(lane)})" if lane else ""
        self._ops(f"👑📌 {self._label(s)} is the main of {escape_html(project)} (#Main){left}")
        return s, None

    def pinned(self, project: str) -> bool:
        return bool((self.svc.store.get("channel_main", _norm(project)) or {}).get("pinned"))

    def unpin_main(self, project: str) -> None:
        """/main p# auto: the gateway may choose the project's main again."""
        rec = self.svc.store.get("channel_main", _norm(project)) or {"project": project}
        self.svc.store.put("channel_main", _norm(project), {**rec, "pinned": False})

    def auto_main(self) -> bool:
        """Whether a project without a main gets one automatically (/main auto on|off)."""
        return (self.svc.store.get("channel_setting", "auto_main") or {}).get("on", True)

    def set_auto_main(self, on: bool) -> None:
        self.svc.store.put("channel_setting", "auto_main", {"on": on})

    def want_new_main(self, project: str) -> None:
        """The operator opens a new main: the next session of the project to connect becomes it."""
        rec = self.svc.store.get("channel_main", _norm(project)) or {"project": project}
        self.svc.store.put("channel_main", _norm(project), {**rec, "pending_new": True})

    def main_for(self, project: str) -> LiveSession | None:
        """The project's main session when it can take work. With none chosen yet, the oldest open
        session (app before CLI, never a child) becomes it. The main stuck (rate limit, API error,
        not picking up): a temporary router session is opened in <repo>/.agw-children/router and
        routes for it with the same lanes (they live in the store); when the main works again it
        takes over, hears what was routed, and the router is closed. The main offline: None (the
        operator is asked)."""
        rec = self.svc.store.get("channel_main", _norm(project))
        router_cwd = (rec or {}).get("router_cwd") or ""
        mine = [x for x in self.sessions.values() if _norm(x.project) == _norm(project)
                and not self.child(x) and not (router_cwd and x.cwd == router_cwd)]  # fmt: skip
        if rec is None or not (rec.get("session_id") or rec.get("pending_new")):
            if (rec or {}).get("pinned") or not self.auto_main():  # the operator chooses (/main)
                return None
            ready = [x for x in mine if x.deliverable]
            if not ready:
                return None
            first = min(ready, key=lambda x: (x.kind != "app", x.agent not in ("", "claude_code"),
                                              x.connected_at))  # fmt: skip
            self.set_main(first, flush=False)
            return first
        # a window's push session and its pull loop are one main (the push one has no agent name);
        # a desktop session in the same folder is another session, not the main's twin
        kind = rec.get("kind") or ("cli" if not rec.get("agent") else "")
        own = _conversation(rec.get("session_id") or "")  # a #Main: that conversation only, no twin
        same = [x for x in mine if x.session_id == rec.get("session_id")
                or (not own and x.cwd and x.cwd == rec.get("cwd")
                and (x.agent == rec.get("agent") or not x.agent or not rec.get("agent"))
                and (not kind or x.kind == kind))]  # fmt: skip
        main = max((x for x in same if x.deliverable), key=lambda x: x.session_id == rec.get("session_id"),
                   default=None)  # fmt: skip
        if main is not None:
            if (
                router_cwd
            ):  # back: the routing context returns to it, once the router finished routing
                if not any(self.in_progress(x.session_id) for x in self.sessions.values()
                           if x.cwd == router_cwd):  # fmt: skip
                    self._merge_back(main, rec)
            elif rec.get("router_failed"):
                self.svc.store.put("channel_main", _norm(project), {k: v for k, v in rec.items()
                                   if k != "router_failed"})  # fmt: skip
            return main
        if rec.get("pending_new") or not same:  # offline: the operator decides (send_main_offline)
            return None
        routers = [
            x
            for x in self.sessions.values()
            if router_cwd and x.cwd == router_cwd and x.deliverable
        ]
        if routers:
            return max(routers, key=lambda x: x.mode == "push")
        if not router_cwd and not rec.get("router_failed"):  # failed once: the operator decides
            self._open_router(project, rec, same[0])
        return None

    def _repo(self, main: LiveSession) -> Path:
        """The project repo the main's lanes are copied from: its folder's, or for a main that was a
        child, the repo its copy came from. Raises ValueError outside a git repo."""
        rec = self.svc.store.get("channel_main", _norm(main.project)) or {}
        if rec.get("repo") and rec.get("cwd") == main.cwd:
            return Path(rec["repo"])
        return _toplevel(main.cwd)

    # ---- the gate: the Gateway Agent's own CLI session, kept alive by the gateway ----

    def gate_path(self) -> Path | None:
        """<gateway project folder>/.agw-children/gate (known once a session opened in the project)."""
        name = getattr(getattr(self.svc, "s", None), "gateway_agent_project", "") or "Agent Gateway"
        p = self.registry.resolve(name)
        return Path(p.root) / CHILDREN / "gate" if p and p.root else None

    def ensure_gate(self) -> None:
        """Watchdog (Settings.gate_cli): the Gateway Agent is a CLI session the gateway opens in the
        gate folder. Not connected (never opened, closed, crashed): opened again, at most once per
        START_WAIT; meanwhile requests are placed by default_lane, so nothing waits."""
        s = getattr(self.svc, "s", None)
        path = self.gate_path() if getattr(s, "gate_cli", False) else None
        if path is None:
            return
        st = self.gate_status()
        rec = self.svc.store.get("channel_setting", "gate") or {}
        self.svc.store.put("channel_setting", "gate", {**rec, "pid": st["pid"], "checked_at": now().isoformat(),
                           "healthy": st["healthy"], "session": st["session"]})  # fmt: skip
        if st["healthy"]:
            return
        dead = st["process_alive"] is False
        if dead:  # its Claude process ended: its sessions went with it, open another at once
            for x in [x for x in self.sessions.values() if x.cwd == str(path)]:
                self.disconnect(x, f"its Claude process {st['pid']} ended")
            self._ops(f"🧭 the gate's Claude process {st['pid']} ended")
        elif (rec.get("launched_at") or "") > (now() - START_WAIT).isoformat():
            return  # opening: give it time to connect
        self.svc.store.put("channel_setting", "gate", {**(self.svc.store.get("channel_setting", "gate") or {}),
                           "launched_at": now().isoformat(), "path": str(path), "pid": None})  # fmt: skip
        project = s.gateway_agent_project
        self._ops("🧭 opening the Gateway Agent's own session (gate)")

        def make() -> None:
            from app.config import ENV_PORTS, env_name
            from app.repo_setup import setup_repo

            repo = path.parent.parent
            (repo / CHILDREN).mkdir(exist_ok=True)
            (repo / CHILDREN / ".gitignore").write_text("*\n")
            path.mkdir(exist_ok=True)
            setup_repo(path, f"http://127.0.0.1:{ENV_PORTS[env_name()]}/mcp", project, False)
            _trust(path, repo)
            self.launch(path, "", self.models(project)["router"])

        self._background(make)

    def gate_status(self) -> dict:
        """The gate as the health check sees it: its Claude process (pid, alive) and its session."""
        path = self.gate_path()
        sess = [x for x in self.sessions.values() if path is not None and x.cwd == str(path)]
        live = [x for x in sess if x.deliverable]
        pid = next((x.claude_pid for x in sess if x.claude_pid), 0)
        alive = claude_alive(pid) if pid else None  # None: no pid reported yet
        gw = self.gateway_agent()
        rec = self.svc.store.get("channel_setting", "gate") or {}
        return {"path": str(path) if path else None, "pid": pid or None, "process_alive": alive,
                "session": live[0].session_id if live else None,
                "gateway_agent": bool(gw is not None and path is not None and gw.cwd == str(path)),
                "launched_at": rec.get("launched_at"),
                "healthy": bool(live) and alive is not False}  # fmt: skip

    def _maybe_gate(self, s: LiveSession) -> None:
        """A session that connects in the gate folder becomes the Gateway Agent (pinned main of the
        gateway project); the conversation that was the main keeps working as a dev worker."""
        path = (
            self.gate_path() if getattr(getattr(self.svc, "s", None), "gate_cli", False) else None
        )
        if path is None or s.cwd != str(path) or self.main_for(s.project) is s:
            return
        old = self.main_for(s.project)
        if old is not None and old.cwd == s.cwd:
            return  # the gate's twin (its push stream and its pull loop): already the main
        if old is not None and old.cwd:
            self.adopt(old, "dev")
        self.set_main(s, pin=True)
        self._ops(f"👑 {self._label(s)} is the Gateway Agent (gate)")

    def _open_router(self, project: str, rec: dict, main: LiveSession) -> None:
        """A temporary router for a stuck main, with the main's agent tool, inside the project."""
        try:
            repo = self._repo(main)
        except ValueError:
            return
        path = repo / CHILDREN / "router"
        self.svc.store.put("channel_main", _norm(project), {**rec, "router_cwd": str(path),
                           "routed": [], "router_since": now().isoformat()})  # fmt: skip
        self._ops(f"🧭 {self._label(main)} (👑 main) is stuck ({escape_html(main.stalled)}): opening a "
                  "temporary router with the same lanes until it works again")  # fmt: skip
        agent = main.agent if main.agent not in ("", "claude_code") else ""

        def make() -> None:
            from app.config import ENV_PORTS, env_name
            from app.repo_setup import setup_repo

            (repo / CHILDREN).mkdir(exist_ok=True)
            (repo / CHILDREN / ".gitignore").write_text("*\n")
            path.mkdir(exist_ok=True)
            setup_repo(path, f"http://127.0.0.1:{ENV_PORTS[env_name()]}/mcp", project, False)
            _trust(path, repo)
            wid = self.launch(path, agent, self.models(project)["router"])
            now_rec = self.svc.store.get("channel_main", _norm(project)) or {}
            if wid and now_rec.get("router_cwd") == str(path):
                self.svc.store.put(
                    "channel_main", _norm(project), {**now_rec, "router_window": wid}
                )

        self._background(make)

    def _remember_window(self, path: str, wid: int | None) -> None:
        """The child's Terminal window, closed when the child is closed."""
        if wid and (c := self.svc.store.get("channel_child", path)):
            self.svc.store.put("channel_child", path, {**c, "window": wid})

    def _router_window(self, s: LiveSession) -> int | None:
        rec = self.svc.store.get("channel_main", _norm(s.project)) or {}
        return rec.get("router_window") if s.cwd and s.cwd == rec.get("router_cwd") else None

    def _background(self, fn) -> None:
        """Run blocking work (git, opening a Terminal) off the event loop; failures are logged."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            fn()
            return

        async def go():
            try:
                await asyncio.to_thread(fn)
            except Exception:  # noqa: BLE001 - routing goes on; the operator sees nothing opened
                log.exception("background job failed")

        loop.create_task(go())

    def _merge_back(self, main: LiveSession, rec: dict) -> None:
        """The main works again: what the router routed goes to it with the lanes as they are now,
        its unrouted work comes back, and the router is closed."""
        router_cwd = rec.get("router_cwd") or ""
        self.svc.store.put("channel_main", _norm(main.project), {k: v for k, v in rec.items()
                           if k not in ("router_cwd", "routed", "router_since", "router_window")})  # fmt: skip
        for r in [x for x in self.sessions.values() if x.cwd == router_cwd]:
            for m in self.in_progress(r.session_id):  # routing work it took but did not route
                for k in ("session_id", "delivered_at"):
                    m.pop(k, None)
                self._set(m, "pending")
            if r.session_id in self.sessions:
                self.close(r)
        if wid := rec.get("router_window"):
            from app.launcher import close_terminal_window

            self._background(lambda: close_terminal_window(wid))
        routed = "; ".join(rec.get("routed") or []) or "nothing"
        mid = new_id("chm")
        self._enqueue(main, {
            "message_id": mid, "num": self._number("channel_mnum", mid), "project": main.project,
            "kind": "answer", "pinned": True, "meta": {"source": "gateway", "kind": "answer"},
            "text": f"While you were stuck, a temporary router routed for you: {routed}. "
                    f"{self.lanes_note(main.project)} Keep routing to these lanes (dispatch reopens a "
                    "closed lane's copy); do not open new ones for the same work. This needs no reply.",
            "created_at": now().isoformat(),
        })  # fmt: skip
        self._ops(f"👑 {self._label(main)} works again and routes; the temporary router is closed")
        self._flush_pending(main.project)

    def children(self, project: str) -> list[dict]:
        """The project's child folders (newest first) with lane, branch, task tags and status:
        working (on a request), idle (open), closed (no session; folder and branch kept), deleted."""
        out = []
        for c in self.svc.store.list("channel_child"):
            if _norm(c["project"]) != _norm(project) or not (path := c.get("path")):
                continue
            open_ = [x for x in self.sessions.values() if x.cwd == path]
            busy = [m for x in open_ for m in self.in_progress(x.session_id)]
            status = ("working" if busy else "idle" if open_
                      else "closed" if Path(path).is_dir() and not c.get("deleted") else "deleted")  # fmt: skip
            nums = ",".join(f"s{x.num}" for x in open_)
            tasks = c.get("tasks") or []
            task = f"m{busy[0].get('num')}" if busy else (tasks[-1]["tag"] if tasks else "")
            out.append({**c, "path": path, "status": status, "sessions": nums, "task": task})
        return sorted(out, key=lambda c: c.get("created_at", ""), reverse=True)

    def find_child(self, ref: str) -> dict | None:
        """A child by its folder name (ui-ab12) or lane, when exactly one is not deleted."""
        kids = [c for _, p in self.projects() for c in self.children(p) if c["status"] != "deleted"]
        hits = [c for c in kids if Path(c["path"]).name == ref] or [
            c for c in kids if c["lane"] == ref
        ]
        return hits[0] if len(hits) == 1 else None

    def prune_child(self, path: str) -> str:
        """Delete a child's folder only when nothing is lost: no open session in it, inside
        <repo>/.agw-children (symlinks resolved), no uncommitted tracked edits, and its HEAD is the
        branch tip in the project repo (fetched back). The branch stays; the record is marked
        deleted. The operator confirms with a button (Telegram). Raises ValueError when it refuses."""
        c = self.svc.store.get("channel_child", path)
        if c is None:
            raise ValueError(f"no child folder {path}")
        if c.get("deleted"):
            return "already deleted"
        if c.get("provider") == "adopted":
            raise ValueError("this is the project's own folder (an adopted worker), never deleted")
        if any(
            x.cwd == path or x.cwd.startswith(path + "/") for x in self.sessions.values() if x.cwd
        ):
            raise ValueError("a session is still open in it: /close it first")
        repo = Path(c.get("repo") or "/nonexistent").resolve()
        home, real = (repo / CHILDREN).resolve(), Path(path).resolve()
        if home.parent != repo or real.parent != home or Path(path).parent.resolve() != home:
            raise ValueError(f"not a folder inside {repo / CHILDREN}")
        branch = c["branch"]
        if real.is_dir():
            tip = _git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}")
            if not tip or tip != _git(real, "rev-parse", "--verify", "-q", "HEAD"):
                raise ValueError(f"branch {branch} is not in the project repo as the copy has it "
                                 "(not fetched back yet)")  # fmt: skip
            setup = {
                "CLAUDE.md",
                ".mcp.json",
                ".claude/",
                ".claude/settings.local.json",
            }  # gateway-made
            dirty = [ln[3:] for ln in (_git(real, "status", "--porcelain") or "").splitlines()
                     if ln[3:] not in setup]  # fmt: skip
            if dirty:  # tracked edits or new files never committed: they would be lost
                raise ValueError(f"it has uncommitted changes: {', '.join(dirty[:5])}")
            if (real / ".git").is_file():  # a git worktree: git drops it and its admin files
                _git(repo, "worktree", "remove", "--force", str(real))
            shutil.rmtree(real, ignore_errors=True)
            if real.exists():
                raise ValueError(f"could not delete {real}")
        self.svc.store.put("channel_child", path, {**c, "deleted": now().isoformat()})
        self.svc.store.event("channel.child_pruned", path=path, branch=branch)
        return f"deleted; branch {branch} kept in the project repo"

    def _tag(self, path: str, msg: dict) -> None:
        """Remember which request a child folder got: m# and its first words."""
        c = self.svc.store.get("channel_child", path)
        if c is None:
            return
        first = (msg.get("orig_text") or msg.get("text") or "").split("\n")
        words = next((ln for ln in first[1:] + first if ln.strip() and "Telegram" not in ln), "")
        tag = {
            "num": msg.get("num"),
            "tag": f"m{msg.get('num')} {words.strip()[:40]}",
            "at": now().isoformat(),
        }
        self.svc.store.put(
            "channel_child", path, {**c, "tasks": [*(c.get("tasks") or []), tag][-20:]}
        )

    def lanes_note(self, project: str) -> str:
        kids = [c for c in self.children(project) if c["status"] != "deleted"]
        lanes = ", ".join(f"{c['lane']} ({', '.join(x for x in (c['sessions'], c['status'], c['task']) if x)})"
                          for c in kids)  # fmt: skip
        return f"Lanes now: {lanes}." if lanes else "No lanes yet."

    def _route(self, msg: dict, exclude: str | None = None) -> LiveSession | None:
        """Where a message for no live session goes: an answer to its tool's session, work for a
        child being opened to that child, project work to the main session (with a routing note).
        The main offline: the operator is asked once whether to wait or open a new one."""
        project = msg["project"]
        if msg.get("kind") == "offer":  # an offer is never routed as work: it lapses
            self._set(msg, "cancelled")
            return None
        if msg.get("kind") == "answer":
            return self.session_for(project, exclude=exclude, agent=msg.get("for_agent"))
        if msg["message_id"] in self._opening or msg["message_id"] in self._bids:
            return (
                None  # its lane session is being opened, or its offers are out: placed when settled
            )
        if msg.get("wait_cwd"):
            kids = [x for x in self.sessions.values() if x.cwd == msg["wait_cwd"] and x.deliverable]
            return max(kids, key=lambda x: x.mode == "push", default=None)
        own = self.sessions.get(msg.get("for_session") or "")
        if (
            own is not None and own.deliverable and msg.get("pinned")
        ):  # back to the session it was for
            return own
        if msg.get("pinned") and msg.get("for_session"):  # that session is gone: its lane folder
            rec = self.svc.store.get("channel_session", msg["for_session"]) or {}
            if c := self.svc.store.get("channel_child", rec.get("cwd") or ""):
                msg.setdefault("lane", c["lane"])
                msg.setdefault("lane_path", c["path"])
        p = self.registry.resolve(project)
        if p is not None and p.routing_mode == "central":
            return self._route_central(msg, exclude)
        main = self.main_for(project)
        if main is not None and main.session_id == exclude:  # the main asked its own project
            return self.session_for(project, exclude=exclude)
        if main is None:
            rec = self.svc.store.get("channel_main", _norm(project)) or {}
            if (rec.get("session_id") or rec.get("pinned") or not self.auto_main()) and _norm(
                project
            ) not in self._main_asked:
                self._main_asked.add(_norm(project))
                self._notify("send_main_offline", project)
            return None
        self._mark_route(msg, main)
        return main

    # ---- central routing (projects with routing_mode "central") ----

    def gateway_agent(self) -> LiveSession | None:
        """The shared Gateway Agent: the main session of Settings.gateway_agent_project."""
        s = getattr(self.svc, "s", None)
        return self.main_for(getattr(s, "gateway_agent_project", "") or "Agent Gateway")

    def rule_lane(self, msg: dict) -> str | None:
        """Deterministic placement: a lane named on the message, else the first matching
        routing.channel_rules entry of config/routing/routing.yaml; None: no rule."""
        if msg.get("lane"):
            return msg["lane"]
        text = msg.get("orig_text") or msg.get("text", "")
        cfg = getattr(self.svc, "gateway_cfg", None)
        for r in cfg.routing.channel_rules if cfg else []:
            if fnmatch.fnmatch(_norm(msg["project"]), r.project.lower()) and re.search(
                r.match, text
            ):
                return _lane_name(r.lane)
        return None

    def _route_central(self, msg: dict, exclude: str | None) -> LiveSession | None:
        """The gateway places the work: a lane named on it or by a rule; else the Gateway Agent reads
        it and calls dispatch; with the Gateway Agent offline, the gateway picks the lane itself
        (default_lane), so no request waits for a router."""
        lane = self.rule_lane(msg)
        gw = None if lane else self.gateway_agent()
        if gw is not None and gw.session_id != exclude:
            if not msg.get("orig_text"):
                msg["orig_text"], msg["route"] = msg["text"], True
                msg["text"] += self._gateway_note(msg)
            return gw
        if lane is None:
            lane = self.default_lane(msg)
            if "gateway" not in self._main_asked:  # said once; the work does not wait for it
                self._main_asked.add("gateway")
                self._notify("send_main_offline", getattr(self.svc.s, "gateway_agent_project", ""))
        msg["lane"] = lane = self.lane_for(msg["project"], lane)
        if gw is None and self._offers_for(msg, lane) and (offers := self._ranked(msg, lane, "")):
            kw = dict(by="gateway", brief="", model="", agent="", main=None, skills=[])
            self._open_bidding(msg, lane, offers, kw)  # the gateway asks, no router needed
            return None
        kid = self._pick_child(msg["project"], lane, exclude, prefer=msg.get("lane_path"))
        if kid is not None:
            self._tag(kid.cwd, msg)
            return kid
        self._opening.add(msg["message_id"])
        self._set(msg, "pending")
        self._spawn(self._open_for(msg, lane))
        return None

    def kind(self, project: str) -> str:
        p = self.registry.resolve(project)
        return p.kind if p else "product"

    def default_lane(self, msg: dict) -> str:
        """The lane when nobody reads the request: a tool project's shared task lane; a product's
        deploy lane when the request says so, else its dev lane (its one worker)."""
        if self.kind(msg["project"]) == "tool":
            return "task"
        text = msg.get("orig_text") or msg.get("text", "")
        if re.search(
            r"(?i)\b(deploy|release|publish|testflight|build (ipa|apk|aab))\b|triển khai|phát hành",
            text,
        ):
            return "deploy"
        return "dev"

    def lane_for(self, project: str, lane: str) -> str:
        """A product has two sessions: its worker (dev: all its work, in order) and its deploy
        session; any other lane is the worker's. A tool project keeps the lanes it is given."""
        lane = _lane_name(lane)
        if self.kind(project) == "tool":
            return lane
        return "deploy" if lane == "deploy" else "dev"

    def _gateway_note(self, msg: dict) -> str:
        project = msg["project"]
        policy = (
            "It is a tool agent: its requests run in parallel, one session per topic, so pick a lane "
            "by topic: a follow-up or question about work in progress keeps that work's lane (it reaches "
            "the session doing it); a new topic gets a new lane."
            if self.kind(project) == "tool"
            else "It is a product: it has two sessions, its worker (lane dev: every kind of work, "
            "in order) and its deploy session (lane deploy: build, release, deploy); any other lane goes "
            "to dev. QA, UI/UX, PO and research are tool projects of their own (the worker asks them)."
        )
        return (
            f"\n\n(You are the Gateway Agent; this request is for the project {project}. Do not do it "
            f"yourself: call dispatch(message_id='{msg['message_id']}', lane=<lane>, brief=<what to do "
            f"and what to report>, session_id=<yours>). {policy} The lane's session works in that "
            f"project's own folder and answers the sender itself. {self.lanes_note(project)} "
            f"{self._skills_line(project)})"
        )

    def _skills_line(self, project: str) -> str:
        roles = "; ".join(f"{k}: {v.description}" for k, v in self.roles().items())
        head = (
            (
                f"Assign the worker a role with dispatch(role=...) when one fits ({roles}): its prompt and "
                "skills go with the request. "
            )
            if roles
            else ""
        )
        cat = self.skills_catalog(project)
        own = ", ".join(cat["project"]) or "none"
        return head + (
            f"Add skills with dispatch(skills=[...]) when needed: {project}'s "
            f"own skills: {own}; shared: {', '.join(cat['global']) or 'none'}; other projects' "
            "skills as <project>/<skill> (list_skills shows them; they are imported into the lane)."
        )

    def _pick_child(self, project: str, lane: str, exclude: str | None = None,
                    prefer: str | None = None, skip=frozenset()) -> LiveSession | None:  # fmt: skip
        """The open session to give a lane's work to, or None to open (or reopen) one. A lane with a
        session keeps it (its context): a product's dev and deploy, and a tool project's topic lanes,
        each have one session that takes its work in order (the one that worked last, when an older
        version left several). Only a tool project's untyped lane "task" spreads out: an idle session,
        else another one while the project has fewer than Settings.tool_parallel open, else the least
        loaded one."""
        main = self.main_for(project)
        everyone = [x for x in self.sessions.values() if x.deliverable and x.session_id != exclude
                    and x is not main and _norm(x.project) == _norm(project) and self.child(x)]  # fmt: skip
        kids = [x for x in everyone if x.session_id not in skip]  # skip: they said no to an offer
        same = [x for x in kids if self._in_lane(self.child(x), lane)]
        if prefer and (mine := [x for x in same if x.cwd == prefer]):
            same = mine
        if self.kind(project) != "tool" or (same and lane != "task"):
            return max(same, key=lambda x: x.last_work, default=None)
        if idle := [x for x in same if self._idle(x)]:
            return max(idle, key=lambda x: x.mode == "push")
        cap = int(getattr(getattr(self.svc, "s", None), "tool_parallel", 3) or 3)
        if len({x.cwd for x in everyone}) < cap:
            return None  # room for one more session
        pool = same or kids or everyone
        load = lambda x: x.queue.qsize() + len(self.in_progress(x.session_id))  # noqa: E731
        return min(pool, key=load, default=None)

    def _spawn(self, coro) -> None:
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:  # no loop (a sync caller): the watchdog's next flush tries again
            coro.close()
            return
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def _open_for(self, msg: dict, lane: str) -> None:
        try:
            await self._to_lane(msg, lane, by="gateway")
        except Exception as e:  # noqa: BLE001 - the operator decides what happens to it
            log.warning("central routing of m%s to %s failed: %s", msg.get("num"), lane, e)
            self._stall([msg], "", f"the gateway could not open the {lane} lane: {e}")
        finally:
            self._opening.discard(msg["message_id"])

    def _mark_route(self, msg: dict, main: LiveSession) -> None:
        """The main session only routes: work reaching it through the gateway (for the project, or
        addressed to it: /send s#, a reply to its report, an ask) carries the routing note."""
        project = msg["project"]
        if not msg.get("orig_text"):
            msg["orig_text"], msg["route"] = msg["text"], True
            rec = self.svc.store.get("channel_main", _norm(project)) or {}
            who = (
                "the temporary router of"
                if rec.get("router_cwd") == main.cwd
                else "the main session of"
            )
            msg["text"] += (
                f"\n\n(You are {who} {project}: route this, do not do it yourself. "
                f"Call dispatch(message_id='{msg['message_id']}', lane=<task type, e.g. ui, build, "
                "art, bugfix-login>, brief=<what to do and what to report>, session_id=<yours>). "
                "The same lane goes to its child session in order; a new lane opens a new child "
                "session in its own copy of the project. The child answers the sender itself. "
                f"{self.lanes_note(project)})"
            )

    async def dispatch(
        self, session_id: str, ref: str, lane: str, brief: str = "", to: str = "", model: str = "",
        skills: list[str] = (), role: str = "",
    ) -> dict:  # fmt: skip
        """The main session hands a message to a child: `to` (s#), else the open child of that
        lane, else a new child opened in a new git worktree with the main's agent tool."""
        session_id = self.as_main(session_id)
        main = self.sessions.get(session_id)
        msg = next((m for st in ("delivered", "queued") for m in self.svc.store.list("channel_msg", states=[st])
                    if ref in (m["message_id"], f"m{m.get('num')}")), None)  # fmt: skip
        if main is None:
            raise ValueError(f"unknown session {session_id}: call get_messages first")
        if msg is None:
            raise ValueError(f"no open message {ref}")
        rec = self.svc.store.get("channel_main", _norm(msg["project"])) or {}
        router = (
            bool(rec.get("router_cwd")) and main.cwd == rec["router_cwd"]
        )  # finishing its routing
        central = (
            p := self.registry.resolve(msg["project"])
        ) is not None and p.routing_mode == "central"
        if central:
            if main is not self.gateway_agent():
                raise PermissionError(
                    f"{msg['project']} is routed by the gateway: only the Gateway Agent dispatches its work"
                )
        elif self.main_for(msg["project"]) is not main and not router:
            raise PermissionError(f"only the main session of {msg['project']} routes its work")
        if msg.get("session_id") not in (None, session_id):
            raise PermissionError(f"m{msg.get('num')} is not yours to route")
        rec = self.svc.store.get("channel_main", _norm(msg["project"])) or {}
        if (
            rec.get("router_cwd") and rec["router_cwd"] == main.cwd
        ):  # told to the main when it is back
            self.svc.store.put("channel_main", _norm(msg["project"]), {**rec, "routed":
                               [*rec.get("routed", []), f"m{msg.get('num')} → {lane}"]})  # fmt: skip
        repo = None if central else main
        rd = self.role(role)  # refused before anything moves, like unknown skills
        chosen = self.resolve_skills(msg["project"], [*(rd.skills if rd else []), *skills])
        if rd is not None:
            msg["role"], msg["role_prompt"] = role, rd.prompt
        kw = dict(
            by=session_id, brief=brief, model=model, agent=main.agent, main=repo, skills=chosen
        )
        lane = self.lane_for(msg["project"], lane)
        if (
            not to
            and self._offers_for(msg, lane)
            and (offers := self._ranked(msg, lane, session_id))
        ):
            return self._open_bidding(msg, lane, offers, kw)
        return await self._to_lane(msg, lane, to=to, **kw)

    # ---- offer/bid: a new topic of a tool project in bid_projects is offered to its idle sessions ----

    def bids_on(self, project: str) -> bool:
        sw = getattr(self.svc, "switches", None)
        if sw is not None and not sw.on("bid"):
            return False  # bid switched off in Telegram /mcp: placed directly
        names = getattr(getattr(self.svc, "s", None), "bid_projects", None) or []
        return _norm(project) in {_norm(n) for n in names}

    def _offers_for(self, msg: dict, lane: str) -> bool:
        """Offers are made for a new topic of a tool project in bid_projects only: a lane that has a
        session keeps it (its context), and a product's two sessions are never auctioned."""
        project = msg["project"]
        if self.kind(project) != "tool" or not self.bids_on(project):
            return False
        if lane != "task" and any(x.deliverable and _norm(x.project) == _norm(project)
                                  and self._in_lane(self.child(x), lane) for x in self.sessions.values()):  # fmt: skip
            return False
        msg.setdefault("request", msg.get("orig_text") or msg["text"])
        return True

    @staticmethod
    def _in_lane(c: dict | None, lane: str) -> bool:
        """A child record of `lane`: its own lane, or a topic it took through an offer."""
        return bool(c) and (c.get("lane") == lane or lane in (c.get("aliases") or []))

    def _history(self, session_id: str) -> list[dict]:
        """The requests a session took (most recent last), offers and answers left out."""
        mine = [m for m in self.svc.store.list("channel_msg")
                if session_id in (m.get("session_id"), m.get("queued_for"))
                and m.get("kind") not in ("answer", "offer")]  # fmt: skip
        return sorted(mine, key=lambda m: m.get("created_at", ""))[-8:]

    @staticmethod
    def _words(text: str | None) -> set[str]:
        body = (text or "").split("\n\n", 1)[0]  # the request itself, before the gateway's notes
        body = re.sub(r"^[^:\n]{0,120}:\s*", "", body)  # its "Request from ... (Telegram):" header
        return set(re.findall(r"\w{3,}", body.lower()))

    def context_score(self, s: LiveSession, msg: dict, lane: str) -> float:
        """How much of what `s` already did fits `msg`: 2 for its lane, plus up to 4 for the share of
        the request's words found in the requests it took lately."""
        want = self._words(msg.get("request") or msg.get("orig_text") or msg["text"])
        seen = set().union(*(self._words(m.get("request") or m.get("text"))
                             for m in self._history(s.session_id)))  # fmt: skip
        overlap = len(want & seen) / len(want) if want else 0.0
        return round(2 * self._in_lane(self.child(s), lane) + 4 * overlap, 2)

    def _ranked(self, msg: dict, lane: str, by: str) -> list[LiveSession]:
        """The sessions to offer `msg` to, best context first, at most Settings.offer_max, one per
        folder (a Terminal child's push and pull twins are one conversation). Not asked: a folder
        with any work (it counts as a no), one reserved by a placement, one that left an offer
        unanswered within CHILD_IDLE (it may have no bid tool), and the project's own folder (an
        adopted former main: it says no to every offer, seen in prod), which keeps its own lane."""
        quiet, folders = now() - CHILD_IDLE, {}
        for x in self.sessions.values():
            if (_norm(x.project) == _norm(msg["project"]) and x.session_id != by
                    and (self.child(x) or {}).get("provider") not in (None, "adopted")
                    and x is not self.main_for(msg["project"])):  # fmt: skip
                folders.setdefault(x.cwd, []).append(x)
        free = [max(twins, key=lambda x: x.mode == "push") for twins in folders.values()
                if all(x.deliverable and self._idle(x) and not self._busy(x)
                       and x.session_id not in self._reserved
                       and not (x.silent_at and x.silent_at > quiet) for x in twins)]  # fmt: skip
        free.sort(key=lambda x: self.context_score(x, msg, lane), reverse=True)
        return free[: int(getattr(self.svc.s, "offer_max", 3) or 3)]

    def _open_bidding(self, msg: dict, lane: str, offers: list[LiveSession], kw: dict) -> dict:
        mid = msg["message_id"]
        self._bids[mid], self._bid_ev[mid] = {}, asyncio.Event()
        for k in ("session_id", "delivered_at", "route"):  # the router is done with it
            msg.pop(k, None)
        # stored as asked, in its lane: after a restart (offers forgotten) it is placed there directly
        msg["text"], msg["lane"] = msg.pop("orig_text", msg["text"]), lane
        self._set(msg, "pending")
        for s in offers:
            self._offer(s, msg, lane)
        self._spawn(self._settle(msg, lane, offers, kw))
        wait = int(getattr(self.svc.s, "offer_wait_s", 90) or 90)
        self._ops(f"🙋 m{msg.get('num')} offered to " + ", ".join(self._label(s) for s in offers))
        return {"to": "offered", "lane": lane, "offers": [f"s{s.num}" for s in offers],
                "note": f"offered to the idle sessions; placed within {wait} s (a yes with the most "
                        "context wins, else a new session). Its follow-ups keep this lane name. "
                        "Nothing more to do."}  # fmt: skip

    def _offer(self, s: LiveSession, msg: dict, lane: str) -> dict:
        oid = new_id("chm")
        o = {"message_id": oid, "project": s.project, "kind": "offer", "offer_for": msg["message_id"],
             "offer_to": s.session_id, "created_at": now().isoformat(),
             "meta": {"source": "gateway", "sender": "the gateway", "project": s.project, "kind": "offer",
                      "message_id": oid},
             "text": (f"🙋 Offer from the gateway (not a task yet), lane {lane}:\n"
                      f"{truncate(msg.get('request') or msg['text'], 600)}\n\nDo not start it. Answer at "
                      f"once with bid(offer_id='{oid}', accept=true|false, context='high'|'medium'|'low'|"
                      f"'none', reason=<one line>, session_id='{s.session_id}'); without the bid tool, "
                      f"reply_message(message_id='{oid}', text='YES <context>' or 'NO <why>'). Accept only "
                      "if you are free to do it now; context = how much of what you already did in this "
                      "session it needs. If you win, the request arrives through get_messages.")}  # fmt: skip
        o["num"] = self._number("channel_mnum", oid)
        self._enqueue(s, o)
        return o

    BID_BONUS = {"high": 3, "medium": 2, "low": 1}

    def bid(self, session_id: str, offer_id: str, accept: bool, context: str = "none",
            reason: str = "") -> dict:  # fmt: skip
        o = self.svc.store.get("channel_msg", offer_id)
        if o is None or o.get("kind") != "offer":
            raise ValueError(f"no offer {offer_id}")
        folder = (self.svc.store.get("channel_session", o["offer_to"]) or {}).get("cwd")
        me = self.sessions.get(session_id) or self.sessions.get(self.as_main(session_id))
        if (o["offer_to"] not in (session_id, self.as_main(session_id))
                and not (me is not None and folder and me.cwd == folder)):  # fmt: skip
            raise PermissionError("that offer was made to another session")
        if me is not None:
            me.silent_at = None
        bids = self._bids.get(o["offer_for"])
        if bids is None or o.get("state") == "cancelled":
            return {"note": "too late: the request was already placed. Nothing to do."}
        bids[o["offer_to"]] = {"accept": bool(accept), "context": (context or "none").lower(),
                               "reason": (reason or "")[:200]}  # fmt: skip
        self._set(o, "done")
        self._bid_ev[o["offer_for"]].set()
        return {
            "note": "noted. If it is yours, it arrives through get_messages; otherwise nothing to do."
        }

    def _bid_by_reply(self, session_id: str, offer_id: str, text: str) -> dict:
        """An offer answered with reply_message (a session without the bid tool): YES/NO and context."""
        head = text.strip().lower()[:80]
        accept = not re.search(r"\b(no|decline|declined|busy)\b|không|từ chối|bận", head)
        context = next((c for c in ("high", "medium", "low") if c in head), "none")
        return self.bid(session_id, offer_id, accept, context, text)

    async def _settle(self, msg: dict, lane: str, offers: list[LiveSession], kw: dict) -> None:
        """Wait for the bids (all in, or Settings.offer_wait_s), then place `msg`: the yes from a
        session still free with the most context (gateway score plus the session's own estimate),
        else as dispatch would (a new session while the project is under tool_parallel); the sessions
        that said no or kept silent are not given it."""
        mid = msg["message_id"]
        bids, ev = self._bids[mid], self._bid_ev[mid]
        end = time.monotonic() + float(getattr(self.svc.s, "offer_wait_s", 90) or 90)
        win = None
        try:
            while len(bids) < len(offers) and (left := end - time.monotonic()) > 0:
                ev.clear()
                try:
                    await asyncio.wait_for(ev.wait(), left)
                except TimeoutError:
                    break
        finally:
            self._opening.add(mid)  # still guarded until it is queued: a flush would route it again
            self._bids.pop(mid, None)
            self._bid_ev.pop(mid, None)
        try:
            for s in offers:  # offers nobody took off the queue: withdrawn
                for o in [o for o in s.queue._queue if o.get("offer_for") == mid]:
                    self._take(s, o)
                    self._set(o, "cancelled")
                if s.session_id not in bids:
                    s.silent_at = now()  # not asked again for a while
            if (self.svc.store.get("channel_msg", mid) or {}).get("state") == "cancelled":
                return  # cancelled on Telegram meanwhile
            yes = [s for s in offers if bids.get(s.session_id, {}).get("accept") and s.deliverable
                   and self.sessions.get(s.session_id) is s and self._idle(s)
                   and s.session_id not in self._reserved]  # fmt: skip
            win = max(yes, default=None, key=lambda s: self.context_score(s, msg, lane)
                      + self.BID_BONUS.get(bids[s.session_id]["context"], 0))  # fmt: skip
            if win is not None:
                self._reserved.add(win.session_id)
                c = self.child(win) or {}
                if c.get("lane") not in (None, lane) and lane != "task":  # its follow-ups find it
                    aliases = sorted({*(c.get("aliases") or []), lane})
                    self.svc.store.put("channel_child", win.cwd, {**c, "aliases": aliases})
                lane = c.get("lane") or lane
            self.svc.store.event("channel.bids", message_id=mid, bids=bids,
                                 winner=win and win.session_id)  # fmt: skip
            await self._to_lane(msg, lane, to=f"s{win.num}" if win else "",
                                skip={s.session_id for s in offers}, **kw)  # fmt: skip
        except Exception as e:  # noqa: BLE001 - the operator decides what happens to it
            log.warning("placing m%s after its offers failed: %s", msg.get("num"), e)
            self._stall([msg], "", f"the gateway could not place it after the offers: {e}")
        finally:
            self._opening.discard(mid)
            if win is not None:
                self._reserved.discard(win.session_id)

    def roles(self) -> dict:
        cfg = getattr(self.svc, "gateway_cfg", None)
        return dict(cfg.routing.roles) if cfg else {}

    def role(self, name: str):
        if not name:
            return None
        rd = self.roles().get(name)
        if rd is None:
            raise ValueError(
                f"unknown role {name!r}: one of {', '.join(self.roles()) or 'none configured'}"
            )
        return rd

    def task_brief(self, message_id: str, session_id: str) -> dict:
        """What a lane session must know when it starts a request (get_task_brief): its role and the
        role's prompt, the skills it must use (with versions), the mandatory rules and commands, the
        routing brief, and the request itself. Only for the session that holds it."""
        msg = self.svc.store.get("channel_msg", message_id)
        if msg is None:
            raise ValueError(f"no message {message_id}")
        holders = {msg.get("session_id"), msg.get("queued_for"), msg.get("for_session")}
        if session_id not in holders and self.as_main(session_id) not in holders:
            raise PermissionError(f"m{msg.get('num')} is not yours")
        return {"message_id": message_id, "project": msg["project"], "lane": msg.get("lane"),
                "role": msg.get("role"), "role_prompt": msg.get("role_prompt"),
                "skills": msg.get("skill_refs") or [], "required_skills": msg.get("required_skills") or [],
                "required_commands": msg.get("required_commands") or [], "rules": msg.get("rules") or [],
                "brief": msg.get("brief"),
                "request": msg.get("request") or msg.get("orig_text") or msg["text"]}  # fmt: skip

    def _known_projects(self) -> list[tuple[str, str]]:
        return [(p.name, p.root) for p in self.registry.all() if p.root]

    def resolve_skills(self, project: str, names: list[str]) -> list:
        from app.skills import resolve

        return resolve(names, project, self._known_projects()) if names else []

    def skills_catalog(self, project: str) -> dict:
        """What the Gateway Agent may name for a request of `project`: its own skills, the shared ones,
        and other projects' skills (named <project>/<skill>; imported into the lane when used)."""
        from app.skills import global_skills, project_skills

        out = {"project": [], "global": [s.name for s in global_skills()], "other": {}}
        for name, root in self._known_projects():
            names = [s.name for s in project_skills(name, root)]
            if _norm(name) == _norm(project):
                out["project"] = names
            elif names:
                out["other"][name] = names
        return out

    def _with_skills(self, msg: dict, skills: list, folder: str) -> None:
        """The chosen skills on the message (recorded with versions), external ones imported into the
        lane's folder first: a lane always accepts a skill from another project."""
        if msg.get("role") and "MANDATORY role" not in msg["text"]:
            msg["text"] += (f"\n\nMANDATORY role: {msg['role']}. {msg.get('role_prompt') or ''} Call "
                            f"get_task_brief(message_id='{msg['message_id']}', session_id=<yours>) first: "
                            "it gives your role, skills, rules and brief.")  # fmt: skip
        if not skills:
            return
        from app.skills import import_into, note

        for s in skills:
            if import_into(s, folder):
                self._ops(f"📚 skill {s.name} ({s.source}) imported into {Path(folder).name}")
        msg["skill_refs"] = [s.ref() for s in skills]
        msg["required_skills"] = list(
            dict.fromkeys([*msg.get("required_skills", []), *(s.name for s in skills)])
        )
        msg["text"] += note(skills)

    async def _to_lane(self, msg: dict, lane: str, *, by: str, brief: str = "", to: str = "",
                       model: str = "", agent: str = "", main: LiveSession | None = None,
                       skills: list = (), skip=frozenset()) -> dict:  # fmt: skip
        """Hand `msg` to a lane: `to` (s#), else the open child of that lane, else a closed child of
        it reopened, else a new child in its own copy of the project. by: who routes it (a main
        session, the Gateway Agent, or "gateway" for a rule). main: the project's own main (its
        folder gives the repo); without it, the project folder from the registry."""
        lane = self.lane_for(msg["project"], lane)
        kids = [x for x in self.sessions.values() if x.session_id != by and x.deliverable
                and _norm(x.project) == _norm(msg["project"])
                and x is not self.main_for(msg["project"])]  # fmt: skip
        if to:
            target = next((x for x in kids if f"s{x.num}" == to.lower()), None)
            if target is None:
                raise ValueError(f"{to} is not an open session of {msg['project']}")
        else:
            target = self._pick_child(msg["project"], lane, by, prefer=msg.get("lane_path"),
                                      skip=skip)  # fmt: skip
        for x in self.sessions.values():  # still queued somewhere (a main): taken out first
            for m in [m for m in x.queue._queue if m["message_id"] == msg["message_id"]]:
                self._take(x, m)
        for k in ("session_id", "delivered_at", "wait_cwd", "routing"):
            msg.pop(k, None)
        who = "the gateway (a routing rule)" if by == "gateway" else "the routing session"
        note = f"\n\nBrief from {who} (lane {lane}): {brief}" if brief else ""
        msg["brief"] = brief or None
        msg.pop("route", None)
        msg.setdefault("request", msg.get("orig_text") or msg["text"])  # as asked, before any notes
        msg.update(text=msg.pop("orig_text", msg["text"]) + note, lane=lane, pinned=True,
                   dispatched_by=by)  # fmt: skip
        fresh = lane == "deploy" and self.kind(msg["project"]) != "tool"  # never an old copy

        async def give(t: LiveSession) -> dict:
            self._tag(t.cwd, msg)
            await asyncio.to_thread(self._with_skills, msg, list(skills), t.cwd)
            if fresh:
                msg["text"] += self._redeploy_note(msg["project"], t.cwd)
            self._enqueue(t, msg)
            self._assigned(msg, t)
            self._ops(f"🧭 m{msg.get('num')} → {self._label(t)} ({lane})")
            return {"to": f"s{t.num}", "lane": lane, "new_session": False}

        if target is not None:
            return await give(target)
        # one opening per lane (a tool project's "task" lane excepted): a request that waited for it
        # joins the session just opened (or still starting) instead of opening another
        sticky = self.kind(msg["project"]) != "tool" or lane != "task"
        lock = self._lane_locks.setdefault(f"{_norm(msg['project'])}/{lane}", asyncio.Lock())
        self._opening.add(msg["message_id"])
        try:
            async with lock if sticky else contextlib.nullcontext():
                if sticky and (t := self._pick_child(msg["project"], lane, by, skip=skip)):
                    return await give(t)
                if sticky and (path := self._starting(msg["project"], lane)):
                    self._tag(path, msg)
                    await asyncio.to_thread(self._with_skills, msg, list(skills), path)
                    if fresh:
                        msg["text"] += self._redeploy_note(msg["project"], path)
                    msg["wait_cwd"], msg["waiting_since"] = path, now().isoformat()
                    self._set(msg, "pending")
                    self._ops(f"🧭 m{msg.get('num')} → the {lane} session being opened")
                    return {"to": "starting", "lane": lane, "new_session": False, "path": path}
                self._set(msg, "pending")  # held for the new child (wait_cwd below)
                return await self._open_lane(msg, lane, by=by, model=model, agent=agent, main=main,
                                             skills=skills, fresh=fresh)  # fmt: skip
        finally:
            self._opening.discard(msg["message_id"])

    def _starting(self, project: str, lane: str) -> str | None:
        """The folder of `lane` launched less than START_WAIT ago whose session has not connected yet."""
        cutoff = (now() - START_WAIT).isoformat()
        return next((c["path"] for c in self.children(project) if self._in_lane(c, lane)
                     and c["status"] == "closed" and (c.get("launched_at") or "") > cutoff), None)  # fmt: skip

    def _redeploy_note(self, project: str, path: str) -> str:
        c = self.svc.store.get("channel_child", path) or {}
        how = (f"git checkout --detach {c['target']}: this folder is a git worktree of the project"
               if c.get("provider") == "worktree" and c.get("target")
               else "copy it again from the project folder")  # fmt: skip
        return (
            f"\n\n(Deploy session of {project}, used again: first bring this folder to the project's "
            f"current code ({how}), then build, test and deploy, and report the result yourself.)"
        )

    async def _open_lane(self, msg: dict, lane: str, *, by: str, model: str, agent: str,
                         main: LiveSession | None, skills: list, fresh: bool) -> dict:  # fmt: skip
        """Reopen the lane's closed folder, else a new child in its own copy of the project."""
        closed = [c for c in self.children(msg["project"])
                  if self._in_lane(c, lane) and c["status"] == "closed" and not fresh]  # fmt: skip
        old = next((c for c in closed if c["path"] == msg.get("lane_path")), None) or next(
            iter(closed), None
        )
        if old is not None:  # the lane's child closed: reopen it on its folder, no new copy
            # its new session has the old one's id: a close still pending for that id would stop it
            self._stopping -= {sid for sid, r in self.svc.store.items("channel_session")
                               if r.get("cwd") == old["path"]}  # fmt: skip
            self._tag(old["path"], msg)
            await asyncio.to_thread(self._with_skills, msg, list(skills), old["path"])
            if model:  # reopened with another model: it keeps that one
                self.svc.store.put("channel_child", old["path"], {**old, "model": model})
            msg["wait_cwd"], msg["waiting_since"] = old["path"], now().isoformat()
            self._set(msg, "pending")
            await asyncio.to_thread(_trust, Path(old["path"]), Path(old.get("repo") or ""))
            if old.get("repo") and await asyncio.to_thread(
                _carry, Path(old["repo"]), Path(old["path"])
            ):
                from app.config import ENV_PORTS, env_name
                from app.repo_setup import setup_repo  # rules changed in the project since: again

                url = f"http://127.0.0.1:{ENV_PORTS[env_name()]}/mcp"
                await asyncio.to_thread(setup_repo, Path(old["path"]), url, msg["project"], False)
            tool = old.get("agent", "") if old.get("agent") not in ("", "claude_code") else ""
            model = model or old.get("model") or self.models(msg["project"])["child"]
            wid = await asyncio.to_thread(self.launch, Path(old["path"]), tool, model)
            self._remember_window(old["path"], wid)
            self.svc.store.put(
                "channel_child",
                old["path"],
                {
                    **(self.svc.store.get("channel_child", old["path"]) or old),
                    "launched_at": now().isoformat(),
                },
            )
            self._ops(
                f"🧭 m{msg.get('num')} → reopened child ({lane}) on <code>{old['branch']}</code>"
            )
            return {"to": "reopened", "lane": lane, "new_session": False, "branch": old["branch"],
                    "path": old["path"]}  # fmt: skip
        try:
            repo = await asyncio.to_thread(self._lane_repo, msg["project"], main)
            free, started = shutil.disk_usage(repo).free, time.monotonic()
            path, branch, base, target, provider = await asyncio.to_thread(_worktree, repo, lane)
        except ValueError:  # no git repo, worktree failed: it stays with whoever routed it
            if by in self.sessions:
                msg.update(session_id=by, delivered_at=now().isoformat())
                self._set(msg, "delivered")
            raise
        self.svc.store.event("channel.child_created", path=str(path), branch=branch, provider=provider,
                             seconds=round(time.monotonic() - started, 2), free_before=free,
                             free_after=shutil.disk_usage(repo).free)  # fmt: skip
        from app.config import ENV_PORTS, env_name
        from app.repo_setup import setup_repo

        url = f"http://127.0.0.1:{ENV_PORTS[env_name()]}/mcp"
        await asyncio.to_thread(_carry, Path(repo), path)  # (then the gateway section on top)
        await asyncio.to_thread(setup_repo, path, url, msg["project"], False)
        self.svc.store.put("channel_child", str(path), {"project": msg["project"], "lane": lane,
                           "branch": branch, "repo": str(repo), "parent": by, "path": str(path),
                           "agent": agent or "claude_code", "created_at": now().isoformat(),
                           "base": base, "target": target, "provider": provider})  # fmt: skip
        self._tag(str(path), msg)
        await asyncio.to_thread(self._with_skills, msg, list(skills), str(path))
        if fresh:
            msg["text"] += (
                f"\n\n(Deploy session: this folder is a fresh copy of {msg['project']} at commit "
                f"{(base or 'the current files')[:12]}, made for this deploy only. Build, test and deploy "
                "from it; do not change the project's main folder or other lanes. When done, report "
                "the result yourself (reply_message / submit_report with the build link).)"
            )
        msg["wait_cwd"], msg["waiting_since"] = str(path), now().isoformat()
        self._set(msg, "pending")
        await asyncio.to_thread(_trust, path, repo)
        tool = agent if agent not in ("", "claude_code") else ""
        model = model or self.models(msg["project"])["child"]
        c = self.svc.store.get("channel_child", str(path)) or {}
        self.svc.store.put(
            "channel_child", str(path), {**c, "model": model, "launched_at": now().isoformat()}
        )
        self._remember_window(str(path), await asyncio.to_thread(self.launch, path, tool, model))
        where = f"on branch <code>{branch}</code>" if branch else "in a copy of the folder (no git)"
        self._ops(f"🧭 m{msg.get('num')} → new child ({lane}) {where}")
        return {"to": "new", "lane": lane, "new_session": True, "branch": branch, "path": str(path)}

    def _lane_repo(self, project: str, main: LiveSession | None) -> Path:
        """The folder a new lane is copied from: the main's repo (main mode), else the project folder
        the registry remembers from its sessions; a folder that is not a git repo is copied as is."""
        from app.projects import repo_for

        if main is not None:
            try:
                return self._repo(main)
            except ValueError:
                return repo_for(Path(main.cwd)) or Path(main.cwd)
        p = self.registry.resolve(project)
        if p is None or not p.root:
            raise ValueError(
                f"the folder of {project} is not known yet (no session has opened there)"
            )
        try:
            return _toplevel(p.root)
        except ValueError:
            return repo_for(Path(p.root)) or Path(p.root)

    def models(self, project: str) -> dict:
        """Claude models for the project's child sessions and temporary router: /model, else the
        gateway's defaults (child_model, router_model). The main picks another per task."""
        s = getattr(self.svc, "s", None) or getattr(self.svc, "settings", None)
        rec = self.svc.store.get("channel_model", _norm(project)) or {}
        return {"child": rec.get("child") or getattr(s, "child_model", "") or "opus",
                "router": rec.get("router") or getattr(s, "router_model", "") or "sonnet"}  # fmt: skip

    def set_model(self, project: str, role: str, model: str) -> dict:
        """/model p# child|router <model|default>."""
        if role not in ("child", "router"):
            raise ValueError("role is child or router")
        if model != "default" and not re.fullmatch(
            r"(opus|sonnet|haiku|claude-[a-z0-9.-]+)", model
        ):
            raise ValueError("model: opus, sonnet, haiku, a claude-… id, or default")
        rec = self.svc.store.get("channel_model", _norm(project)) or {}
        rec = {**rec, "project": project, role: None if model == "default" else model}
        self.svc.store.put("channel_model", _norm(project), rec)
        return self.models(project)

    def _busy(self, s: LiveSession) -> bool:
        """Said still_working lately, or has work waiting for the operator (stalled): not idle."""
        if s.busy_until and s.busy_until > now():
            return True
        return any(m.get("stalled_session") == s.session_id
                   for m in self.svc.store.list("channel_msg", states=["stalled"]))  # fmt: skip

    def still_working(self, session_id: str, note: str = "") -> dict:
        """Long work: the agent says it is still on it. Its open requests get a fresh lease (not
        stuck), and the session counts as busy for BUSY (a child is not closed as idle)."""
        s = self.sessions.get(session_id)
        if s is None:
            raise KeyError(session_id)
        s.last_seen = s.last_work = now()
        s.busy_until = now() + BUSY
        renewed = []
        for m in self.svc.store.list("channel_msg", states=["delivered"]):
            if m.get("session_id") == session_id:
                m["delivered_at"] = now().isoformat()
                self._set(m, "delivered")
                renewed.append(m["message_id"])
        self.svc.store.event(
            "channel.still_working", session=session_id, note=note[:200], renewed=renewed
        )
        return {"busy_until": s.busy_until.isoformat(), "renewed": renewed}

    def _close_idle_children(self) -> None:
        """A child with no work for CHILD_IDLE is closed; its branch and worktree stay for review."""
        cutoff = now() - CHILD_IDLE
        for s in list(self.sessions.values()):
            c = self.child(s)
            if (
                c is None or c.get("provider") == "adopted"
            ):  # a user's own conversation: never closed
                continue
            # a Terminal child is a push session plus its pull loop, one Claude process: the work goes
            # to either, so the child is idle only when all of them are (seen in prod: the push twin
            # looked idle and closing it killed the loop mid-task)
            twins = [x for x in self.sessions.values() if x.cwd == s.cwd]
            if any(x.last_work > cutoff or not self._idle(x) or self._busy(x) for x in twins):
                continue
            if s.mode == "pull" and any(x is not s and x.cwd == s.cwd and x.mode == "push"
                                        for x in self.sessions.values()):  # fmt: skip
                continue  # the push twin is closed, which takes this one along
            self.close(s)
            self._ops(f"🧹 {self._label(s)} ({c['lane']}) closed after "
                      f"{int(CHILD_IDLE.total_seconds() // 60)} min idle; branch "
                      f"<code>{c['branch']}</code> kept for review")  # fmt: skip

    def _assigned(self, msg: dict, target: LiveSession) -> None:
        """An agent's request changed hands: its ask follows (watchdog, attribution)."""
        if msg.get("ask_id") and msg.get("kind") == "request":
            self.svc.asks.note_delivery(msg["ask_id"], target.session_id)

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
        key = (extra or {}).get("idempotency_key")
        if key and (same := next((m for m in self.svc.store.list("channel_msg")
                                  if m.get("idempotency_key") == key
                                  and m.get("state") not in ("cancelled", "expired")), None)):  # fmt: skip
            # sent before (a retried call): the same message, not a second job
            holder = same.get("session_id") or same.get("queued_for") or same.get("for_session")
            return same, self.sessions.get(holder or "")
        exclude = (extra or {}).get("from_session")
        target = self.sessions.get(session_id or "")
        # an answer belongs to the agent tool that asked: never to another tool's session of the
        # project (a Claude question answered into a Codex thread); it waits for that tool instead
        rec = self.svc.store.get("channel_session", session_id or "") or {}
        for_agent = (extra or {}).get("kind") == "answer" and (
            target.agent if target else rec.get("agent")
        )
        pinned = target is not None and target.deliverable and target.session_id != exclude
        msg = {
            "message_id": new_id("chm"),
            "project": target.project if pinned else (self.resolve_project(project) or project),
            "text": text,
            "meta": {"source": source, "sender": sender, "project": project},
            "created_at": now().isoformat(),
        }
        msg["meta"]["message_id"] = msg["message_id"]
        msg.update(extra or {})
        if (
            msg.get("kind") != "answer"
        ):  # work: the project's core skills lead it, task rules bind it
            msg["text"] += self.skills_note(msg["project"]) + self._task_rules(msg)
            msg["required_skills"] = self.core_names(msg["project"])
        if for_agent:
            msg["for_agent"] = for_agent
        if msg.get("kind"):
            msg["meta"]["kind"] = msg["kind"]
        msg["num"] = self._number("channel_mnum", msg["message_id"])  # short id m<num>
        msg["project_id"] = self.registry.ensure(msg["project"]).project_id
        msg.setdefault(
            "correlation_id", new_id("cor")
        )  # a conversation/workflow's messages share it
        msg["pinned"] = pinned  # for that session: it waits its turn there
        if pinned:
            msg["for_session"] = target.session_id  # kept across a restart (the queue is in memory)
        elif session_id and (c := self.svc.store.get("channel_child", rec.get("cwd") or "")):
            # for a lane session that is gone: its lane, in its own folder (reopened), keeps the work
            # and its context instead of starting over somewhere else
            msg.setdefault("lane", c["lane"])
            msg.setdefault("lane_path", c["path"])
        if not pinned:
            target = self._route(msg, exclude=exclude)
        elif msg.get("kind") != "answer" and target is self.main_for(target.project):
            self._mark_route(msg, target)  # addressed to the main: it routes this too
        if target:
            self._enqueue(target, msg)
        else:
            self._set(msg, "pending")
        self.svc.store.event("channel.message", message_id=msg["message_id"],
                             delivered_to=target and target.session_id)  # fmt: skip
        return msg, target

    def own_request(self, session_id: str, text: str) -> dict:
        """Work the user typed straight into the main session: a message of its own, held by the
        main, for dispatch to hand to a child (the main only routes)."""
        s = self.sessions.get(self.as_main(session_id))
        if s is None:
            raise ValueError(f"unknown session {session_id}: call get_messages first")
        if self.main_for(s.project) is not s:  # checked first: no message left behind on a refusal
            raise PermissionError(f"only the main session of {s.project} routes its work")
        if not text.strip():
            raise ValueError("text: the request the user typed")
        msg = {"message_id": new_id("chm"), "project": s.project,
               "text": text.strip() + self.skills_note(s.project),
               "required_skills": self.core_names(s.project),
               "meta": {"source": "session", "sender": "the user (in the main session)",
                        "project": s.project}, "created_at": now().isoformat()}  # fmt: skip
        msg["meta"]["message_id"] = msg["message_id"]
        msg["num"] = self._number("channel_mnum", msg["message_id"])
        self.delivered(msg, s.session_id)
        return msg

    def _task_rules(self, msg: dict) -> str:
        """routing.task_rules matching the request: their text on it, their commands required."""
        cfg = getattr(self.svc, "gateway_cfg", None)
        rules = [
            r for r in (cfg.routing.task_rules if cfg else []) if re.search(r.match, msg["text"])
        ]
        if not rules:
            return ""
        msg["required_commands"] = [r.require_command for r in rules if r.require_command]
        msg["rules"] = [r.rule for r in rules]
        return "".join(f"\n\nMANDATORY rule: {r.rule}" for r in rules)

    def core_names(self, project: str) -> list[str]:
        s = getattr(self.svc, "s", None)
        return list(next((v for k, v in (getattr(s, "core_skills", None) or {}).items()
                          if _norm(k) == _norm(project)), []))  # fmt: skip

    def skills_note(self, project: str) -> str:
        """The project's core skills (Settings.core_skills): every request through the gateway must
        use them; the PreToolUse hook blocks edits and commands until they are loaded."""
        core = self.core_names(project)
        if not core:
            return ""
        names = ", ".join(f"`{n}`" for n in core)
        return (f"\n\nMANDATORY core skills of {project}: load {names} with the Skill tool before anything "
                f"else (not listed there: read .claude/skills/<name>/SKILL.md) and do this request by "
                f"`{core[0]}`'s workflow. Edits and commands are blocked until they are loaded. In your "
                "answer, say which of its steps you followed.")  # fmt: skip

    def required_skills(self, session_id: str) -> list[dict]:
        """The skills the session's current work must use: [{message_id, skills}] (PreToolUse hook)."""
        return [{"message_id": m["message_id"], "skills": m.get("required_skills") or [],
                 "commands": m.get("required_commands") or []}
                for m in self.in_progress(session_id)
                if m.get("required_skills") or m.get("required_commands")]  # fmt: skip

    def as_main(self, session_id: str) -> str:
        """A #Main conversation that names its folder's shared session (an id from before it was
        the main): its own session. Only the main routes, so a routing call is the main's."""
        s = self.sessions.get(session_id)
        main = self.main_for(s.project) if s else None
        if main is not None and main.session_id.startswith(session_id + "_"):
            return main.session_id
        return session_id

    def conversation_sid(
        self, project: str, agent: str, cwd: str, client: str = "", instance: str = ""
    ) -> str:
        """Conversations of one folder and tool share its session; one has its own only as the
        project's #Main (claim_main), until it disconnects."""
        own = self.pull_id(project, agent, cwd, client)
        mine = self.pull_id(project, agent, cwd, client, instance) if instance else own
        # own session only while pinned: once not the main any more, the conversation folds back
        # into the folder's shared session (its waiter follows), so what it asks with and what it
        # listens on are one session again (seen in prod: an answer queued on the shared session
        # while the waiter listened on the old #Main one)
        return mine if mine == self._pinned_sid(project) else own

    def is_main(
        self, project: str, agent: str, cwd: str, client: str = "", instance: str = ""
    ) -> bool:
        """Whether this conversation is its project's main (PreToolUse hook: the main only routes)."""
        main = self.main_for(project)
        ids = {
            self.pull_id(project, agent, cwd, client),
            self.pull_id(project, agent, cwd, client, instance),
        }
        return main is not None and main.session_id in ids

    def _set(self, msg: dict, state: str) -> None:
        msg["state"] = state
        self.svc.store.put("channel_msg", msg["message_id"], msg, state=state)

    def delivered(self, msg: dict, session_id: str) -> None:
        """Handed to a session: in progress (the session is busy) until reply_message answers it."""
        msg["session_id"], msg["delivered_at"] = session_id, now().isoformat()
        if (s := self.sessions.get(session_id)) and msg.get(
            "kind"
        ) != "offer":  # an offer is no work
            s.last_work = now()
        # an answer to the session's own ask is information, not a job: it never keeps it busy
        self._set(msg, "done" if msg.get("kind") in ("answer", "offer") else "delivered")

    def in_progress(self, session_id: str) -> list[dict]:
        cutoff = now() - LEASE
        return [
            m for m in self.svc.store.list("channel_msg", states=["delivered"])
            if m.get("session_id") == session_id and m.get("delivered_at")
            and datetime.fromisoformat(m["delivered_at"]) > cutoff
        ]  # fmt: skip

    def _expire(self) -> None:
        """Delivered but unanswered past LEASE (the session lost it or died): back to the inbox, or
        after MAX_DELIVERIES to the operator as stuck (never dropped: the agent may still be on it)."""
        # runs on pull rounds, connects and the gateway's 30 s watchdog (app/main.py)
        cutoff, projects = now() - LEASE, set()
        for msg in self.svc.store.list("channel_msg", states=["delivered"]):
            at = msg.get("delivered_at")
            holder = self.sessions.get(msg.get("session_id") or "")
            if at is None:  # delivered before leases existed: too old to hand out again, expire it
                self._set(msg, "expired")
                continue
            elif datetime.fromisoformat(at) > cutoff:
                continue
            elif holder is not None and holder.mode == "push":
                continue  # Claude has it on screen; pushing it again would start it twice
            elif msg.get(
                "route"
            ):  # the main did not route it: stuck, a temporary router takes over
                self.stall(
                    msg.get("session_id") or "",
                    f"no routing within {int(LEASE.total_seconds() // 60)} min",
                )
                continue
            elif not msg.get("ask_id"):  # nobody answered within the lease: the operator decides
                self._stall(
                    [msg],
                    msg.get("session_id") or "",
                    f"no answer within {int(LEASE.total_seconds() // 60)} min",
                )
                continue
            msg["deliveries"] = msg.get("deliveries", 1) + 1
            if (
                msg["deliveries"] > MAX_DELIVERIES
            ):  # never dropped: the operator decides (move/wait/cancel)
                mins = int(LEASE.total_seconds() // 60)
                self._stall(
                    [msg],
                    msg.get("session_id") or "",
                    f"no answer after {MAX_DELIVERIES} leases of {mins} min",
                )
                continue
            elif holder is not None:  # still connected: only its own session gets it again
                msg["pinned"] = True
                self._enqueue(holder, msg)
            else:
                self._set(msg, "pending")
                projects.add(msg["project"])
            self.svc.store.event("channel.lease_expired", message_id=msg["message_id"],
                                 session=msg.get("session_id"), state=msg["state"])  # fmt: skip
        for project in projects:
            self._flush_pending(project)
        self._untaken()
        self._never_started()

    def stall(self, session_id: str, reason: str) -> list[dict]:
        """The session's agent stopped (StopFailure hook, Cursor error, lease over): its unanswered and
        queued work is held as "stalled" and shown on Telegram, where the operator moves it to another
        session, waits, or cancels. The session takes no new work until it is active again."""
        msgs = [m for m in self.in_progress(session_id) if m.get("kind") != "answer"]
        if s := self.sessions.get(session_id):
            s.stalled = reason[:200] or "error"
            keep = []
            while not s.queue.empty():
                m = s.queue.get_nowait()
                (keep if m.get("kind") in ("answer", "offer") else msgs).append(m)
            for m in keep:
                s.queue.put_nowait(m)
        routes = [
            m for m in msgs if m.get("route")
        ]  # a stuck main: its routing work is routed again
        for m in routes:
            for k in ("session_id", "delivered_at"):
                m.pop(k, None)
            self._set(m, "pending")
        self._stall([m for m in msgs if not m.get("route")], session_id, reason)
        for project in {m["project"] for m in routes}:
            self._flush_pending(project)
        return msgs

    def _stall(self, msgs: list[dict], session_id: str, reason: str) -> None:
        for m in msgs:
            m.update(stalled_session=session_id, stall_reason=reason[:200], stalled_at=now().isoformat(),
                     back_asked=False)  # fmt: skip
            self._set(m, "stalled")
            self.svc.store.event("channel.stalled", message_id=m["message_id"], session=session_id,
                                 reason=reason[:200])  # fmt: skip
        if msgs:
            self._notify("send_stalled", msgs, back=False)

    def active(self, session_id: str) -> list[dict]:
        """The session's agent works again (a prompt, get_messages): it takes work again, and work it
        left stalled is put to the operator once: go on here, move it, or cancel."""
        if s := self.sessions.get(session_id):
            was, s.stalled = s.stalled, ""
            if was:  # work that waited for it (a stuck main's project work) goes out now
                self._flush_pending(s.project)
        msgs = [m for m in self.svc.store.list("channel_msg", states=["stalled"])
                if m.get("stalled_session") == session_id and not m.get("back_asked")]  # fmt: skip
        for m in msgs:
            m["back_asked"] = True
            self._set(m, "stalled")
        if msgs:
            self._notify("send_stalled", msgs, back=True)
        return msgs

    def resume(
        self, ref: str, to_session: str | None = None
    ) -> tuple[dict, LiveSession | None] | None:
        """A stalled message goes out again: to that session, else the best open one of the project."""
        msg = next((m for m in self.svc.store.list("channel_msg", states=["stalled"])
                    if ref in (m["message_id"], f"m{m.get('num')}")), None)  # fmt: skip
        if msg is None:
            return None
        target = self.sessions.get(to_session or "")
        if (
            target is not None
            and target.stalled
            and target.session_id == msg.get("stalled_session")
        ):
            target.stalled = ""  # the operator says its own session may go on
        for k in (
            "stalled_session",
            "stall_reason",
            "stalled_at",
            "back_asked",
            "session_id",
            "delivered_at",
        ):
            msg.pop(k, None)
        msg["pinned"] = target is not None and target.deliverable
        if not msg["pinned"]:
            target = self.session_for(msg["project"], exclude=msg.get("from_session"))
        if target is None:
            self._set(msg, "pending")  # the next session of the project to connect takes it
            return msg, None
        if target is self.main_for(target.project) and msg.get("kind") != "answer":
            self._mark_route(msg, target)  # the main routes it, it does not do it
        self._enqueue(target, msg)
        self._assigned(msg, target)
        return msg, target

    def _notify(self, name: str, *args, **kw) -> None:
        """Call an async notifier method (Telegram) without blocking delivery; failures are logged."""
        fn = getattr(getattr(self.svc, "notifier", None), name, None)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if fn is not None:

            async def go():
                try:
                    await fn(*args, **kw)
                except Exception:  # noqa: BLE001 - a notice never breaks delivery
                    log.exception("operator notice %s failed", name)

            loop.create_task(go())

    def open_messages(self) -> list[dict]:
        """/queue: everything not answered or cancelled yet, oldest first."""
        msgs = [m for st in OPEN_STATES for m in self.svc.store.list("channel_msg", states=[st])
                if m.get("kind") != "offer"]  # fmt: skip
        for m in msgs:
            m.setdefault("state", "pending")
        return sorted(msgs, key=lambda m: m.get("created_at", ""))

    def cancel(self, ref: str) -> dict | None:
        """m<num> or message id. Waiting work is dropped. In-progress work is marked, and its session is
        told to stop (a note that needs no reply); an answer that still comes is labelled cancelled."""
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
        if msg["cancelled_from"] == "delivered" and msg.get("session_id"):
            self.send(
                msg.get("project", ""),
                f"🛑 The operator cancelled m{msg.get('num')} ({msg['message_id']}). Stop working on it and "
                "leave what you changed as it is; say what state it is in only if asked. This needs no reply.",
                session_id=msg["session_id"], sender="operator", source="operator", extra={"kind": "answer"},
            )  # fmt: skip
        return msg

    @staticmethod
    def pull_id(project: str, agent: str, cwd: str, client: str = "", instance: str = "") -> str:
        """One pull session per project, agent tool and folder (Claude, Codex and OpenCode on the
        same project stay apart), stable across polls that forget to pass session_id. A terminal
        session (cli) in the folder of a desktop one is another session, not a twin taking its work.
        instance: the agent's own conversation id (Claude's, from its waiter and hooks): two
        conversations in one folder are two sessions (base id + "_" + its hash)."""
        where = hashlib.sha256(cwd.encode()).hexdigest()[:6] if cwd else "0"
        where += "c" if client == "cli" else ""
        sid = f"pull_{_norm(project)[:24]}_{_norm(agent or 'agent')[:12]}_{where}"
        return sid + "_" + hashlib.sha256(instance.encode()).hexdigest()[:4] if instance else sid

    def pull(
        self, project: str, session_id: str | None, cwd: str = "", limit: int = 1, agent: str = "",
        client: str = "", instance: str = "", pid: int = 0,
    ):  # fmt: skip
        """get_messages: register/refresh a pull session and hand over what is queued for it.
        Nothing new while it still has an unanswered message (no duplicate or parallel jobs), except
        answers to its own questions, which that message is waiting for.
        Returns (session, new messages, messages still in progress)."""
        own = self.pull_id(project, agent, cwd, client)
        sid = session_id or self.conversation_sid(project, agent, cwd, client, instance)
        if (
            cwd
            and agent
            and sid.startswith("pull_")
            and sid != own
            and not sid.startswith(own + "_")  # a conversation of this folder
        ):
            # a stale id (remembered from before a folder rename or a restart): the folder's session,
            # the one its waiter and the gateway's pushes use
            sid = own
        if not cwd and sid not in self.sessions:
            # the agent forgot cwd, or reused a stale id (Codex desktop does both): its project's one
            # open session of that tool
            mine = [x for x in self.sessions.values() if x.mode == "pull" and x.cwd
                    and _norm(x.project) == _norm(project)
                    and (not agent or _norm(x.agent) == _norm(agent))]  # fmt: skip
            if len(mine) > 1:
                # which of them is calling is unknown: registering it would make a folderless twin
                # that takes the project's work and then lapses with it; hand out nothing
                return LiveSession(sid, project, "", "", mode="pull"), [], []
            sid = mine[0].session_id if mine else sid
        if sid in self._stopping:  # /close: hand out nothing; the tool tells the loop to stop
            self._stopping.discard(sid)
            return LiveSession(sid, project, cwd, "", mode="pull", closing=True), [], []
        s = self.sessions.get(sid)
        new = s is None or s.mode != "pull"
        if new:
            s = self.connect(sid, project, cwd, mode="pull", notify=False)
        s.last_seen = now()
        s.claude_pid = pid or s.claude_pid
        s.agent = agent or s.agent
        s.client = client if client in ("app", "cli") else s.client
        if new:  # announced once the agent and its kind are known
            self._ops(f"🔌 {self._label(s)} connected")
            self._maybe_gate(s)
            self.main_for(s.project)  # the project's first session becomes its main
            if agent:  # waiting work for this tool (an answer to its question) can go now
                self._flush_pending(s.project)
        if agent or client:
            self._save(s)
        if limit > 0:  # get_messages: the agent's model is running again
            self.active(s.session_id)
        self._expire()
        busy = self.in_progress(s.session_id)
        if s.enabled and not busy and s.queue.empty():
            self._flush_pending(s.project)  # free again: the project's waiting work
        out = []
        while s.enabled and not s.queue.empty() and len(out) < limit:
            head = s.queue._queue[0]
            if busy and head.get("kind") != "answer" and not head.get("route"):
                break  # one job at a time; answers and routing work still get through
            msg = s.queue.get_nowait()
            self.delivered(msg, s.session_id)
            out.append(msg)
        return s, out, busy

    @staticmethod
    def _next(s: LiveSession) -> str | None:
        """The queued message a pull would hand out next (None: nothing for this session now)."""
        if not s.enabled or s.closing or s.queue.empty():
            return None
        return s.queue._queue[0]["message_id"]  # ponytail: asyncio.Queue internals

    def peek(
        self, project: str, agent: str, cwd: str = "", client: str = "", instance: str = "",
        pid: int = 0,
    ) -> dict:  # fmt: skip
        """Keep a pull session listed and say whether it has work, handing nothing out (no model run):
        queued = a new message waits, work = new or unfinished."""
        s, _, busy = self.pull(project, None, cwd, 0, agent, client, instance, pid)
        nxt = self._next(s)
        return {"session_id": s.session_id, "closing": s.closing, "enabled": s.enabled,
                "queued": nxt is not None, "next": nxt,
                "work": nxt is not None or (s.enabled and not s.closing and bool(busy))}  # fmt: skip

    async def wait(
        self, project: str, agent: str, cwd: str = "", client: str = "", after: str = "",
        hold: float = 25, instance: str = "", pid: int = 0,
    ) -> dict:  # fmt: skip
        """Long-poll peek (`agw wait`): returns as soon as a message other than `after` is queued or
        the session closes, else after `hold` seconds (under PULL_TTL, so the session stays listed)."""
        state, end = self.peek(project, agent, cwd, client, instance, pid), time.monotonic() + hold
        while not state["closing"] and state["next"] in (None, after) and time.monotonic() < end:
            await asyncio.sleep(1)  # ponytail: in-process check each second, no event per queue
            s = self.sessions.get(state["session_id"])
            if s is None or self._next(s) not in (None, after):
                state = self.peek(project, agent, cwd, client, instance, pid)  # closed, or new work
        return state

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
            s.last_seen = s.last_work = now()
        note = f"\n\n(answer to {message_id})" if message_id else ""
        msg = self.svc.store.get("channel_msg", message_id) if message_id else None
        if (
            msg is not None
            and msg.get("session_id") not in (None, session_id)
            and msg.get("state") == "delivered"
        ):
            raise PermissionError(
                f"m{msg.get('num')} was handed to another session; answer only your own messages"
            )
        if msg is not None and _norm(msg.get("project", "")) != _norm(rec["project"]):
            raise PermissionError(f"m{msg.get('num')} belongs to another project")
        if msg is not None and msg.get("kind") == "offer":  # an offer answered: a bid, no report
            return self._bid_by_reply(session_id, message_id, text)
        # a child's answer (to the operator or another agent) brings its branch into the project repo
        child = self.svc.store.get("channel_child", rec.get("cwd") or "")
        fetched = bool(child) and await asyncio.to_thread(_fetch_back, child)
        overlap = ""
        if fetched or (child or {}).get("provider") == "worktree":  # its branch is in the repo
            others = [c for c in self.children(rec["project"]) if c["status"] != "deleted"
                      and c["path"] != child["path"] and c.get("repo") == child.get("repo")]  # fmt: skip
            overlap = await asyncio.to_thread(_overlap_report, child, others)
        if msg is not None and msg.get("kind") == "answer":  # answers need no reply
            self._set(msg, "done")
            return {"note": "that message was an answer to your own ask; nothing to reply"}
        if msg is not None and msg.get("ask_id") and msg.get("state") != "cancelled":
            self._set(
                msg, "done"
            )  # an agent's request: the answer goes back to that agent, not to Telegram
            by = f"s{s.num} ({s.project})" if s else rec["project"]
            self._flush_pending(rec["project"])
            shots = [self.svc.artifacts.add(rec["task_id"], "screenshot", name, data, producer=by)
                     for name, data in images]  # fmt: skip
            if shots:  # the asker reads them by path; the operator's copy shows them as photos
                text += "\n\nScreenshots:\n" + "\n".join(str(a.path) for a in shots)
            return {"ask": self.svc.asks.answer(msg["ask_id"], text, by=by,
                                                images=[a.artifact_id for a in shots])}  # fmt: skip
        if msg is not None:
            if msg.get("state") == "cancelled":
                note = f"\n\n(answer to {message_id}, which was cancelled on Telegram)"
            else:
                self._set(msg, "done")  # frees the session for its next message
                self._flush_pending(rec["project"])
        body = text.strip() + note
        if fetched:
            body += f"\n\n(branch {child['branch']} is in the project repo for review)"
        if overlap:
            body += f"\n{overlap}"
        mode = rec.get("mode", "push")
        agent = (s.agent if s else "") or rec.get("agent") or "claude_code"
        return await self.svc.collab.submit_report(
            rec["task_id"], body, f"{agent} ({mode} session)", images=images
        )


def _toplevel(cwd: str) -> Path:
    out = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], capture_output=True,
                         text=True, timeout=30)  # fmt: skip
    if out.returncode != 0:
        raise ValueError(f"the main session's folder is not a git repo: {cwd}")
    return Path(out.stdout.strip())


CHILDREN = ".agw-children"  # inside the project, ignored by its git
CLAUDE_JSON = (
    Path.home() / ".claude.json"
)  # Claude Code's per-folder trust (tests point it elsewhere)
TRUST_KEYS = ("hasTrustDialogAccepted", "enabledMcpjsonServers", "disabledMcpjsonServers",
              "hasClaudeMdExternalIncludesApproved", "hasClaudeMdExternalIncludesWarningShown")  # fmt: skip


CARRY = ("CLAUDE.md", "AGENTS.md", "GEMINI.md", ".claude")  # the project's agent rules and skills
CARRY_SKIP = {"worktrees", "scheduled_tasks.lock"}  # (the app's own worktrees, a lock)


def _carry(repo: Path, child: Path) -> int:
    """The project's rules, skills, subagents and agent settings that git does not carry (untracked
    or ignored, e.g. a global `CLAUDE.md` ignore or `.claude/` in .gitignore): copied into its child,
    which is a git worktree and would start without them. A file the child lacks, or one changed in
    the project since, is copied; returns how many."""
    n = 0
    for name in CARRY:
        src = repo / name
        files = (
            [src]
            if src.is_file()
            else sorted(p for p in src.rglob("*") if p.is_file())
            if src.is_dir()
            else []
        )
        for f in files:
            rel = f.relative_to(repo)
            if CARRY_SKIP & set(rel.parts):
                continue
            dst = child / rel
            if dst.exists() and dst.stat().st_mtime >= f.stat().st_mtime:
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst)
            n += 1
    return n


def _trust(path: Path, repo: Path) -> bool:
    """A child folder the gateway made inside a project the operator already trusts is trusted the
    same way, so its Claude Code does not stop at "trust this folder?" (operator's choice,
    2026-10-27). Never for a project that is not trusted itself."""
    try:
        data = json.loads(CLAUDE_JSON.read_text())
    except (OSError, ValueError):
        return False
    projects = data.setdefault("projects", {})
    parent = projects.get(str(repo)) or {}
    if not parent.get("hasTrustDialogAccepted"):
        return False
    entry = projects.setdefault(str(path), {})
    entry.update({k: parent[k] for k in TRUST_KEYS if k in parent})
    # Claude Code also reads the project's CLAUDE.md above the copy; its imports (e.g. @AGENTS.md)
    # are the project's own files but sit outside the copy, so they would ask again
    entry.update(
        hasClaudeMdExternalIncludesApproved=True, hasClaudeMdExternalIncludesWarningShown=True
    )
    tmp = CLAUDE_JSON.with_name(f".claude.json.agw-{os.getpid()}")
    # ponytail: read-modify-write; a Claude Code writing the same file at that instant can drop it
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(CLAUDE_JSON)
    return True


def _git(cwd, *args: str) -> str | None:
    """stdout of a read-only git command, None when it fails."""
    out = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=300
    )
    return out.stdout.strip() if out.returncode == 0 else None


def _worktree(repo: Path, lane: str) -> tuple[Path, str, str, str, str]:
    """A child's own copy of the project and branch: <repo>/.agw-children/<lane>-<id>. An APFS
    copy-on-write clone (almost no disk until files change; ignored build caches come along, so
    engines need no re-import), reset to the last commit on a new branch agw/<lane>-<id>: the main
    folder's unfinished edits stay out. Its branch lives in the copy's .git (see _fetch_back).
    Where cloning fails (not APFS, the repo is itself a worktree): a git worktree in the same place.
    A folder that is not a git repo (or has no commit): a plain copy, no branch (provider "copy").
    Returns (path, branch, base: the commit it starts from, target: the repo's branch then, provider:
    clone | worktree | copy)."""
    name = f"{lane}-{new_id('w')[-4:]}"
    branch, home = f"agw/{name}", repo / CHILDREN
    path = home / name
    home.mkdir(exist_ok=True)
    (home / ".gitignore").write_text("*\n")  # never part of the repo itself
    (home / ".gdignore").touch()  # Godot: do not import the copies
    if not (repo / ".git").exists() or not _git(repo, "rev-parse", "--verify", "-q", "HEAD"):
        # not a git repo, or no commit yet: a plain copy-on-write copy, no branch to bring back
        path.mkdir()
        items = [str(x) for x in repo.iterdir() if x.name != CHILDREN]
        if subprocess.run(
            ["cp", "-cR", *items, str(path)], capture_output=True, timeout=900
        ).returncode:
            shutil.rmtree(path, ignore_errors=True)
            raise ValueError(f"could not copy {repo} for the child")
        return path, "", "", "", "copy"
    target = _git(repo, "symbolic-ref", "--short", "-q", "HEAD") or ""  # "" when detached
    if (repo / ".git").is_dir():
        path.mkdir()
        items = [str(x) for x in repo.iterdir() if x.name != CHILDREN]
        copied = subprocess.run(["cp", "-cR", *items, str(path)], capture_output=True, timeout=900)
        base = copied.returncode == 0 and _git(path, "rev-parse", "--verify", "-q", "HEAD")
        git = ["git", "-C", str(path)]
        ok = bool(base) and all(subprocess.run(cmd, capture_output=True, timeout=900).returncode == 0
                                for cmd in ([*git, "checkout", "-q", "-f", "-B", branch, base],
                                            [*git, "clean", "-fdq"]))  # fmt: skip
        if ok:
            return path, branch, base, target, "clone"
        shutil.rmtree(path, ignore_errors=True)
    base = _git(repo, "rev-parse", "--verify", "-q", "HEAD") or ""
    out = subprocess.run(["git", "-C", str(repo), "worktree", "add", "-b", branch, str(path), base or "HEAD"],
                         capture_output=True, text=True, timeout=900)  # fmt: skip
    if out.returncode != 0:
        raise ValueError(f"git worktree add failed: {(out.stderr or out.stdout).strip()[:300]}")
    return path, branch, base, target, "worktree"


def _changed(repo: Path, base: str, branch: str) -> set[str]:
    """Files a branch changed since its base, as the project repo has it (empty: unknown)."""
    names = _git(repo, "diff", "--name-only", base, f"refs/heads/{branch}") if base else None
    return set(names.splitlines()) if names else set()


def _overlap_report(child: dict, others: list[dict]) -> str:
    """Read-only, after the branch is in the project repo: files that conflict with its target
    branch (`git merge-tree`, git >= 2.38; it writes only loose objects, no ref or working tree) and
    files other lanes of the project changed too. "" when there is nothing to say."""
    repo, branch, lines = Path(child.get("repo") or ""), child["branch"], []
    short = lambda files: ", ".join(sorted(files)[:5]) + (" …" if len(files) > 5 else "")  # noqa: E731
    if target := child.get("target"):
        out = subprocess.run(["git", "-C", str(repo), "merge-tree", "--write-tree", "--name-only",
                              "--no-messages", target, f"refs/heads/{branch}"],
                             capture_output=True, text=True, timeout=300)  # fmt: skip
        if out.returncode == 1:  # 1: conflicts (tree id, then the conflicted files); >1: error
            lines.append(
                f"conflicts with {target}: {short(set(out.stdout.splitlines()[1:]) - {''})}"
            )
    mine = _changed(repo, child.get("base") or "", branch)
    for o in others if mine else []:
        # ponytail: other lanes count as they are in the project repo (their last fetch-back)
        if both := mine & _changed(repo, o.get("base") or "", o["branch"]):
            lines.append(f"also changed by lane {o['lane']}: {short(both)}")
    return "\n".join(lines)


def _fetch_back(child: dict) -> bool:
    """A clone's branch into the project repo (`git fetch`), so it can be reviewed and merged
    there; the project folder's files are not touched. A worktree's branch is already there."""
    path, repo = Path(child["path"]), Path(child.get("repo") or "")
    if child.get("provider") == "adopted" or path.resolve() == repo.resolve():
        return False  # the project's own folder: nothing to fetch
    if not (path / ".git").is_dir() or not (repo / ".git").exists():
        return False
    out = subprocess.run(["git", "-C", str(repo), "fetch", "-q", str(path),
                          f"+{child['branch']}:{child['branch']}"], capture_output=True, timeout=300)  # fmt: skip
    return out.returncode == 0
