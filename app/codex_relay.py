"""Codex desktop has no loop of its own. A SessionStart hook (installed by `agw setup --agents codex`)
records the folder's newest desktop thread; the prod gateway itself keeps that folder's Codex session
listed and pushes new Telegram work into the thread with `codex queue` within seconds (no relay
process, no model run while idle). The record outlives SessionEnd (work waits in the thread until it is
opened again) and is dropped when the thread is deleted or the operator closes the session.
State: var/codex-relays/<folder hash>.json = {"thread": ..., "cwd": ..., "pushed": <message id>}."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

# Codex installed with the ChatGPT desktop app is not on PATH.
CODEX_APP_CLI = (
    "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
)
RELAYS = Path(__file__).resolve().parents[1] / "var" / "codex-relays"


def codex_cli() -> str | None:
    return shutil.which("codex") or (CODEX_APP_CLI if Path(CODEX_APP_CLI).is_file() else None)


def state_file(cwd: str) -> Path:
    return RELAYS / f"{hashlib.sha256(cwd.encode()).hexdigest()[:12]}.json"


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _record(path: Path, **changes) -> None:
    """Write a folder record; dropping "pid" makes a relay process of the old design exit."""
    data = {k: v for k, v in _read(path).items() if k != "pid"}
    path.write_text(json.dumps({**data, **changes}))


def hook(event: dict) -> dict | None:
    """SessionStart / SessionEnd from Codex. Returns the SessionStart output (context for Codex)."""
    from app.projects import project_for

    cwd, thread = event.get("cwd") or os.getcwd(), event.get("session_id")
    project = project_for(Path(cwd))
    if not project or not thread:
        return None
    if event.get("hook_event_name") == "SessionEnd":
        # the record stays: work waits in the thread (codex queue) until it is opened again
        return None
    RELAYS.mkdir(parents=True, exist_ok=True)
    _record(
        state_file(cwd), thread=thread, cwd=cwd
    )  # the newest thread of this folder gets the work
    context = (
        f"agent-gateway: this folder is the project '{project}'; Telegram work for it reaches this "
        f"thread as a message asking you to call get_messages (agent 'codex', client 'app', cwd "
        f"'{cwd}'). Use project '{project}' for every agent-gateway call."
    )
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}


def codex_prompt(project: str, cwd: str) -> str:
    """What a pushed message asks the thread to do: fetch the exact folder session's work (Codex
    desktop otherwise drops cwd or reuses an old session_id)."""
    from app.channel import ChannelHub
    from app.repo_setup import loop_prompt

    sid = ChannelHub.pull_id(project, "codex", cwd)
    prompt = loop_prompt(project, "app", "codex").replace("this folder", cwd)
    prompt = prompt.replace("reusing the session_id it returned before", f"session_id '{sid}'")
    return prompt + " Then call get_messages again the same way until it has no new requests."


async def push_round(hub, relays: Path, refresh: bool) -> None:
    """One round: `refresh` keeps every recorded folder's session listed (and drops the record when the
    operator closed it); new queued work is pushed once into the folder's thread."""
    from app.projects import project_for

    exe = codex_cli()
    for path in relays.glob("*.json") if relays.is_dir() else []:
        data = _read(path)
        cwd, thread = data.get("cwd"), data.get("thread")
        project = cwd and thread and project_for(Path(cwd))
        if not project:
            continue
        sid = hub.pull_id(project, "codex", cwd)
        if refresh or sid not in hub.sessions:
            if hub.peek(project, "codex", cwd, "app")["closing"]:
                path.unlink(missing_ok=True)
                continue
        s = hub.sessions.get(sid)
        nxt = hub._next(s) if s is not None else None
        # pushed once per (oldest, newest) queued pair: the thread taking one (oldest changes) or a
        # new request behind an unanswered push (newest changes) pushes again
        mark = (
            nxt and f"{nxt}|{s.queue._queue[-1]['message_id']}"
        )  # ponytail: asyncio.Queue internals
        if nxt is None or mark == data.get("pushed") or exe is None:
            continue
        proc = await asyncio.create_subprocess_exec(
            exe, "queue", "--thread", thread, "--message", codex_prompt(project, cwd),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )  # fmt: skip
        try:
            out = (await asyncio.wait_for(proc.communicate(), 120))[0].decode(errors="ignore")
        except TimeoutError:
            proc.kill()
            out = "timed out"
        log.info(
            "codex queue %s -> %s: exit %s %s", nxt, thread, proc.returncode, out.strip()[-200:]
        )
        if "no rollout found" in out:  # the thread was deleted: a new thread records itself again
            path.unlink(missing_ok=True)
        elif proc.returncode == 0:
            _record(path, pushed=mark)  # pushed once, also across gateway restarts


async def push_loop(hub, relays: Path, every: float = 2, refresh_every: int = 30) -> None:
    """Prod gateway task: new Codex work reaches the open desktop thread within seconds; sessions are
    refreshed every `every * refresh_every` seconds (1 min), well inside PULL_TTL. relays: the
    gateway's DATA_DIR/codex-relays, which is RELAYS for prod (tests use their own folder)."""
    for path in relays.glob("*.json") if relays.is_dir() else []:
        _record(path)  # stop relay processes of the old design (they exit when their pid is gone)
    n = 0
    while True:
        try:
            await push_round(hub, relays, refresh=n % refresh_every == 0)
        except Exception:
            log.exception("codex push round failed")
        n += 1
        await asyncio.sleep(every)
