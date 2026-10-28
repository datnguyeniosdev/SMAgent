"""`agw global-setup`: connect every git repo directly under PROJECTS_ROOT at once, through Claude Code's
user-level config instead of per-repo files. `agw global-setup --remove` undoes exactly what it added.

  user MCP            agent-gateway (HTTP) and agent-gateway-channel (stdio shim; project from the folder)
  ~/.claude/settings.json   allow the gateway tools; SessionStart hook `agw hook-session-start`
                            (silent outside connected repos; first change keeps a .agw-backup copy)
  ~/.claude/skills/agent-gateway/SKILL.md   how to report, take Telegram work, generate reviewed art

Per-repo files written by `agw repo-setup` keep working and take precedence where they exist.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from app.config import require_prod

SKILL = "agent-gateway"
PROJECT_REF = "the project named in the agent-gateway session note"


def claude_home() -> Path:
    """The Claude Code account being set up: CLAUDE_CONFIG_DIR (one folder per account), else ~/.claude."""
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()


def skill_file() -> Path:
    return claude_home() / "skills" / SKILL / "SKILL.md"


def installed() -> bool:
    return skill_file().exists()


def _agw() -> Path:
    return Path(sys.executable).parent / "agw"


def hook_command() -> str:
    return f'"{_agw()}" hook-session-start'


def prompt_hook_command() -> str:
    return f'"{_agw()}" hook-prompt'


def failure_hook_command() -> str:
    return f'"{_agw()}" hook-failure'


def tool_hook_command() -> str:
    return f'"{_agw()}" hook-tool'


HOOKS = {"SessionStart": hook_command, "UserPromptSubmit": prompt_hook_command,
         "StopFailure": failure_hook_command, "PreToolUse": tool_hook_command}  # fmt: skip
MATCHERS = {"PreToolUse": "Edit|Write|MultiEdit|NotebookEdit|Bash"}  # the main's work tools


def _ours(entry: dict) -> bool:
    mine = {fn() for fn in HOOKS.values()}
    return any(h.get("command") in mine for h in entry.get("hooks", []))


def permission_rules() -> list[str]:
    from app.mcp_server import TOOL_NAMES
    from app.repo_setup import CHANNEL, SERVER

    return [f"mcp__{SERVER}__{t}" for t in TOOL_NAMES] + [f"mcp__{CHANNEL}__reply"]


def skill_text() -> str:
    from app.repo_setup import DEPLOY, DEPLOY_SCRIPT, SECTION

    deploy = f"\n(Only in repos that have `{DEPLOY_SCRIPT}`.)" + DEPLOY.format(
        script=DEPLOY_SCRIPT, project=PROJECT_REF
    )
    body = SECTION.format(project=PROJECT_REF, deploy=deploy)
    body = re.sub(  # per-repo setup paragraph -> how the global install works
        r"\*\*Setup \(once per machine.*?\n\n",
        "**Setup.** Installed for every git repo directly under the projects folder (`agw global-setup`). "
        "At session start a hook names this repo's project (README Game Identity, else the README title "
        "or folder name) and asks you to start the Telegram pull loop.\n\n",
        body,
        flags=re.S,
    )
    front = (
        "---\n"
        f"name: {SKILL}\n"
        "description: Work with the local agent gateway and its Telegram control group. Use whenever a "
        "task in a repo under the projects folder ends (report it with submit_report), when the user "
        "mentions Telegram, a report, a deploy, game art or design images (generate_art, reviewed by the "
        'operator), requests arriving through get_messages or <channel source="agent-gateway-channel"> '
        "events, when a game or app needs a review, second opinion, competitor research or ideas "
        "(review_project runs the game-review skill), or when agent-gateway (mcp__agent-gateway__*) "
        "tools are missing after setup.\n"
        "---\n\n"
    )
    return front + body + "\n"


def _load(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def _claude(*args: str) -> subprocess.CompletedProcess | None:
    claude = shutil.which("claude")
    if claude is None:
        return None
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    return subprocess.run([claude, *args], capture_output=True, text=True, env=env)


def install(
    register_mcp: bool = True,
    gateway_url: str = "http://127.0.0.1:8000/mcp",
    token: str | None = None,
) -> list[str]:
    require_prod("global setup")
    done = []
    path = claude_home() / "settings.json"
    backup = path.with_name("settings.json.agw-backup")
    if path.exists() and not backup.exists():
        shutil.copy2(path, backup)
        done.append(f"backup of your settings: {backup}")
    data = _load(path)
    allow = data.setdefault("permissions", {}).setdefault("allow", [])
    allow += [r for r in permission_rules() if r not in allow]
    for event, cmd in HOOKS.items():  # the hint at start; a waiter check on every prompt
        hooks = data.setdefault("hooks", {}).setdefault(event, [])
        hooks[:] = [h for h in hooks if not _ours(h)]
        entry = {"hooks": [{"type": "command", "command": cmd(), "timeout": 10}]}
        hooks.append({"matcher": MATCHERS[event], **entry} if event in MATCHERS else entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    done.append(
        f"{path}: gateway tools allowed, SessionStart, UserPromptSubmit, StopFailure and PreToolUse "
        "(the main only routes) hooks added"
    )
    skill_file().parent.mkdir(parents=True, exist_ok=True)
    skill_file().write_text(skill_text())
    done.append(f"skill: {skill_file()}")
    if register_mcp:
        _claude("mcp", "remove", "agent-gateway", "--scope", "user")
        header = ["--header", f"Authorization: Bearer {token}"] if token else []
        r = _claude(
            "mcp",
            "add",
            "--transport",
            "http",
            "--scope",
            "user",
            "agent-gateway",
            gateway_url,
            *header,
        )
        done.append(
            "user MCP agent-gateway "
            + ("registered" if r and r.returncode == 0 else "NOT registered")
        )
        _claude("mcp", "remove", "agent-gateway-channel", "--scope", "user")
        r = _claude(
            "mcp", "add", "--scope", "user", "agent-gateway-channel", "--", str(_agw()), "channel"
        )
        ok = r is not None and r.returncode == 0
        why = (r.stderr if r else "claude CLI not found")[:200]
        done.append(
            "user MCP agent-gateway-channel " + ("registered" if ok else f"NOT registered: {why}")
        )
    return done


def remove(register_mcp: bool = True) -> list[str]:
    require_prod("global setup")
    done = []
    path = claude_home() / "settings.json"
    if path.exists():
        data = _load(path)
        rules = set(permission_rules())
        perms = data.get("permissions", {})
        if "allow" in perms:
            perms["allow"] = [r for r in perms["allow"] if r not in rules]
        hooks = data.get("hooks", {})
        for event in HOOKS:
            if event in hooks:
                hooks[event] = [h for h in hooks[event] if not _ours(h)]
                if not hooks[event]:
                    del hooks[event]
        if "hooks" in data and not data["hooks"]:
            del data["hooks"]
        path.write_text(json.dumps(data, indent=2) + "\n")
        done.append(f"{path}: gateway rules and hook removed")
    if skill_file().parent.exists():
        shutil.rmtree(skill_file().parent)
        done.append(f"skill removed: {skill_file().parent}")
    if register_mcp:
        _claude("mcp", "remove", "agent-gateway-channel", "--scope", "user")
        done.append("user MCP agent-gateway-channel removed (agent-gateway itself stays)")
    return done
