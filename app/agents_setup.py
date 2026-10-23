"""`agw setup --agents ...`: connect other coding agents to the gateway (global MCP config + global
instructions), using each agent's documented format (checked 2026-10-22):

  opencode     ~/.config/opencode/opencode.json  mcp.<name> = {type: remote, url, enabled}
               ~/.config/opencode/AGENTS.md
  codex        ~/.codex/config.toml               [mcp_servers.<name>] url = ...   (marked block, TOML checked)
               ~/.codex/AGENTS.md
  antigravity  ~/.gemini/config/mcp_config.json   mcpServers.<name> = {serverUrl}  (Gemini CLI's successor;
               ~/.gemini/GEMINI.md                                                  url/httpUrl are not read)
  cursor       ~/.cursor/mcp.json                 mcpServers.<name> = {url}
               (global rules are UI-only: the instructions are printed to paste into Cursor > Rules)

Every file is backed up once (<file>.agw-backup) before the first change; remove() takes out only the
gateway's own entries. Claude Code is handled by app.global_setup.
"""

from __future__ import annotations

import json
import re
import shutil
import tomllib
from pathlib import Path

from app.config import require_prod

SERVER = "agent-gateway"
START, END = "<!-- agent-gateway:start (managed by `agw setup`) -->", "<!-- agent-gateway:end -->"
TOML_START, TOML_END = "# agent-gateway:start (managed by `agw setup`)", "# agent-gateway:end"
AGENTS = ("opencode", "codex", "antigravity", "cursor")
ALIASES = {
    "gemini": "antigravity",
    "agy": "antigravity",
    "claude": "claude",
    "claude_code": "claude",
}

INSTRUCTIONS = """## Agent gateway (Telegram control)

This machine runs a local agent gateway: MCP server `agent-gateway` (http://127.0.0.1:8000/mcp),
connected to the operator's Telegram group. It applies to every git repo directly under the projects
folder (`~/Working`). Project name: the README "Game Identity" as `Name (CODE)`, else the README
title, else the folder name.

- **Task finished** in such a repo: call `submit_report(project, text, agent_status)`. The text is a
  title line plus at most 5 short bullets (changed / validated / not done), with no logs.
- **Work from Telegram**: when the user asks you to check Telegram or take work, call
  `get_messages(project, agent="{agent}", session_id=<the id it returned before>)`.
  - Do each request, then answer it with `reply_message(session_id, message_id, text)`.
  - `meta.kind: "answer"` answers your own `ask_agent`: use it, no reply.
- **Other agents**: `list_agents` shows the open sessions. To ask one, call
  `ask_agent(to=s#|project, request, session_id)`; check the answer with `get_ask`. The operator
  steps in on Telegram if nobody answers.
- **Reviews and research**: `review_project(project, mode, role, agent, evidence)` runs the game-review
  skill on a fresh read-only agent; poll `get_review(review_id)`, then read the reports in `files`.
- **Questions** for the operator: `request_human_input(question, project)`, then poll
  `get_human_response`.
- **Game art**: call `generate_art(prompt, purpose, requirements, width, height, project)`. The
  operator approves and reviews each image; use `get_task_result(...).images` only after
  `review.state == "accepted"`. 3D models: `output="model3d"` (+ `image_path`, `model_format`);
  the file is `models[0].path` once accepted.
- Confirm with the user before commits, pushes, deploys or anything destructive.
"""


def home() -> Path:
    return Path.home()


def paths(agent: str) -> dict[str, Path | None]:
    h = home()
    return {
        "opencode": {
            "mcp": h / ".config/opencode/opencode.json",
            "rules": h / ".config/opencode/AGENTS.md",
        },
        "codex": {"mcp": h / ".codex/config.toml", "rules": h / ".codex/AGENTS.md"},
        "antigravity": {
            "mcp": h / ".gemini/config/mcp_config.json",
            "rules": h / ".gemini/GEMINI.md",
        },
        "cursor": {"mcp": h / ".cursor/mcp.json", "rules": None},
    }[agent]


def detected() -> list[str]:
    """Agents with a config folder on this machine (they are, or were, installed)."""
    h = home()
    marks = {"opencode": h / ".config/opencode", "codex": h / ".codex", "antigravity": h / ".gemini",
             "cursor": h / ".cursor"}  # fmt: skip
    return [a for a in AGENTS if marks[a].exists()]


def resolve(names: str) -> list[str]:
    """'opencode,codex' / 'all' / 'detected' -> agent names (claude is handled separately)."""
    out = []
    for raw in names.split(","):
        n = ALIASES.get(raw.strip().lower(), raw.strip().lower())
        if n == "all":
            out += list(AGENTS)
        elif n == "detected":
            out += detected()
        elif n in AGENTS or n == "claude":
            out.append(n)
        elif n:
            raise ValueError(
                f"unknown agent {raw!r} (known: {', '.join(AGENTS)}, claude, all, detected)"
            )
    return list(dict.fromkeys(out))


def _backup(path: Path) -> None:
    b = path.with_name(path.name + ".agw-backup")
    if path.exists() and not b.exists():
        shutil.copy2(path, b)


def _json(path: Path) -> dict:
    if not path.exists() or not path.read_text().strip():
        return {}
    return json.loads(path.read_text())


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def _block(path: Path, text: str | None, start: str = START, end: str = END) -> None:
    """Put text between the markers (replacing an earlier block); text=None removes the block."""
    old = path.read_text() if path.exists() else ""
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end) + r"\n?", re.S)
    base = pattern.sub("", old).rstrip()
    if text is not None:
        base = (base + "\n\n" if base else "") + f"{start}\n{text.strip()}\n{end}"
    if base or path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(base + "\n" if base else "")


def configured(agent: str) -> bool:
    p = paths(agent)["mcp"]
    if not p.exists():
        return False
    text = p.read_text()
    return (TOML_START in text) if agent == "codex" else (f'"{SERVER}"' in text)


def install(agent: str, url: str = "http://127.0.0.1:8000/mcp", token: bool | None = None) -> str:
    """token (default: MCP_REQUIRE_TOKEN): send `Authorization: Bearer $AGW_API_TOKEN`. The files name
    the variable, never the secret; the agent needs AGW_API_TOKEN in its environment."""
    require_prod("agent setup")
    if token is None:
        from app.config import get_settings

        token = get_settings().mcp_require_token
    # ponytail: env-reference syntax from each tool's docs, checked only for opencode on this Mac
    p = paths(agent)
    mcp = p["mcp"]
    _backup(mcp)
    if agent == "opencode":
        data = _json(mcp)
        data.setdefault("$schema", "https://opencode.ai/config.json")
        data.setdefault("mcp", {})[SERVER] = {"type": "remote", "url": url, "enabled": True}
        if token:
            data["mcp"][SERVER]["headers"] = {"Authorization": "Bearer {env:AGW_API_TOKEN}"}
        _write_json(mcp, data)
    elif agent == "codex":
        old = mcp.read_text() if mcp.exists() else ""
        outside = re.sub(re.escape(TOML_START) + r".*?" + re.escape(TOML_END), "", old, flags=re.S)
        if re.search(rf"^\[mcp_servers\.{re.escape(SERVER)}\]", outside, re.M):
            return (
                f"{mcp}: [mcp_servers.{SERVER}] already exists (not managed here); left unchanged"
            )
        auth = '\nbearer_token_env_var = "AGW_API_TOKEN"' if token else ""
        _block(mcp, f'[mcp_servers.{SERVER}]\nurl = "{url}"\nstartup_timeout_sec = 10\ntool_timeout_sec = 120{auth}',
               TOML_START, TOML_END)  # fmt: skip
        try:
            tomllib.loads(mcp.read_text())
        except tomllib.TOMLDecodeError as e:  # never leave Codex with a broken config
            mcp.write_text(old)
            raise ValueError(f"{mcp} would not be valid TOML ({e}); left unchanged") from e
    elif agent == "antigravity":
        data = _json(mcp)
        data.setdefault("mcpServers", {})[SERVER] = {"serverUrl": url}
        if token:
            data["mcpServers"][SERVER]["headers"] = {"Authorization": "Bearer ${AGW_API_TOKEN}"}
        _write_json(mcp, data)
    elif agent == "cursor":
        data = _json(mcp)
        data.setdefault("mcpServers", {})[SERVER] = {"url": url}
        if token:
            data["mcpServers"][SERVER]["headers"] = {"Authorization": "Bearer ${env:AGW_API_TOKEN}"}
        _write_json(mcp, data)
    rules = p["rules"]
    if rules is not None:
        _backup(rules)
        _block(rules, INSTRUCTIONS.format(agent=agent))
        return f"{agent}: MCP in {mcp}, instructions in {rules}; restart {agent}"
    return (f"{agent}: MCP in {mcp}; Cursor keeps global rules in its UI: paste the text from "
            "`agw setup --agents cursor --print-rules` into Cursor > Settings > Rules; restart Cursor")  # fmt: skip


def remove(agent: str) -> str:
    require_prod("agent setup")
    p = paths(agent)
    mcp = p["mcp"]
    if mcp.exists():
        if agent == "codex":
            _block(mcp, None, TOML_START, TOML_END)
        else:
            data = _json(mcp)
            data.get("mcp" if agent == "opencode" else "mcpServers", {}).pop(SERVER, None)
            _write_json(mcp, data)
    if p["rules"] is not None and p["rules"].exists():
        _block(p["rules"], None)
    return f"{agent}: gateway entries removed"


def rules_text(agent: str) -> str:
    return INSTRUCTIONS.format(agent=agent)
