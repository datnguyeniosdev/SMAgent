# Connecting an agent to the gateway (runbook for agents)

You are a coding agent (Claude Code, OpenCode, Codex, Antigravity, Cursor) asked to install the
agent gateway on this machine or connect yourself to it. Follow the steps in order and stop at the
first one that already holds. Everything runs locally; the gateway listens on `127.0.0.1` only.

## Rules

- Never print, log or paste secrets: `TELEGRAM_BOT_TOKEN`, `API_TOKEN`, `OPENCODE_PASSWORD`, `FAL_KEY`
  and other `*_KEY` values. Check that a value is set (`grep -c '^NAME=.' .env`), never show it.
- Telegram values come from the operator. Ask for them; do not invent placeholder ids.
- The install changes this machine's agent configs (Claude Code user settings, other agents' MCP
  files; each file is backed up once as `*.agw-backup`). Do it only when the operator asked for the
  install or connection.
- Do not start paid providers (fal art, OpenAI images) unless the operator asks for that work.

## 1. Already connected?

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/health
```

`200` and your tool list has the `agent-gateway` tools (Claude Code: `mcp__agent-gateway__*`) →
go to step 5. `200` without the tools → step 4. No answer → step 2.

## 2. Install

The gateway is a folder with `pyproject.toml` and `scripts/install.sh` (usually
`~/Working/local_agent_tool`; prod may run from `~/Working/.agw-prod`, see the README). Ask the
operator where it is if neither exists. Then, in that folder:

```bash
scripts/install.sh
```

What it does, idempotently:
- checks git and uv (`brew install uv` if missing), installs the Python environment
- creates `.env` with generated local secrets (`MCP_REQUIRE_TOKEN=false`: local agents connect
  without a token)
- installs the Claude Code global setup: user MCP `agent-gateway` + `agent-gateway-channel`, the
  `agent-gateway` skill, a SessionStart hook and tool permissions, for every git repo directly under
  the projects folder (default: the gateway folder's parent)
- runs `agw doctor`

Options: `--agents opencode,codex,antigravity,cursor|detected|all` (also connect those agents),
`--no-global` (only `.env`), `--env dev|uat` (a test environment beside prod, see the README),
`--start` (start the gateway at the end).

**Telegram** (optional; without it reports go to the gateway log). The operator creates a bot with
@BotFather, adds it to their group as administrator, and gives you the group id and their user id.
Either they put the values in `.env` themselves, or you export them for one run:

```bash
TELEGRAM_BOT_TOKEN=... TELEGRAM_CONTROL_CHAT_ID=-100... TELEGRAM_ALLOWED_USER_IDS=123 scripts/install.sh
```

Values already in `.env` are never overwritten. Reports go to the control group unless
`TELEGRAM_REPORT_CHANNEL_ID` says otherwise. One bot serves one gateway: two gateways polling the
same bot steal each other's messages.

## 3. Start the gateway

In a terminal that stays open (or your background-process tool):

```bash
uv run agw dev
```

Wait until `curl http://127.0.0.1:8000/health` returns `200`. If port 8000 is taken, `agw dev` says
a gateway is already running and starts nothing.

## 4. Connect yourself

| You are | Connect with | Then |
|---|---|---|
| Claude Code | included in step 2 (`uv run agw setup` again repairs it) | `/exit`, then `claude --continue` in the same folder |
| OpenCode | `uv run agw setup --agents opencode` | restart OpenCode |
| Codex | `uv run agw setup --agents codex` | restart Codex |
| Antigravity (Gemini CLI) | `uv run agw setup --agents antigravity` | restart Antigravity |
| Cursor | `uv run agw setup --agents cursor` | paste `uv run agw setup --agents cursor --print-rules` into Cursor > Settings > Rules; restart Cursor |

Tools load when a session starts, so a session that ran the setup does not see the tools until it is
restarted. Tell the operator that, with the exact command, instead of continuing without them.

Per-repo instead of global (one repo only): `uv run agw setup --repo /path/to/repo`.

## 5. Verify

- Call `list_capabilities`: it answers with the gateway's capabilities.
- Call `list_agents`: it lists the open sessions (yours appears after step 6).
- `uv run agw doctor`: every line that matters to you starts with `OK`.

## 6. Work through the gateway

Project name: the README "Game Identity" line as `Name (CODE)`, else the README title, else the
folder name. Claude Code gets it from the session-start hook. Use the same name in every call.

- **Take work from Telegram:** call `get_messages(project, agent="<your tool>", session_id=<the id it
  returned before>)`. Do each request, then `reply_message(session_id, message_id, text)`. Messages
  with `meta.kind == "answer"` answer your own `ask_agent`: use them, no reply. Claude Code polls
  every minute with a recurring job (the hook asks for it); other agents call it when the user says
  to check Telegram.
- **Finish a task:** `submit_report(project, text, agent_status)`: a title line plus at most 5 short
  bullets (changed / validated / not done), no logs.
- **Ask another agent:** `ask_agent(to=s#|project, request, session_id)`, then `get_ask`. If nobody
  answers, the operator gets buttons on Telegram.
- **Ask the operator:** `request_human_input(question, project)`, then `get_human_response`.
- **Game art:** `generate_art(prompt, purpose, requirements, width, height, project)`. The operator
  approves it first and reviews each image; use images only after `review.state == "accepted"`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| no `agent-gateway` tools after setup | restart the session (step 4) |
| `401` / "token required" from `/mcp` | `.env` has `MCP_REQUIRE_TOKEN=true`: set it to `false` and restart the gateway, or export `AGW_API_TOKEN` |
| Telegram silent, log shows a 409 conflict | another gateway uses the same bot; stop it or give it its own bot |
| bot ignores replies in the group | make the bot an administrator; post as yourself, not as anonymous admin |
| `doctor` shows `-- Telegram` | the three `TELEGRAM_*` values are missing in `.env` |
| `get_messages` says busy | finish and reply to the message in progress first |
| `get_messages` says switched off | the operator ran `/off`; wait for `/on` |
