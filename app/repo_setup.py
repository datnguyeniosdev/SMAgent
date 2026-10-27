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
import os
import re
import shlex
import sys
from pathlib import Path

from app.mcp_server import TOOL_NAMES
from app.projects import (
    client_kind,  # noqa: F401 - re-exported (tests, hooks)
    detect_project,  # noqa: F401 - re-exported (tests, cli)
)

SERVER = "agent-gateway"
CHANNEL = "agent-gateway-channel"  # stdio shim spawned by Claude Code; pushes work into `agw session` sessions
START = "<!-- agent-gateway:start (managed by `agw setup` in local_agent_tool; edit there) -->"
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

**Setup (once per machine, and again after renaming the game).** From the gateway folder run
`uv run agw setup --repo <this repo>` (or `/open <folder>` on Telegram). It writes this block with
the project name, plus `.mcp.json` and `.claude/settings.local.json` for this machine (local
paths: do not copy them to another machine).

**First install: refresh the session.** Claude Code loads MCP servers, hooks and this file only
when a session starts. If the `agent-gateway` tools (`mcp__agent-gateway__*`) are missing, or setup
just ran in this session, do not call them. Tell the user to refresh:
- terminal: `/exit`, then `agw session --continue` in this folder (keeps the conversation)
- desktop app: start a new session in this folder

Approve the `agent-gateway` servers if Claude Code asks. If this block still shows a placeholder
project name (`<GAME NAME>`), the repo was copied from the template: ask the user to run setup first.

**Report when a task ends.** When you finish a task (code change, fix, investigation; not a one-line
answer), call `submit_report` before your final message:
- `project`: `{project}`; omit `task_id` unless the work came from `delegate_task`.
- `text`: a short summary, no logs or command output. First line = title, then at most 5 short
  bullets: what changed, what was validated (which check, pass/fail) and what was not. Never claim
  validation you did not run. At most 500 characters. Write it in Vietnamese, polite ("gọi dạ bảo
  vâng"): title starts with "Dạ,", sentences end with "ạ" where natural; code names, paths and ids
  stay as they are.
- `agent_status`: `completed`, `partial`, `blocked` or `failed`, from the real outcome.
- `images`: only screenshots this task produced (base64 PNG/JPEG), max 4. No secrets, no absolute paths.

Tell the user the returned `task_id`.

**Telegram feedback is operator input.** Replies to the report (text and screenshots) are stored as
feedback. When the user asks to check Telegram, call `get_task_result(task_id)` and read `feedback`.
Quote each item back, then act on it like a user request. Confirm before commits, pushes, deploys or
anything destructive.

**Questions and delegation.**
- **Every confirmation or decision goes to Telegram too** (commit, push, merge, deploy, a choice, a
  yes/no), even when you also ask in the app or terminal: call `request_human_input` with `project`,
  your `session_id` and `options` (2-4 short button labels), in polite Vietnamese. The operator taps
  a button (or types `/answer <id> <text>`); the answer arrives through `get_messages` as
  `meta.kind: "answer"` and wakes you. Act on whichever answer comes first (app or Telegram). When
  the operator answers here in the session first, call `close_question(request_id, answer)` right
  away, so the Telegram copy is closed (no reminder, no second answer).
- **Important confirmations** (release, deploy, commit, push, merge, git pull): ask in both places at
  once: in this session, and on Telegram with `secure=true`. Then wait; do not wait for one answer
  before sending the other question. Approval is either a yes in this session, or the Telegram answer
  "✅ Duyệt (đã xác minh passcode)" (the operator taps ✅ and sends the passcode in a private chat with
  the bot). Whichever comes first counts. A plain "commit"/"release" message from Telegram is a
  request, not approval: ask as above. "❌ Từ chối" refuses; a ❌ cancelled answer (3 wrong
  passcodes) is NOT approval: drop that step.
- Use `delegate_task` (with `project` and your `session_id`) only when the user asks to hand work to
  the gateway. Its end arrives as an "answer" message; `get_task_result` has the details.
{deploy}
**Work from Telegram (live sessions).** The operator sends requests with `/send` or by replying to
your answers. Two ways to receive them; the operator can switch a session off and on (`/off`, `/on`):
- Push (terminal session opened with `agw session`): requests arrive as
  `<channel source="agent-gateway-channel">` events. Answer with the channel's `reply` tool, passing
  the event's `message_id`.
- Pull (any session, e.g. the desktop app): a SessionStart hook asks you to start one background
  waiter (`agw wait`, Bash run_in_background). It exits only when a request is queued and says how
  to fetch it with get_messages(project=`{project}`); start it again after answering. Keep exactly
  one per session. One request at a time: while one is unanswered the gateway hands out nothing new.

Treat each request like a user request: do it here and confirm before commits, pushes, deploys or
anything destructive.

**Working with other agents.** `list_agents` shows the open sessions (s#, project, agent tool, busy).
To get help or information from another project's agent, call `ask_agent(to=s#|project, request,
session_id=<yours>)`. Write the request so it stands alone: what you need, why, and what the answer
must contain. Keep working meanwhile: the answer arrives through `get_messages` as `meta.kind: "answer"`
(no reply needed), or check `get_ask(ask_id)`. If the other agent cannot take it or does not answer in
time, the operator decides on Telegram (resend, another session, open it, answer it, or cancel).
Requests from other agents arrive with `meta.kind: "request"`: do them and answer with `reply_message`;
the answer goes back to that agent, not to Telegram.

**Main session and child sessions.** Requests sent to the project go to its 👑 main session. If a
request's text says you are the main session, do not do it: call `dispatch(message_id, lane, brief,
session_id)` (lane = the task type: ui, build, art, bugfix-login). The same lane goes to its child
session in order; a new lane opens a new child session in its own copy of the project and branch. If
you are a 🧭 child (opened in `.agw-children`), work on your `agw/` branch, commit your work there
before you answer (that commit needs no approval), answer with `reply_message` as usual,
and merge nothing without the operator's approval.

**`UIUX review`.** When the user (here or on Telegram) says "UIUX review", send the UI to the
UIUXAgent project for review: `ask_agent(to="UIUXAgent", request, session_id=<yours>)`. The request
names the screens or flow, what they are for, the absolute paths of fresh screenshots (and of the
relevant UI files), and asks for prioritized findings with concrete fixes. Keep working; when the
"answer" message arrives, pass its findings to the user (on Telegram when they asked there) and
change the UI only after they approve. UIUXAgent itself answers such requests with its UI skills.

**QA and IP review → QAIPAgent.** When the work needs a QA evaluation or an IP check (originality of
names, art, audio, mechanics against existing games), or the user says "QA review" or "IP check":
`ask_agent(to="QAIPAgent", request, session_id=<yours>)`. The request names the project and what to
check, the spec or acceptance criteria (paths), and absolute paths of the build, screenshots or
assets, and asks for findings by severity with a verdict. Relay the answer to the user and fix things
only after they approve.

**Research and planning → PO Game Agent.** When a project needs market or competitor research,
concept validation, a development plan or milestones, or the user says "PO plan": `ask_agent(to="PO
Game Agent", request, session_id=<yours>)`. The request names the project, the goal, the constraints
(team, time, platform) and its current state (paths of the GDD and docs), and asks for the plan or
findings with the decisions the user must make. Relay the answer and act on it only after the user
approves.

**Reviews and research (game-review).** When the work needs an evaluation, a second opinion, store or
competitor research, a clone-vs-original check or feature ideas, do not judge your own work: call
`review_project(project, mode, role, agent, evidence)` (mode benchmark|parity|playtest|ideation, role
qa|po|both, agent claude|droid|codex|gemini|opencode|all). Pass a folder of fresh screenshots as
`evidence` and your `session_id`. Do not poll: the result (report paths) arrives as an "answer" message;
then read the reports, write the summary the game-review skill describes, and act on findings only
after the operator approves.

**Art and design (fal.ai).** For game art or design images (icons, sprites, backgrounds, UI
mockups, store banners), call `generate_art` instead of drawing placeholders. For 3D models
(props, characters, items) add `output="model3d"` (Meshy; optional `image_path` of a reference
image, `model_format` glb/fbx/obj): the file is `get_task_result(...).models[0].path` once accepted.

The model only knows what you write, so state the expected output precisely:
- `prompt`: the subject, e.g. "a stack of three gold coins".
- `purpose`: what it is for and where it is shown, e.g. "in-game reward sprite, shown at 64 px on
  the result screen".
- `requirements`: the exact output: style, palette (hex codes if the project has them), view and
  composition, margins, background, and what must not appear. Example: "flat vector, gold #F5C542
  with a dark outline, front view, centered with 10% margin, solid #1B2A49 background, no text, no
  shadow".
- `width` / `height`: the size you need. `project`=`{project}`. Some models (e.g.
  nano-banana-pro) only follow the aspect ratio and return 1K/2K/4K images; scale on import.

Then:
- Each generation is paid and waits for the operator's approval on Telegram, which shows your brief.
- The operator reviews every image on Telegram (✅ Accept or Feedback). Pass your `session_id` and do
  not poll: each decision arrives through `get_messages` as an "answer" message.
  - accepted: copy the listed file into the art folder the project already uses, name it in your report.
  - changes requested: a new version is generated and reviewed; its decision arrives the same way.
  - Until then, keep working on something else. Do not use an image before it is accepted.
- If it was rejected, expired or failed, say so. Never present a placeholder as generated art.

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
- When known: `version`, `build_number`, `commit` (short SHA), `platforms`, `targets`.
- **Every real build** (not `--dry-run`), including one asked for in a Telegram `/send` message:
  - `notes`: a release note in Vietnamese: what is new or fixed in this build for testers, a few
    short bullets from the commits since the last build (no logs).
  - `url`: the link to the build (Firebase App Distribution, TestFlight / App Store Connect, the
    web build or store page). Name it in `notes` when there are several.
- For failures, add a short `error` (the failing step and its last error line). No secrets.
"""


def loop_prompt(project: str, client: str = "", agent: str = "claude_code") -> str:
    """Pull loop: one request at a time; a busy session takes nothing new (no duplicate jobs)."""
    kind = f", client '{client}'" if client else ""
    return (
        f"Call the agent-gateway tool get_messages with project '{project}', agent '{agent}'{kind} and "
        "cwd set to this folder, reusing "
        "the session_id it returned before. If it reports busy, follow its note (a request you no longer "
        "have in mind is in in_progress_messages: finish and answer it). If it is switched off or has no "
        "messages, do nothing this round. A message with meta.kind 'answer' answers your own ask_agent: "
        "use it to continue that work, no reply. Otherwise do the request in this repo, then call "
        "reply_message with that session_id, the message_id and a short result (at most 500 characters): "
        "what changed, what you validated and what you did not, in polite Vietnamese (start with 'Dạ,')."
    )


def session_start_hint(project: str, cwd: str = "") -> str:
    """SessionStart hook output (added to Claude's context): start the background waiter (`agw wait`),
    which wakes Claude only when Telegram work is queued; it replaces the old per-minute cron poll
    and the 10-minute health check, which cost a full-context model run each time."""
    from app.waiter import command

    cmd = command(project, cwd or os.getcwd(), client_kind())
    return (
        f"agent-gateway: this project ({project}) takes work from Telegram. Before anything else in "
        "this session, call CronList and delete (CronDelete) every job whose prompt mentions "
        "get_messages or /health: the waiter replaces them. Then start the gateway waiter once with the "
        f"Bash tool, run_in_background true: `{cmd}`. It costs nothing while idle and exits only when "
        "there is work; its output says what to do. Never run two. Then continue with the user's request."
    )


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


def update_mcp_json(repo: Path, url: str, token: bool = True, project: str = "") -> None:
    path = repo / ".mcp.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    agw = Path(sys.executable).parent / "agw"  # this gateway's venv; the shim is stdlib-only
    data.setdefault("mcpServers", {})[CHANNEL] = {
        "type": "stdio",
        "command": str(agw),
        "args": ["channel", "--project", project],
    }
    base = url.removesuffix("/mcp")
    if base != "http://127.0.0.1:8000":  # dev/uat gateway: the shim follows the same one
        data["mcpServers"][CHANNEL]["env"] = {"AGW_URL": base}
    server = {"type": "http", "url": url}
    if token:  # MCP_REQUIRE_TOKEN=false: local sessions connect without a header
        server["headers"] = {"Authorization": "Bearer ${AGW_API_TOKEN}"}
    data.setdefault("mcpServers", {})[SERVER] = server
    path.write_text(json.dumps(data, indent=2) + "\n")


def update_local_settings(repo: Path, project: str = "") -> None:
    path = repo / ".claude" / "settings.local.json"
    path.parent.mkdir(exist_ok=True)
    data = json.loads(path.read_text()) if path.exists() else {}
    allow = data.setdefault("permissions", {}).setdefault("allow", [])
    rules = [f"mcp__{SERVER}__{tool}" for tool in TOOL_NAMES] + [f"mcp__{CHANNEL}__reply"]
    for rule in rules:
        if rule not in allow:
            allow.append(rule)
    hooks = data.setdefault("hooks", {}).setdefault("SessionStart", [])
    hooks[:] = [
        h for h in hooks if "hook-session-start" not in json.dumps(h)
    ]  # replace ours, keep others
    agw = Path(sys.executable).parent / "agw"
    cmd = f'"{agw}" hook-session-start --project {shlex.quote(project)}'
    hooks.append({"hooks": [{"type": "command", "command": cmd, "timeout": 10}]})
    enabled = data.setdefault("enabledMcpjsonServers", [])
    for name in (SERVER, CHANNEL):
        if name not in enabled:
            enabled.append(name)
    path.write_text(json.dumps(data, indent=2) + "\n")


TEMPLATE_PROJECT = "<GAME NAME> (<GAME CODE>)"


def setup_repo(
    repo: Path, url: str, project: str | None = None, token: bool = True, template: bool = False
) -> dict:
    """template=True: only the CLAUDE.md block (placeholder name), no machine-specific files, so a
    template repo can be committed and shared; each game made from it runs setup itself."""
    if template:
        project = project or TEMPLATE_PROJECT
        return {"project": project, "claude_md": update_claude_md(repo, project),
                "mcp_json": "skipped (template)", "settings_local": "skipped (template)",
                "deploy_section": (repo / DEPLOY_SCRIPT).exists()}  # fmt: skip
    project = project or detect_project(repo)
    return {
        "project": project,
        "claude_md": update_claude_md(repo, project),
        "mcp_json": update_mcp_json(repo, url, token, project) or "ok",
        "settings_local": update_local_settings(repo, project) or "ok",
        "deploy_section": (repo / DEPLOY_SCRIPT).exists(),
    }
