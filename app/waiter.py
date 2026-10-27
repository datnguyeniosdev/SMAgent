"""`agw wait`: the pull loop of a Claude Code session without a paid model run per minute. Claude runs it
with Bash `run_in_background`; it holds a long-poll to the gateway (/api/v1/channel/wait: no model,
keeps the session listed) and exits only when a new request is queued, which wakes Claude with what to
do. A gateway that is down is waited out silently (the operator starts it). The newest waiter of a
session wins; an older one exits quietly.
Antigravity (`--agent antigravity`, started by its agent in its own terminal): instead of exiting, the
waiter wakes the conversation with `agentapi send-message` ($ANTIGRAVITY_CONVERSATION_ID) and keeps
waiting, so the agent never has to start it again.
State: var/waiters/<session id>.json = {"pid": ..., "woke": <message id>, "at": <time>}."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

WAITERS = Path(__file__).resolve().parents[1] / "var" / "waiters"
REMIND_S = 600  # a request still queued after a wake-up (ignored or lost) wakes Claude again


AGENTAPI = Path.home() / ".gemini/antigravity/bin/agentapi"


def command(project: str, cwd: str, client: str = "", agent: str = "claude_code") -> str:
    """The shell command Claude runs in the background (absolute agw: hooks run outside the venv)."""
    agw = Path(sys.executable).parent / "agw"
    cmd = f"{shlex.quote(str(agw))} wait --project {shlex.quote(project)} --cwd {shlex.quote(cwd)}"
    cmd = f"{cmd} --client {client}" if client else cmd
    return cmd if agent == "claude_code" else f"{cmd} --agent {agent}"


def wake_text(project: str, cwd: str, client: str, sid: str, message_id: str,
              agent: str = "claude_code") -> str:  # fmt: skip
    from app.repo_setup import loop_prompt

    prompt = loop_prompt(project, client, agent).replace("this folder", f"'{cwd}'")
    prompt = prompt.replace("reusing the session_id it returned before", f"session_id '{sid}'")
    if agent == "antigravity":  # detached, it outlives the turn; running it again just replaces it
        again = ("The waiter keeps running by itself. If you are unsure it still runs, run "
                 f"`{command(project, cwd, client, agent)}` again (it returns at once and replaces "
                 "the old one).")  # fmt: skip
    elif agent == "cursor":  # Cursor's stop hook waits again after this turn
        again = "After you answer, the gateway wakes this conversation again by itself."
    else:
        again = ("When you have answered, start this waiter again with the Bash tool, run_in_background "
                 f"true: `{command(project, cwd, client)}`.")  # fmt: skip
    return (
        f"agent-gateway has work for '{project}' (message {message_id}). {prompt} {again} If the "
        "get_messages tool is missing or fails, tell the user in one line that the agent-gateway MCP "
        "server is not connected."
    )


def antigravity_notify():
    """A wake-up for the Antigravity conversation this waiter was started from, or None outside one
    (agentapi needs the address and token Antigravity gives its own terminals)."""
    conv = os.environ.get("ANTIGRAVITY_CONVERSATION_ID")
    exe = os.environ.get("ANTIGRAVITY_AGENTAPI_EXE") or str(AGENTAPI)
    if not conv or not os.environ.get("ANTIGRAVITY_LS_ADDRESS"):
        return None

    def notify(text: str) -> bool:
        cmd = [exe]
        if Path(exe).name == "language_server":
            cmd.append("agentapi")
        cmd.extend(["send-message", "--title=Telegram work", conv, text])
        r = subprocess.run(  # noqa: S603 - Antigravity's own CLI
            cmd,
            capture_output=True, text=True, timeout=60,
        )  # fmt: skip
        ok = r.returncode == 0 and '"error"' not in r.stdout
        print(f"{time.strftime('%H:%M:%S')} woke conversation {conv[:8]}: {'ok' if ok else r.stdout[-200:]}",
              flush=True)  # fmt: skip
        return ok

    return notify


def missing(project: str, cwd: str) -> bool:
    """The session's waiter is not running (the app restarted, or it was never started), and the
    operator did not close the session. Cheap: a file read and a signal 0, no gateway call."""
    from app.channel import ChannelHub

    data = _read(WAITERS / f"{ChannelHub.pull_id(project, 'claude_code', cwd)}.json")
    if data.get("closed"):
        return False
    try:
        os.kill(int(data["pid"]), 0)
        return False
    except (KeyError, TypeError, ValueError, OSError):
        return True


def reminder(project: str, cwd: str, client: str = "") -> str:
    """UserPromptSubmit hook output when the waiter is gone: Claude starts it before the request."""
    return (
        f"agent-gateway: the waiter of this session ('{project}') is not running (the app restarted?), "
        "so Telegram work cannot wake you. Before anything else, start it once with the Bash tool, "
        f"run_in_background true: `{command(project, cwd, client)}`. Then do the user's request."
    )


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def wait(project: str, cwd: str, client: str, port: int, interval: float = 30,
         agent: str = "claude_code", notify=None) -> int:  # fmt: skip
    """notify(text) -> bool: wake the agent and keep waiting (Antigravity); None: print and exit."""
    from app.channel import ChannelHub

    sid = ChannelHub.pull_id(project, agent, cwd)
    base = {"project": project, "agent": agent, "cwd": cwd, "client": client or "app"}
    WAITERS.mkdir(parents=True, exist_ok=True)
    path = WAITERS / f"{sid}.json"
    old = {k: v for k, v in _read(path).items() if k != "closed"}  # started again: open again
    path.write_text(json.dumps({**old, "pid": os.getpid()}))
    failures = 0  # wake-ups in a row that did not reach the agent (notify mode)
    while True:
        data = _read(path)
        if data.get("pid") != os.getpid():
            print("Replaced by a newer waiter for this session: do not restart this one.")
            return 0
        # a message already woken for is waited past, until it is due for a reminder
        recent = time.time() - data.get("at", 0) <= REMIND_S
        query = urllib.parse.urlencode(
            {**base, "after": (data.get("woke") or "") if recent else ""}
        )
        url = f"http://127.0.0.1:{port}/api/v1/channel/wait?{query}"
        try:
            with urllib.request.urlopen(url, timeout=60) as r:  # noqa: S310 - fixed local URL
                state = json.loads(r.read())
        except (OSError, ValueError):
            time.sleep(interval)  # gateway down or restarting: keep waiting, the operator starts it
            continue
        if state.get("closing"):
            path.write_text(json.dumps({"closed": True}))  # the prompt hook must not restart it
            print("The operator closed this session (/close): do not restart the waiter.")
            return 0
        nxt = state.get("next")
        if state.get("queued") and (nxt != data.get("woke") or not recent):
            text = wake_text(project, cwd, client, sid, nxt, agent)
            if notify is None:
                path.write_text(json.dumps({**data, "woke": nxt, "at": time.time()}))
                print(text, flush=True)
                return 0
            if notify(text):  # woken in place; a failed one is tried again next round
                path.write_text(json.dumps({**data, "woke": nxt, "at": time.time()}))
                failures = 0
            else:
                failures += 1
                if failures >= 3:  # the conversation or the app is gone: nothing left to wake
                    print(
                        "Could not wake the conversation 3 times: stopping (start it again from there)."
                    )
                    return 1
                time.sleep(interval)
