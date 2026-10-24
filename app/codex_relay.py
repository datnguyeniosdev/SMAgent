"""Codex desktop has no loop of its own. A SessionStart hook (installed by `agw setup --agents codex`)
starts one relay per project folder: every minute it asks the gateway about that folder's Codex session
(which keeps it listed) and pushes new Telegram work into the open desktop thread with `codex queue`.
The relay outlives SessionEnd (work waits in the thread until it is opened again) and stops when the
thread is deleted or the operator closes the session; `agw dev` restarts relays after a reboot.
State: var/codex-relays/<folder hash>.json = {"thread": ..., "pid": ..., "cwd": ...}."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

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


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (TypeError, ValueError, OSError):
        return False


def _start(cwd: str, thread: str) -> None:
    """Record the folder's thread and run its relay unless one is running."""
    path = state_file(cwd)
    data = {**_read(path), "thread": thread, "cwd": cwd}
    if not _alive(data.get("pid")):
        agw = Path(sys.executable).parent / "agw"
        with open(path.with_suffix(".log"), "a") as log:
            data["pid"] = subprocess.Popen(  # noqa: S603 - our own CLI
                [str(agw), "codex-relay", "--cwd", cwd], stdin=subprocess.DEVNULL, stdout=log,
                stderr=log, start_new_session=True,
            ).pid  # fmt: skip
    path.write_text(json.dumps(data))


def resume_all() -> None:
    """`agw dev`: restart the relays that are not running (e.g. after a reboot)."""
    from app.projects import repo_for

    for path in RELAYS.glob("*.json") if RELAYS.is_dir() else []:
        data = _read(path)
        cwd = data.get("cwd")
        if cwd and data.get("thread") and not _alive(data.get("pid")) and repo_for(Path(cwd)):
            _start(cwd, data["thread"])


def hook(event: dict) -> dict | None:
    """SessionStart / SessionEnd from Codex. Returns the SessionStart output (context for Codex)."""
    from app.projects import project_for

    cwd, thread = event.get("cwd") or os.getcwd(), event.get("session_id")
    project = project_for(Path(cwd))
    if not project or not thread:
        return None
    if event.get("hook_event_name") == "SessionEnd":
        # the relay stays: work waits in the thread (codex queue) until it is opened again
        return None
    RELAYS.mkdir(parents=True, exist_ok=True)
    _start(cwd, thread)  # the newest thread of this folder gets the work
    context = (
        f"agent-gateway: this folder is the project '{project}'; Telegram work for it reaches this "
        f"thread as a message asking you to call get_messages (agent 'codex', client 'app', cwd "
        f"'{cwd}'). Use project '{project}' for every agent-gateway call."
    )
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}


def relay(cwd: str, port: int, once: bool = False) -> int:
    """Keep the folder's Codex session listed; push new work into the desktop thread."""
    from app.projects import project_for
    from app.repo_setup import loop_prompt

    project, path, exe = project_for(Path(cwd)), state_file(cwd), codex_cli()
    if not project or not exe:
        print(f"relay for {cwd}: {'no project' if not project else 'codex not found'}")
        return 1
    query = urllib.parse.urlencode(
        {"project": project, "agent": "codex", "cwd": cwd, "client": "app"}
    )
    url = f"http://127.0.0.1:{port}/api/v1/channel/peek?{query}"
    from app.channel import ChannelHub

    # the exact session: Codex desktop otherwise drops cwd or reuses an old session_id
    sid = ChannelHub.pull_id(project, "codex", cwd)
    prompt = loop_prompt(project, "app", "codex").replace("this folder", cwd)
    prompt = prompt.replace("reusing the session_id it returned before", f"session_id '{sid}'")
    pushed = None  # the queued message last pushed: each new one is pushed once
    while True:
        data = _read(path)
        if not once and data.get("pid") != os.getpid():  # replaced by a newer relay
            return 0
        try:
            with urllib.request.urlopen(url, timeout=10) as r:  # noqa: S310 - fixed local URL
                state = json.loads(r.read())
        except OSError:
            state = {}  # gateway down: try again next minute
        if state.get("closing"):
            path.unlink(missing_ok=True)
            return 0
        if state.get("queued") and state.get("next") != pushed:
            r = subprocess.run(  # noqa: S603
                [exe, "queue", "--thread", data["thread"], "--message", prompt],
                capture_output=True, text=True, timeout=120,
            )  # fmt: skip
            out = (r.stderr or r.stdout).strip()
            print(f"{time.strftime('%H:%M:%S')} pushed to {data['thread']}: exit {r.returncode} "
                  f"{out[-200:]}", flush=True)  # fmt: skip
            if "no rollout found" in out:  # the thread was deleted: a new one restarts the relay
                path.unlink(missing_ok=True)
                return 0
            pushed = state.get("next") if r.returncode == 0 else pushed
        if once:
            return 0
        time.sleep(60)
