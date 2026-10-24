"""Codex desktop has no loop of its own. A SessionStart hook (installed by `agw setup --agents codex`)
starts one relay per project folder: every minute it asks the gateway about that folder's Codex session
(which keeps it listed) and pushes new Telegram work into the open desktop thread with `codex queue`.
SessionEnd stops it. State: var/codex-relays/<folder hash>.json = {"thread": ..., "pid": ...}."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
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
REPUSH_S = 600  # ponytail: same work is pushed again after 10 min, not tracked per message


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


def hook(event: dict) -> dict | None:
    """SessionStart / SessionEnd from Codex. Returns the SessionStart output (context for Codex)."""
    from app.projects import project_for

    cwd, thread = event.get("cwd") or os.getcwd(), event.get("session_id")
    project = project_for(Path(cwd))
    if not project or not thread:
        return None
    path = state_file(cwd)
    data = _read(path)
    if event.get("hook_event_name") == "SessionEnd":
        if data.get("thread") == thread:
            if _alive(data.get("pid")):
                os.kill(int(data["pid"]), signal.SIGTERM)
            path.unlink(missing_ok=True)
        return None
    RELAYS.mkdir(parents=True, exist_ok=True)
    data["thread"] = thread  # the newest thread of this folder gets the work
    if not _alive(data.get("pid")):
        agw = Path(sys.executable).parent / "agw"
        with open(path.with_suffix(".log"), "a") as log:
            data["pid"] = subprocess.Popen(  # noqa: S603 - our own CLI
                [str(agw), "codex-relay", "--cwd", cwd], stdin=subprocess.DEVNULL, stdout=log,
                stderr=log, start_new_session=True,
            ).pid  # fmt: skip
    path.write_text(json.dumps(data))
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
    prompt, pushed = (
        prompt.replace("reusing the session_id it returned before", f"session_id '{sid}'"),
        0.0,
    )
    while True:
        data = _read(path)
        if not once and data.get("pid") != os.getpid():  # stopped (SessionEnd) or replaced
            return 0
        try:
            with urllib.request.urlopen(url, timeout=10) as r:  # noqa: S310 - fixed local URL
                state = json.loads(r.read())
        except OSError:
            state = {}  # gateway down: try again next minute
        if state.get("closing"):
            path.unlink(missing_ok=True)
            return 0
        if state.get("queued") and time.time() - pushed > REPUSH_S:
            r = subprocess.run(  # noqa: S603
                [exe, "queue", "--thread", data["thread"], "--message", prompt],
                capture_output=True, text=True, timeout=120,
            )  # fmt: skip
            print(f"{time.strftime('%H:%M:%S')} pushed to {data['thread']}: exit {r.returncode} "
                  f"{(r.stderr or r.stdout).strip()[-200:]}", flush=True)  # fmt: skip
            pushed = time.time() if r.returncode == 0 else pushed
        if once:
            return 0
        time.sleep(60)
