"""Open a project remotely: find its folder under PROJECTS_ROOT and start `agw session` in a new
Terminal window, so the Claude Code session starts its pull loop (and push channel) by itself.

Only direct sub-folders of PROJECTS_ROOT can be opened (no paths from the request), and only by an
allowlisted Telegram user. A folder not yet connected to the gateway gets `repo-setup` first.
"""

from __future__ import annotations

import difflib
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

CHANNEL = "agent-gateway-channel"


def _norm(text: str) -> str:
    return re.sub(r"[^0-9a-z]+", "", text.lower())


def project_dirs(root: Path) -> list[Path]:
    """Candidate projects: direct sub-folders of the root that are projects (projects.is_project)."""
    from app.projects import is_project

    if not root.is_dir():
        return []
    return sorted(
        (d for d in root.iterdir() if not d.name.startswith(".") and is_project(d)),
        key=lambda d: d.name.lower(),
    )


def find_projects(root: Path, query: str) -> list[Path]:
    """Exact name, then prefix, then substring, then close spellings (typos). Best matches only."""
    dirs = project_dirs(root)
    q = _norm(query)
    if not q:
        return []
    by = {d: _norm(d.name) for d in dirs}
    for test in (lambda n: n == q, lambda n: n.startswith(q), lambda n: q in n):
        hits = [d for d, n in by.items() if test(n)]
        if hits:
            return hits
    close = difflib.get_close_matches(q, list(by.values()), n=5, cutoff=0.75)
    return [d for d, n in by.items() if n in close]


def is_connected(repo: Path) -> bool:
    try:
        return CHANNEL in json.loads((repo / ".mcp.json").read_text()).get("mcpServers", {})
    except (OSError, ValueError):
        return False


def terminal_command(repo: Path, agent: str = "") -> str:
    agw = Path(sys.executable).parent / "agw"
    extra = f" --agent {shlex.quote(agent)}" if agent and agent != "claude_code" else ""
    return f"cd {shlex.quote(str(repo))} && {shlex.quote(str(agw))} session{extra}"


def open_in_terminal(repo: Path, agent: str = "") -> None:
    """New Terminal.app window running `agw session` in the repo (macOS asks once for permission)."""
    cmd = (
        terminal_command(repo, agent).replace("\\", "\\\\").replace('"', '\\"')
    )  # AppleScript string
    script = f'tell application "Terminal"\nactivate\ndo script "{cmd}"\nend tell'
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:300] or "osascript failed")


def open_project(root: Path, query: str, live_cwds=(), new: bool = False, token: bool = True,
                 launch=None, agent: str = "") -> tuple[str, list[Path]]:  # fmt: skip
    """-> (status, matches). status: none | many | already_open | opened | opened_connected."""
    hits = find_projects(root, query)
    if not hits:
        return "none", []
    if len(hits) > 1:
        return "many", hits
    repo = hits[0]
    if not new and str(repo) in {c.rstrip("/") for c in live_cwds}:
        return "already_open", hits
    status = "opened"
    from app.global_setup import installed as global_installed

    # first time and no global install: connect this repo (CLAUDE.md, .mcp.json; not committed)
    if not is_connected(repo) and not global_installed():
        from app.repo_setup import setup_repo

        # ponytail: assumes the gateway's default address; pass the real one if it ever moves
        setup_repo(repo, "http://127.0.0.1:8000/mcp", token=token)
        status = "opened_connected"
    if launch is not None or not agent:
        (launch or open_in_terminal)(repo)
    else:
        open_in_terminal(repo, agent)
    return status, hits
