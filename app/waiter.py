"""`agw wait`: the pull loop of a Claude Code session without a paid model run per minute. Claude runs it
with Bash `run_in_background`; it asks the gateway every 30 s (peek: no model, keeps the session listed)
and exits only when a new request is queued, which wakes Claude with what to do. A gateway that is down
is waited out silently (the operator starts it). The newest waiter of a session wins; an older one exits
quietly. State: var/waiters/<session id>.json = {"pid": ..., "woke": <message id>, "at": <time>}."""

from __future__ import annotations

import json
import os
import shlex
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

WAITERS = Path(__file__).resolve().parents[1] / "var" / "waiters"
REMIND_S = 600  # a request still queued after a wake-up (ignored or lost) wakes Claude again


def command(project: str, cwd: str, client: str = "") -> str:
    """The shell command Claude runs in the background (absolute agw: hooks run outside the venv)."""
    agw = Path(sys.executable).parent / "agw"
    cmd = f"{shlex.quote(str(agw))} wait --project {shlex.quote(project)} --cwd {shlex.quote(cwd)}"
    return f"{cmd} --client {client}" if client else cmd


def wake_text(project: str, cwd: str, client: str, sid: str, message_id: str) -> str:
    from app.repo_setup import loop_prompt

    prompt = loop_prompt(project, client, "claude_code").replace("this folder", f"'{cwd}'")
    prompt = prompt.replace("reusing the session_id it returned before", f"session_id '{sid}'")
    return (
        f"agent-gateway has work for '{project}' (message {message_id}). {prompt} When you have "
        f"answered, start this waiter again with the Bash tool, run_in_background true: "
        f"`{command(project, cwd, client)}`. If the get_messages tool is missing or fails, tell the "
        "user in one line to run /mcp and reconnect agent-gateway."
    )


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def wait(project: str, cwd: str, client: str, port: int, interval: float = 30) -> int:
    from app.channel import ChannelHub

    sid = ChannelHub.pull_id(project, "claude_code", cwd)
    query = urllib.parse.urlencode(
        {"project": project, "agent": "claude_code", "cwd": cwd, "client": client or "app"}
    )
    url = f"http://127.0.0.1:{port}/api/v1/channel/peek?{query}"
    WAITERS.mkdir(parents=True, exist_ok=True)
    path = WAITERS / f"{sid}.json"
    path.write_text(json.dumps({**_read(path), "pid": os.getpid()}))
    while True:
        data = _read(path)
        if data.get("pid") != os.getpid():
            print("Replaced by a newer waiter for this session: do not restart this one.")
            return 0
        try:
            with urllib.request.urlopen(url, timeout=10) as r:  # noqa: S310 - fixed local URL
                state = json.loads(r.read())
        except (OSError, ValueError):
            state = {}  # gateway down or restarting: keep waiting, the operator starts it
        if state.get("closing"):
            path.unlink(missing_ok=True)
            print("The operator closed this session (/close): do not restart the waiter.")
            return 0
        nxt = state.get("next")
        if state.get("queued") and (
            nxt != data.get("woke") or time.time() - data.get("at", 0) > REMIND_S
        ):
            path.write_text(json.dumps({**data, "woke": nxt, "at": time.time()}))
            print(wake_text(project, cwd, client, sid, nxt), flush=True)
            return 0
        time.sleep(interval)
