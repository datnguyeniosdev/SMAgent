"""Deliver work from Telegram into open Claude Code sessions, in two modes sharing one inbox.

push  `agw session` in a terminal: a stdio shim (app/channel_shim.py) holds an SSE stream and turns
      each message into a `notifications/claude/channel` event (Claude Code channels, research
      preview, >= 2.1.80); the session starts working at once. Answers go through the shim's `reply`.
pull  any session (desktop app, VS Code, terminal): `/loop 1m` calls the MCP tool `get_messages`
      and answers with `reply_message`. Works without channels, up to one loop interval late.

Each session has an operator switch (/on, /off, persisted per session id). Messages go to an open,
enabled, non-blocked session of the project (push preferred, newest first) and otherwise wait.
Routing (docs/DISPATCH_DESIGN.md): work for a named session (/send s#, a reply to its report) stays
in that session, one job after another. Project work goes to the project's main session, which only
routes it with `dispatch`: to the child session of that lane (same lane: in order) or to a new child
in its own copy-on-write copy of the project (other lanes: in parallel). Answers to a session's own questions skip the
line: they are what its current job waits for.
Answers are published as reports of the session's record-only task, so a Telegram reply to one is
routed back into the same session (see CollaborationService.submit_feedback).
"""

from __future__ import annotations

import asyncio
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

from app.models import new_id, now

log = logging.getLogger(__name__)
PULL_TTL = timedelta(minutes=5)  # a pull session is open while it polled this recently (/loop 1m)
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


# display names of the agent tools in session titles (ClaudeCode(App)-Coinlets)
TOOL_TITLES = {"claude_code": "ClaudeCode", "codex": "Codex", "opencode": "OpenCode",
               "antigravity": "Antigravity", "gemini": "Gemini", "droid": "Droid",
               "cursor": "Cursor", "claude_desktop": "ClaudeDesktop"}  # fmt: skip


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
    agent: str = ""  # which agent tool runs the session (claude_code, opencode, codex, ...)
    closing: bool = False  # pull: /close asked its loop to stop at the next round
    client: str = ""  # "app" (desktop app / IDE) or "cli", as the session reports it
    stalled: str = ""  # why its agent stopped (rate limit, API error, no answer): no new work
    last_work: datetime = field(default_factory=now)  # last message handed to it or answered
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
        self._stopping: set[str] = (
            set()
        )  # closed pull sessions: tell their loop to stop if it polls again
        self._main_asked: set[str] = set()  # projects whose offline main was put to the operator
        from app.launcher import open_in_terminal

        self.launch = open_in_terminal  # (folder, agent, model): opens a child; tests replace it
        # session queues live in memory: what they held when the last process stopped waits again
        for msg in svc.store.list("channel_msg", states=["queued"]):
            self._set(msg, "pending")

    # ---- sessions ----

    def _ops(self, html: str) -> None:
        """A short notice to the operator's group (sessions coming and going, late deliveries)."""
        self._notify("send_control", html)

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
        self.project_num(project)
        self._save(s)
        self.sessions[session_id] = s
        main = self.svc.store.get("channel_main", _norm(project)) or {}
        if main.get("pending_new") and not self.child(s):  # the operator asked for a new main
            self.set_main(s)
        elif mode == "push":  # (a pull session is known once its agent is: see pull)
            self.main_for(project)
        self._expire()
        self._flush_pending(project)
        self.svc.store.event("channel.connected", task_id=task.task_id, session=session_id,
                             project=project, mode=mode, blocked=blocked)  # fmt: skip
        log.info("%s session %s connected (%s, %s)", mode, session_id, project, cwd)
        if notify:
            self._ops(f"🔌 {self._label(s)} connected")
        return s

    def _save(self, s: LiveSession) -> None:
        body = {"project": s.project, "cwd": s.cwd, "task_id": s.task_id, "mode": s.mode,
                "enabled": s.enabled, "agent": s.agent, "client": s.client}  # fmt: skip
        self.svc.store.put("channel_session", s.session_id, body, task_id=s.task_id, state="open")

    def disconnect(self, s: LiveSession, why: str = "") -> None:
        if (
            self.sessions.get(s.session_id) is s
        ):  # a newer stream for the same id may have replaced it
            del self.sessions[s.session_id]
            self._release(s)
            self._ops(f"⚪ {self._label(s)} left" + (f": {why}" if why else ""))
            self.svc.store.event("channel.disconnected", task_id=s.task_id, session=s.session_id)
            log.info("%s session %s disconnected", s.mode, s.session_id)

    def _release(self, s: LiveSession) -> None:
        """Messages a session took but never handed to Claude go back to the inbox."""
        while not s.queue.empty():
            msg = s.queue.get_nowait()
            self._set(msg, "pending")
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
        if self.main_for(s.project) is s:
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
        return s.queue.empty() and not self.in_progress(s.session_id)

    def _enqueue(self, s: LiveSession, msg: dict) -> None:
        """Into a session's queue; an answer (its current job waits for it) or routing work for the
        main session (a quick dispatch) goes first."""
        msg["queued_at"] = now().isoformat()
        s.queue.put_nowait(msg)
        first = lambda m: m.get("kind") == "answer" or m.get("route")  # noqa: E731
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
                    and msg.get("kind") != "answer"
                    and not msg.get("route")
                    and waiting_since < late
                ):
                    self._take(s, msg)
                    self._stall([msg], s.session_id,
                                f"not picked up within {int(QUEUE_WAIT.total_seconds() // 60)} min")  # fmt: skip

    # ---- main session and children: docs/DISPATCH_DESIGN.md ----

    def child(self, s: LiveSession) -> dict | None:
        """The child record of a session opened by dispatch (lane, branch, worktree), else None."""
        return self.svc.store.get("channel_child", s.cwd) if s.cwd else None

    def set_main(self, s: LiveSession, flush: bool = True) -> None:
        """/main s#, or the first session of a project: it routes the project's work."""
        self.svc.store.put("channel_main", _norm(s.project), {"project": s.project,
                           "session_id": s.session_id, "cwd": s.cwd, "agent": s.agent, "kind": s.kind})  # fmt: skip
        self._main_asked.discard(_norm(s.project))
        if flush:  # not from main_for, which may run inside a flush (a message queued twice)
            self._flush_pending(s.project)

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
        same = [x for x in mine if x.session_id == rec.get("session_id") or (x.cwd and x.cwd == rec.get("cwd")
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

    def _open_router(self, project: str, rec: dict, main: LiveSession) -> None:
        """A temporary router for a stuck main, with the main's agent tool, inside the project."""
        try:
            repo = _toplevel(main.cwd)
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
        if msg.get("kind") == "answer":
            return self.session_for(project, exclude=exclude, agent=msg.get("for_agent"))
        if msg.get("wait_cwd"):
            kids = [x for x in self.sessions.values() if x.cwd == msg["wait_cwd"] and x.deliverable]
            return max(kids, key=lambda x: x.mode == "push", default=None)
        own = self.sessions.get(msg.get("for_session") or "")
        if (
            own is not None and own.deliverable and msg.get("pinned")
        ):  # back to the session it was for
            return own
        main = self.main_for(project)
        if main is not None and main.session_id == exclude:  # the main asked its own project
            return self.session_for(project, exclude=exclude)
        if main is None:
            rec = self.svc.store.get("channel_main", _norm(project)) or {}
            if rec.get("session_id") and _norm(project) not in self._main_asked:
                self._main_asked.add(_norm(project))
                self._notify("send_main_offline", project)
            return None
        self._mark_route(msg, main)
        return main

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
        self, session_id: str, ref: str, lane: str, brief: str = "", to: str = "", model: str = ""
    ) -> dict:
        """The main session hands a message to a child: `to` (s#), else the open child of that
        lane, else a new child opened in a new git worktree with the main's agent tool."""
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
        if self.main_for(msg["project"]) is not main and not router:
            raise PermissionError(f"only the main session of {msg['project']} routes its work")
        if msg.get("session_id") not in (None, session_id):
            raise PermissionError(f"m{msg.get('num')} is not yours to route")
        lane = re.sub(r"[^a-z0-9-]+", "-", lane.lower()).strip("-")[:30] or "task"
        kids = [x for x in self.sessions.values() if x is not main and x.deliverable
                and _norm(x.project) == _norm(msg["project"])]  # fmt: skip
        if to:
            target = next((x for x in kids if f"s{x.num}" == to.lower()), None)
            if target is None:
                raise ValueError(f"{to} is not an open session of {msg['project']}")
        else:
            same = [x for x in kids if (self.child(x) or {}).get("lane") == lane]
            target = max(same, key=lambda x: x.mode == "push", default=None)
        for x in self.sessions.values():  # still queued in the main: taken out first
            for m in [m for m in x.queue._queue if m["message_id"] == msg["message_id"]]:
                self._take(x, m)
        for k in ("session_id", "delivered_at", "wait_cwd"):
            msg.pop(k, None)
        note = f"\n\nBrief from the main session (lane {lane}): {brief}" if brief else ""
        msg.pop("route", None)
        msg.update(text=msg.pop("orig_text", msg["text"]) + note, lane=lane, pinned=True,
                   dispatched_by=session_id)  # fmt: skip
        rec = self.svc.store.get("channel_main", _norm(msg["project"])) or {}
        if (
            rec.get("router_cwd") and rec["router_cwd"] == main.cwd
        ):  # told to the main when it is back
            self.svc.store.put("channel_main", _norm(msg["project"]), {**rec, "routed":
                               [*rec.get("routed", []), f"m{msg.get('num')} → {lane}"]})  # fmt: skip
        if target is not None:
            self._tag(target.cwd, msg)
            self._enqueue(target, msg)
            self._assigned(msg, target)
            self._ops(f"🧭 m{msg.get('num')} → {self._label(target)} ({lane})")
            return {"to": f"s{target.num}", "lane": lane, "new_session": False}
        self._set(msg, "pending")  # held for the new child (wait_cwd below)
        old = next((c for c in self.children(msg["project"])
                    if c["lane"] == lane and c["status"] == "closed"), None)  # fmt: skip
        if old is not None:  # the lane's child closed: reopen it on its folder, no new copy
            self._tag(old["path"], msg)
            if model:  # reopened with another model: it keeps that one
                self.svc.store.put("channel_child", old["path"], {**old, "model": model})
            msg["wait_cwd"], msg["waiting_since"] = old["path"], now().isoformat()
            self._set(msg, "pending")
            await asyncio.to_thread(_trust, Path(old["path"]), Path(old.get("repo") or ""))
            agent = old.get("agent", "") if old.get("agent") not in ("", "claude_code") else ""
            model = model or old.get("model") or self.models(msg["project"])["child"]
            wid = await asyncio.to_thread(self.launch, Path(old["path"]), agent, model)
            self._remember_window(old["path"], wid)
            self._ops(
                f"🧭 m{msg.get('num')} → reopened child ({lane}) on <code>{old['branch']}</code>"
            )
            return {"to": "reopened", "lane": lane, "new_session": False, "branch": old["branch"],
                    "path": old["path"]}  # fmt: skip
        try:
            try:
                repo = await asyncio.to_thread(_toplevel, main.cwd)
            except ValueError:  # not a git repo: its children get a plain copy of the folder
                from app.projects import repo_for

                repo = repo_for(Path(main.cwd)) or Path(main.cwd)
            free, started = shutil.disk_usage(repo).free, time.monotonic()
            path, branch, base, target, provider = await asyncio.to_thread(_worktree, repo, lane)
        except ValueError:  # no git repo, worktree failed: it stays with the main
            msg.update(session_id=session_id, delivered_at=now().isoformat())
            self._set(msg, "delivered")
            raise
        self.svc.store.event("channel.child_created", path=str(path), branch=branch, provider=provider,
                             seconds=round(time.monotonic() - started, 2), free_before=free,
                             free_after=shutil.disk_usage(repo).free)  # fmt: skip
        from app.config import ENV_PORTS, env_name
        from app.repo_setup import setup_repo

        url = f"http://127.0.0.1:{ENV_PORTS[env_name()]}/mcp"
        await asyncio.to_thread(setup_repo, path, url, msg["project"], False)
        self.svc.store.put("channel_child", str(path), {"project": msg["project"], "lane": lane,
                           "branch": branch, "repo": str(repo), "parent": session_id, "path": str(path),
                           "agent": main.agent or "claude_code", "created_at": now().isoformat(),
                           "base": base, "target": target, "provider": provider})  # fmt: skip
        self._tag(str(path), msg)
        msg["wait_cwd"], msg["waiting_since"] = str(path), now().isoformat()
        self._set(msg, "pending")
        await asyncio.to_thread(_trust, path, repo)
        agent = main.agent if main.agent not in ("", "claude_code") else ""
        model = model or self.models(msg["project"])["child"]
        c = self.svc.store.get("channel_child", str(path)) or {}
        self.svc.store.put("channel_child", str(path), {**c, "model": model})
        self._remember_window(str(path), await asyncio.to_thread(self.launch, path, agent, model))
        where = f"on branch <code>{branch}</code>" if branch else "in a copy of the folder (no git)"
        self._ops(f"🧭 m{msg.get('num')} → new child ({lane}) {where}")
        return {"to": "new", "lane": lane, "new_session": True, "branch": branch, "path": str(path)}

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
            if c is None:
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
        if for_agent:
            msg["for_agent"] = for_agent
        if msg.get("kind"):
            msg["meta"]["kind"] = msg["kind"]
        msg["num"] = self._number("channel_mnum", msg["message_id"])  # short id m<num>
        msg["pinned"] = pinned  # for that session: it waits its turn there
        if pinned:
            msg["for_session"] = target.session_id  # kept across a restart (the queue is in memory)
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

    def _set(self, msg: dict, state: str) -> None:
        msg["state"] = state
        self.svc.store.put("channel_msg", msg["message_id"], msg, state=state)

    def delivered(self, msg: dict, session_id: str) -> None:
        """Handed to a session: in progress (the session is busy) until reply_message answers it."""
        msg["session_id"], msg["delivered_at"] = session_id, now().isoformat()
        if s := self.sessions.get(session_id):
            s.last_work = now()
        # an answer to the session's own ask is information, not a job: it never keeps it busy
        self._set(msg, "done" if msg.get("kind") == "answer" else "delivered")

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
                (keep if m.get("kind") == "answer" else msgs).append(m)
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
        msgs = [m for st in OPEN_STATES for m in self.svc.store.list("channel_msg", states=[st])]
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
    def pull_id(project: str, agent: str, cwd: str, client: str = "") -> str:
        """One pull session per project, agent tool and folder (Claude, Codex and OpenCode on the
        same project stay apart), stable across polls that forget to pass session_id. A terminal
        session (cli) in the folder of a desktop one is another session, not a twin taking its work."""
        where = hashlib.sha256(cwd.encode()).hexdigest()[:6] if cwd else "0"
        where += "c" if client == "cli" else ""
        return f"pull_{_norm(project)[:24]}_{_norm(agent or 'agent')[:12]}_{where}"

    def pull(
        self, project: str, session_id: str | None, cwd: str = "", limit: int = 1, agent: str = "",
        client: str = "",
    ):  # fmt: skip
        """get_messages: register/refresh a pull session and hand over what is queued for it.
        Nothing new while it still has an unanswered message (no duplicate or parallel jobs), except
        answers to its own questions, which that message is waiting for.
        Returns (session, new messages, messages still in progress)."""
        sid = session_id or self.pull_id(project, agent, cwd, client)
        if (
            cwd
            and agent
            and sid.startswith("pull_")
            and sid != (own := self.pull_id(project, agent, cwd, client))
        ):
            # a stale id (remembered from before a folder rename or a restart): the folder's session,
            # the one its waiter and the gateway's pushes use
            sid = own
        if not cwd and sid not in self.sessions:
            # the agent forgot cwd, or reused a stale id (Codex desktop does both): its project's one
            # open session of that tool
            mine = [x for x in self.sessions.values() if x.mode == "pull" and x.cwd
                    and _norm(x.project) == _norm(project) and _norm(x.agent) == _norm(agent)]  # fmt: skip
            sid = mine[0].session_id if len(mine) == 1 else sid
        if sid in self._stopping:  # /close: hand out nothing; the tool tells the loop to stop
            self._stopping.discard(sid)
            return LiveSession(sid, project, cwd, "", mode="pull", closing=True), [], []
        s = self.sessions.get(sid)
        new = s is None or s.mode != "pull"
        if new:
            s = self.connect(sid, project, cwd, mode="pull", notify=False)
        s.last_seen = now()
        s.agent = agent or s.agent
        s.client = client if client in ("app", "cli") else s.client
        if new:  # announced once the agent and its kind are known
            self._ops(f"🔌 {self._label(s)} connected")
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

    def peek(self, project: str, agent: str, cwd: str = "", client: str = "") -> dict:
        """Keep a pull session listed and say whether it has work, handing nothing out (no model run):
        queued = a new message waits, work = new or unfinished."""
        s, _, busy = self.pull(project, None, cwd, 0, agent, client)
        nxt = self._next(s)
        return {"session_id": s.session_id, "closing": s.closing, "enabled": s.enabled,
                "queued": nxt is not None, "next": nxt,
                "work": nxt is not None or (s.enabled and not s.closing and bool(busy))}  # fmt: skip

    async def wait(
        self, project: str, agent: str, cwd: str = "", client: str = "", after: str = "",
        hold: float = 25,
    ) -> dict:  # fmt: skip
        """Long-poll peek (`agw wait`): returns as soon as a message other than `after` is queued or
        the session closes, else after `hold` seconds (under PULL_TTL, so the session stays listed)."""
        state, end = self.peek(project, agent, cwd, client), time.monotonic() + hold
        while not state["closing"] and state["next"] in (None, after) and time.monotonic() < end:
            await asyncio.sleep(1)  # ponytail: in-process check each second, no event per queue
            s = self.sessions.get(state["session_id"])
            if s is None or self._next(s) not in (None, after):
                state = self.peek(project, agent, cwd, client)  # closed, or new work
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
            return {"ask": self.svc.asks.answer(msg["ask_id"], text, by=by)}
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
    if not (path / ".git").is_dir() or not (repo / ".git").exists():
        return False
    out = subprocess.run(["git", "-C", str(repo), "fetch", "-q", str(path),
                          f"+{child['branch']}:{child['branch']}"], capture_output=True, timeout=300)  # fmt: skip
    return out.returncode == 0
