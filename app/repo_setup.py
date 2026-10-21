"""`agw repo-setup <repo>`: connect a repo's Claude Code sessions to the gateway, the same way everywhere.

Writes (idempotent; re-running refreshes the managed text):
  CLAUDE.md                    managed block between START/END markers (created if missing,
                               importing AGENTS.md when the repo has one)
  .mcp.json                    `agent-gateway` HTTP server; with MCP_REQUIRE_TOKEN (default) the token
                               comes from $AGW_API_TOKEN (no secret in git), otherwise no header
  .claude/settings.local.json  pre-approves the gateway tools and enables the .mcp.json server
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from app.mcp_server import TOOL_NAMES

SERVER = "agent-gateway"
START = "<!-- agent-gateway:start (managed by `agw repo-setup` in local_agent_tool; edit there) -->"
END = "<!-- agent-gateway:end -->"
# Hand-written sections from before the standard; replaced by the managed block.
LEGACY_HEADINGS = (
    "## Deploy notification (agent-gateway MCP)",
    "## Task report and Telegram control (agent-gateway MCP)",
)
DEPLOY_SCRIPT = "4_delivery/deploy/deploy.sh"

SECTION = """## Agent gateway: reports and Telegram control

This repo is connected to the local agent gateway (MCP server `agent-gateway`, see `.mcp.json`).
Project name for every gateway call: `{project}`.

**Report when a task ends.** When you finish a task (code change, fix, investigation; not a one-line
answer), call `submit_report` before your final message:
- `project`: `{project}`; omit `task_id` unless the work came from `delegate_task`.
- `text`: first line = short title; then what changed (files/areas), what was validated (the exact
  commands run and their result) and what was not. Never claim validation you did not run.
- `agent_status`: `completed`, `partial`, `blocked` or `failed`, from the real outcome.
- `images`: only screenshots this task produced (base64 PNG/JPEG), max 4. No secrets, no absolute paths.

Tell the user the returned `task_id`.

**Telegram feedback is operator input.** Replies to the report (text and screenshots) are stored as
feedback. When the user asks to check Telegram, call `get_task_result(task_id)` and read `feedback`.
Quote each item back, then act on it like a user request. Confirm before commits, pushes, deploys or
anything destructive.

**Questions and delegation.**
- To ask the operator mid-task, call `request_human_input` with `project`, then poll
  `get_human_response`. The operator answers with `/answer <id> <text>`.
- Use `delegate_task` (with `project`) only when the user asks to hand work to the gateway. Follow it
  with `get_task_status` / `get_task_result`.
{deploy}
If a gateway call fails or a message is not delivered, tell the user Telegram was not notified and why."""

DEPLOY = """
**Deploy notification.** After every run of `{script}` (any environment, including `--dry-run`),
once the command has finished, call `report_deployment`:
- `project`: `{project}`; `environment`: the deploy target (the first argument of the script).
- `status`, from the real outcome only:
  - exit code 0 -> `succeeded` (`dry_run` when `--dry-run` was used)
  - non-zero -> `failed`
  - `partial` if only some platforms shipped

  Never report a success you did not observe.
- When known: `version`, `build_number`, `commit` (short SHA), `platforms`, `targets`, `url`, `notes`.
  For failures, add a short `error` (the failing step and its last error line). No secrets.
"""


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
    if title := re.search(r"^# (.+)$", text, re.M):
        return title.group(1).strip()
    return repo.resolve().name


def section(repo: Path, project: str) -> str:
    deploy = (
        DEPLOY.format(script=DEPLOY_SCRIPT, project=project)
        if (repo / DEPLOY_SCRIPT).exists()
        else ""
    )
    return f"{START}\n{SECTION.format(project=project, deploy=deploy)}\n{END}\n"


def _strip_legacy(text: str) -> str:
    for heading in LEGACY_HEADINGS:
        if heading in text:  # drop the heading and its body, up to the next "## " heading or EOF
            text = re.sub(re.escape(heading) + r"\n.*?(?=^## |\Z)", "", text, flags=re.S | re.M)
    return text


def update_claude_md(repo: Path, project: str) -> str:
    path = repo / "CLAUDE.md"
    block = section(repo, project)
    if path.exists():
        text = path.read_text()
        if START.split(" (")[0] in text:
            text = re.sub(
                re.escape(START.split(" (")[0]) + r".*?" + re.escape(END) + r"\n?",
                lambda _: block,
                text,
                flags=re.S,
            )
            path.write_text(text)
            return "updated"
        text = _strip_legacy(text).rstrip() + "\n\n" + block
        path.write_text(text)
        return "added"
    agents = next((p for p in ("AGENTS.md", ".claude/AGENTS.md") if (repo / p).exists()), None)
    head = "# CLAUDE.md\n\n"
    if agents:
        head += f"All agent rules for this repo live in {agents}. Read it fully before working.\n\n@{agents}\n\n"
    path.write_text(head + block)
    return "created"


def update_mcp_json(repo: Path, url: str, token: bool = True) -> None:
    path = repo / ".mcp.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    server = {"type": "http", "url": url}
    if token:  # MCP_REQUIRE_TOKEN=false: local sessions connect without a header
        server["headers"] = {"Authorization": "Bearer ${AGW_API_TOKEN}"}
    data.setdefault("mcpServers", {})[SERVER] = server
    path.write_text(json.dumps(data, indent=2) + "\n")


def update_local_settings(repo: Path) -> None:
    path = repo / ".claude" / "settings.local.json"
    path.parent.mkdir(exist_ok=True)
    data = json.loads(path.read_text()) if path.exists() else {}
    allow = data.setdefault("permissions", {}).setdefault("allow", [])
    for tool in TOOL_NAMES:
        if (rule := f"mcp__{SERVER}__{tool}") not in allow:
            allow.append(rule)
    enabled = data.setdefault("enabledMcpjsonServers", [])
    if SERVER not in enabled:
        enabled.append(SERVER)
    path.write_text(json.dumps(data, indent=2) + "\n")


def setup_repo(repo: Path, url: str, project: str | None = None, token: bool = True) -> dict:
    project = project or detect_project(repo)
    return {
        "project": project,
        "claude_md": update_claude_md(repo, project),
        "mcp_json": update_mcp_json(repo, url, token) or "ok",
        "settings_local": update_local_settings(repo) or "ok",
        "deploy_section": (repo / DEPLOY_SCRIPT).exists(),
    }
