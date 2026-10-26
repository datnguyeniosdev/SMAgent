"""Which project is this folder? Standard library only: the channel shim and the SessionStart hook run
it at every Claude Code session start, in any folder.

Connected projects are the git repositories directly under PROJECTS_ROOT (default: the folder that
contains this gateway). A session in such a repo, or in any sub-folder of it, belongs to that project.
"""

from __future__ import annotations

import re
from pathlib import Path

GATEWAY_DIR = Path(__file__).resolve().parents[1]
# README titles that hosting sites / templates generate: not a project name, use the folder name
_GENERIC_TITLE = re.compile(
    r"^(sample (gitlab|github) project|project title|readme|untitled|my project)$", re.I
)


def projects_root() -> Path:
    """PROJECTS_ROOT from the gateway's own .env (not the session's), else the gateway's parent."""
    env = GATEWAY_DIR / ".env"
    if env.exists():
        for line in env.read_text(errors="ignore").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "PROJECTS_ROOT" and value.strip():
                return Path(value.strip().strip("'\"")).expanduser()
    return GATEWAY_DIR.parent


def repo_for(cwd: Path, root: Path | None = None) -> Path | None:
    """The project folder directly under the projects root that contains cwd, or None (not
    connected). A project is a git repo, or a folder made from the project template before its
    `git init` (AGENTS.md or CLAUDE.md at the top), so a new project is attached from its first session."""
    root = (root or projects_root()).resolve()
    try:
        rel = cwd.resolve().relative_to(root)
    except ValueError:
        return None
    if not rel.parts:
        return None
    repo = root / rel.parts[0]
    return repo if any((repo / m).exists() for m in (".git", "AGENTS.md", "CLAUDE.md")) else None


def detect_project(repo: Path) -> str:
    """README "Game Identity" table (name + code), else the README title, else the folder name."""
    readme = repo / "README.md"
    text = readme.read_text(errors="replace") if readme.exists() else ""

    def cell(field: str) -> str | None:  # `value` (any note after it ignored) or the bare cell text
        m = re.search(rf"^\|\s*{field}\s*\|\s*(?:`([^`]+)`[^|]*|([^|`]+?))\s*\|", text, re.M)
        value = m and (m.group(1) or m.group(2)).strip()
        return value if value and "<" not in value else None  # skip unfilled template placeholders

    if name := cell("Game name"):
        return f"{name} ({code})" if (code := cell("Game code")) else name
    title = re.search(r"^# (.+)$", text, re.M)
    if title and not _GENERIC_TITLE.match(title.group(1).strip()):
        return title.group(1).strip()
    return repo.resolve().name


def project_for(cwd: Path) -> str | None:
    """Project name for a session started in cwd, or None when cwd is not in a connected repo."""
    repo = repo_for(cwd)
    return detect_project(repo) if repo else None
