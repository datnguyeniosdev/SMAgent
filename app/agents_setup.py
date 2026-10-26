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
import shlex
import shutil
import sys
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
  title line plus at most 5 short bullets (changed / validated / not done), at most 500 characters, no logs, in polite
  Vietnamese ("gọi dạ bảo vâng": title starts with "Dạ,", "ạ" where natural).
- **After a build or deploy** (also one asked for via Telegram `/send`): call `report_deployment` with
  `notes` = a short Vietnamese release note (what is new or fixed for testers) and `url` = the link
  to the build (Firebase App Distribution, TestFlight, web or store page).
- **Work from Telegram**: when the user asks you to check Telegram or take work, call
  `get_messages(project, agent="{agent}", session_id=<the id it returned before>)`.
  - Do each request, then answer it with `reply_message(session_id, message_id, text)`.
  - `meta.kind: "answer"` answers your own `ask_agent`: use it, no reply.
- **Other agents**: `list_agents` shows the open sessions. To ask one, call
  `ask_agent(to=s#|project, request, session_id)`; the answer arrives through `get_messages`
  (`meta.kind: "answer"`), `get_ask` shows it too. The operator
  steps in on Telegram if nobody answers.
- **Reviews and research**: `review_project(project, mode, role, agent, evidence)` runs the game-review
  skill on a fresh read-only agent; pass `session_id`: the result (report paths) arrives through
  `get_messages` as an "answer" message, no polling (`get_review(review_id)` shows it too).
- **Every confirmation or decision** (commit, push, merge, deploy, a choice, a yes/no) also goes to
  Telegram: `request_human_input(question, project, session_id, options=[2-4 short button labels])`
  in polite Vietnamese; the answer comes back through `get_messages` (`meta.kind: "answer"`). If the
  user answers in your session first, call `close_question(request_id, answer)` at once.
  Release, deploy, commit, push, merge and git pull: add `secure=true` (Telegram approval needs the
  operator's passcode); go ahead only on "✅ Duyệt (đã xác minh passcode)" or an approval in your
  session; a 🔒 locked answer is not approval.
- **Game art**: call `generate_art(prompt, purpose, requirements, width, height, project, session_id)`.
  The operator approves and reviews each image; the decision arrives through `get_messages` as an
  "answer" message (no polling). Use `get_task_result(...).images` only once accepted. 3D models:
  `output="model3d"` (+ `image_path`, `model_format`); the file is `models[0].path` once accepted.
- Confirm with the user before commits, pushes, deploys or anything destructive.
"""


ANTIGRAVITY_WAKE = """
- **Telegram work wakes you (Antigravity)**: at the start of each conversation in a project folder
  under `~/Working`, start the gateway waiter once as a background terminal command you do not wait
  for: `{agw} wait --agent antigravity --cwd <the workspace folder>`. It keeps running and sends a
  message into this conversation when Telegram work arrives; then call `get_messages` with the
  session_id that message names, do the request and answer with `reply_message`, then start the
  waiter again the same way (the new turn stops it). Never run two at once.
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
        "codex": {
            "mcp": h / ".codex/config.toml",
            "rules": h / ".codex/AGENTS.md",
            "skills": h / ".codex/skills",
            "hooks": h / ".codex/hooks.json",
        },  # fmt: skip
        "antigravity": {
            "mcp": h / ".gemini/config/mcp_config.json",
            "rules": h / ".gemini/GEMINI.md",
        },
        "cursor": {"mcp": h / ".cursor/mcp.json", "rules": None},
    }[agent]


def _skills_src() -> Path:
    return home() / ".claude/skills"


def link_skills(dst: Path) -> list[str]:
    """The Claude Code skills (agent-gateway, game-review, ...) for an agent that reads the same
    SKILL.md format: symlinks, so an edit or update reaches every agent. Own skills there stay."""
    src = _skills_src()
    if not src.is_dir():
        return []
    dst.mkdir(parents=True, exist_ok=True)
    linked = []
    for d in sorted(src.iterdir()):
        t = dst / d.name
        if (d / "SKILL.md").is_file() and not t.exists() and not t.is_symlink():
            t.symlink_to(d, target_is_directory=True)
        if t.is_symlink() and t.resolve() == d.resolve():
            linked.append(d.name)
    return linked


def codex_hooks(path: Path, install: bool) -> None:
    """SessionStart/SessionEnd -> `agw codex-hook` (the Codex desktop relay). Other hooks stay."""
    cmd = f"{shlex.quote(str(Path(sys.executable).parent / 'agw'))} codex-hook"
    data = _json(path)
    hooks = data.setdefault("hooks", {})
    for event in ("SessionStart", "SessionEnd"):
        groups = [g for g in hooks.get(event, [])
                  if not any("codex-hook" in h.get("command", "") for h in g.get("hooks", []))]  # fmt: skip
        if install:
            groups.append({"hooks": [{"type": "command", "command": cmd}]})
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    _write_json(path, data)


def unlink_skills(dst: Path) -> None:
    src = _skills_src().resolve()
    for t in dst.iterdir() if dst.is_dir() else ():
        if t.is_symlink() and t.resolve().parent == src:
            t.unlink()


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


def _codex_strip(text: str) -> str:
    """config.toml without the gateway's markers and its [mcp_servers.<SERVER>] table. Only those:
    the Codex app rewrites the file and may move its own tables in between the markers."""
    text = re.sub(
        rf"^(?:{re.escape(TOML_START)}|{re.escape(TOML_END)})[ \t]*\n?", "", text, flags=re.M
    )
    table = rf"^\[mcp_servers\.{re.escape(SERVER)}\][^\n]*\n(?:(?!\[)[^\n]*(?:\n|$))*"
    return re.sub(table, "", text, flags=re.M).strip()


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
        if TOML_START not in old and re.search(
            rf"^\[mcp_servers\.{re.escape(SERVER)}\]", old, re.M
        ):
            return (
                f"{mcp}: [mcp_servers.{SERVER}] already exists (not managed here); left unchanged"
            )
        auth = '\nbearer_token_env_var = "AGW_API_TOKEN"' if token else ""
        # the gateway's own tools run without a prompt (codex exec cannot answer one)
        table = (f'[mcp_servers.{SERVER}]\nurl = "{url}"\nstartup_timeout_sec = 10\ntool_timeout_sec = 120'
                 f'\ndefault_tools_approval_mode = "approve"{auth}')  # fmt: skip
        base = _codex_strip(old)
        mcp.parent.mkdir(parents=True, exist_ok=True)
        mcp.write_text((base + "\n\n" if base else "") + f"{TOML_START}\n{table}\n{TOML_END}\n")
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
        text = INSTRUCTIONS.format(agent=agent)
        if (
            agent == "antigravity"
        ):  # its desktop agent wakes itself through agentapi (app/waiter.py)
            text += ANTIGRAVITY_WAKE.format(agw=Path(sys.executable).parent / "agw")
        _block(rules, text)
        skills = (
            f", skills ({', '.join(link_skills(p['skills']))}) in {p['skills']}"
            if p.get("skills")
            else ""
        )
        if p.get("hooks"):
            codex_hooks(p["hooks"], install=True)
            skills += (f", desktop relay hook in {p['hooks']} (approve it once when Codex asks to "
                       "review the new hook)")  # fmt: skip
        return f"{agent}: MCP in {mcp}, instructions in {rules}{skills}; restart {agent}"
    return (f"{agent}: MCP in {mcp}; Cursor keeps global rules in its UI: paste the text from "
            "`agw setup --agents cursor --print-rules` into Cursor > Settings > Rules; restart Cursor")  # fmt: skip


def remove(agent: str) -> str:
    require_prod("agent setup")
    p = paths(agent)
    mcp = p["mcp"]
    if mcp.exists():
        if agent == "codex":
            base = _codex_strip(mcp.read_text())
            mcp.write_text(base + "\n" if base else "")
        else:
            data = _json(mcp)
            data.get("mcp" if agent == "opencode" else "mcpServers", {}).pop(SERVER, None)
            _write_json(mcp, data)
    if p["rules"] is not None and p["rules"].exists():
        _block(p["rules"], None)
    if p.get("skills"):
        unlink_skills(p["skills"])
    if p.get("hooks") and p["hooks"].exists():
        codex_hooks(p["hooks"], install=False)
    return f"{agent}: gateway entries removed"


def rules_text(agent: str) -> str:
    return INSTRUCTIONS.format(agent=agent)
