"""Cursor (installed by `agw setup --agents cursor` into ~/.cursor/hooks.json) runs `agw cursor-hook` with
the event as JSON on stdin and reads JSON from stdout:

sessionStart  -> {"additional_context": ...}: the gateway rules and this folder's project, so nothing has to
                 be pasted into Cursor's rules UI.
stop          -> waits (long-poll, no model run) for Telegram work for this folder's Cursor session; when
                 some is queued it returns {"followup_message": ...}, which Cursor submits as the next
                 user message, so the conversation picks the work up by itself. After that turn, stop runs
                 again and waits again. Nothing queued within HOLD_S: {} (Cursor stays idle).
State: var/waiters/<session id>.json, shared with app/waiter.py (the message last woken for)."""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

HOLD_S = 3300  # under the hook's 3600 s timeout in hooks.json
REMIND_S = 600


def _folder(event: dict) -> str:
    roots = event.get("workspace_roots") or []
    return str(Path(roots[0]).resolve()) if roots else ""


def session_start(event: dict) -> dict:
    from app.agents_setup import INSTRUCTIONS
    from app.channel import ChannelHub
    from app.projects import project_for

    cwd = _folder(event)
    project = cwd and project_for(Path(cwd))
    if not project:
        return {}
    sid = ChannelHub.pull_id(project, "cursor", cwd)
    return {
        "additional_context": (
            f"agent-gateway: this folder is the project '{project}'. Use it for every agent-gateway call "
            f"(agent 'cursor', client 'app', cwd '{cwd}', session_id '{sid}'). Telegram work reaches "
            "this conversation by itself as a message asking you to call get_messages; answer it with "
            "reply_message.\n\n" + INSTRUCTIONS.format(agent="cursor")
        )
    }


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def stop(event: dict, port: int, hold: float = HOLD_S) -> dict:
    """Wait for this folder's Cursor work; the wake-up text when some is queued, else {}."""
    from app.channel import ChannelHub
    from app.projects import project_for
    from app.waiter import WAITERS, wake_text

    if event.get("status") == "aborted":  # the user stopped the agent: do not start it again
        return {}
    cwd = _folder(event)
    project = cwd and project_for(Path(cwd))
    if not project:
        return {}
    sid = ChannelHub.pull_id(project, "cursor", cwd)
    WAITERS.mkdir(parents=True, exist_ok=True)
    path = WAITERS / f"{sid}.json"
    base = {"project": project, "agent": "cursor", "cwd": cwd, "client": "app"}
    end = time.monotonic() + hold
    while time.monotonic() < end:
        data = _read(path)
        recent = time.time() - data.get("at", 0) <= REMIND_S
        after = (data.get("woke") or "") if recent else ""
        left = max(1, min(25, int(end - time.monotonic())))
        query = urllib.parse.urlencode({**base, "after": after, "hold": left})
        try:
            url = f"http://127.0.0.1:{port}/api/v1/channel/wait?{query}"
            with urllib.request.urlopen(url, timeout=left + 30) as r:  # noqa: S310 - fixed local URL
                state = json.loads(r.read())
        except (OSError, ValueError):
            time.sleep(min(30, max(0, end - time.monotonic())))  # gateway down: keep waiting
            continue
        if state.get("closing"):
            return {}
        nxt = state.get("next")
        if state.get("queued") and (nxt != data.get("woke") or not recent):
            path.write_text(json.dumps({**data, "woke": nxt, "at": time.time()}))
            text = wake_text(project, cwd, "app", sid, nxt, "cursor")
            return {"followup_message": text}
    return {}


def hook(event: dict, port: int) -> dict:
    name = event.get("hook_event_name") or ""
    if name == "sessionStart":
        return session_start(event)
    if name == "stop":
        return stop(event, port)
    return {}
