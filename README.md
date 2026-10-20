# Agent Gateway

A Telegram-controlled multi-agent gateway (MVP). Send a task with reference images or files to a private control group, pick a workflow, approve risky steps, and get reports in a channel.
Behind it, an orchestrator routes work by capability to Claude Code (coding runtime), OpenAI, or DeepSeek (model APIs), plus an image provider.
Code changes happen in isolated git worktrees, then get QA'd and independently reviewed. Nothing is ever merged automatically.

> Read [docs/CAPABILITY_MATRIX.md](docs/CAPABILITY_MATRIX.md) first: it says what was really verified and what is mocked.
> In short: worktrees, QA, REST and asset validation are verified locally. Telegram and the HTTP providers are tested with mocks only.
> Claude Code is partially verified: the CLI protocol works, but this machine's login returned 401.

Docs: [Architecture](docs/ARCHITECTURE.md) · [Workflows](docs/WORKFLOWS.md) · [Security](docs/SECURITY.md) · [Plan & status](docs/IMPLEMENTATION_PLAN.md)

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (`brew install uv`). uv installs Python 3.12 from `.python-version`.
- git (worktrees). Optional: Claude Code CLI, Docker (QA sandbox), Unity + Unity-MCP.

## Setup

```bash
uv sync
```

```bash
cp .env.example .env
```

Edit `.env` (blank values are treated as unset), then run:

```bash
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Other commands:

```bash
uv run pytest
```

```bash
uv run ruff check .
```

```bash
uv run ruff format .
```

```bash
uv tree
```

CI runs `uv sync --locked` (see `.github/workflows/ci.yml`). For a runtime-only install, use `uv sync --locked --no-dev`.

## Telegram setup

1. **Create the bot** with @BotFather (`/newbot`) and put the token in `TELEGRAM_BOT_TOKEN`.
2. **Privacy mode**: by default, bots in groups only see commands. A `/task` caption on a single photo or file works. In an album, only the captioned item is a command.
   To use albums, either disable privacy (@BotFather → `/setprivacy` → Disable) or make the bot a group admin, then re-add it to the group.
3. **Control group**: create a private group, add the bot, and set `TELEGRAM_CONTROL_CHAT_ID` (negative, usually `-100…`).
4. **Report channel**: create a channel and add the bot as an admin with only *Post messages*. Set `TELEGRAM_REPORT_CHANNEL_ID`.
   Optionally link the group to the channel (Channel → Edit → Discussion). Linking is a convenience only; it grants no permissions.
5. **IDs**: get user and chat IDs from a helper such as @userinfobot, or from `getUpdates` *before* the gateway starts polling.
   `getUpdates` fails while a webhook is set, and the gateway won't poll in that case either. Check with `https://api.telegram.org/bot<token>/getWebhookInfo`.
6. Set `TELEGRAM_ALLOWED_USER_IDS` (comma-separated). Only these users, and only in the control group, can issue commands. Button clicks also require an allowlisted user.

At startup the gateway logs whether it can see both chats and what its member status is in each.

Commands: `/start` `/help` `/task <description>` `/status` `/report <task_id>` `/stop <task_id|session_id>` `/retry <task_id>` `/agents` `/agents_status` `/sessions`.
After `/task`, an inline keyboard offers the workflows. Approvals appear in the control group with Approve/Reject/Stop buttons.
Final reports go to the channel with View report / Retry / Review diff. A report longer than 4096 characters is summarized, and the full text is attached as a file.

## Providers and runtimes

| Connector | Enable with | Notes |
|---|---|---|
| `claude_code` | `CLAUDE_CODE_ENABLED=true`, logged-in `claude` CLI | Implementer/reviewer. Run the gateway **outside** a Claude Code session (the CLI refuses to nest). Smoke test: `uv run python -m scripts.smoke_claude_code` (makes one small paid call). |
| `openai` | `OPENAI_API_KEY` + `OPENAI_MODEL` | Text, vision, structured output, tool-call passthrough |
| `openai_images` | `OPENAI_API_KEY` + `OPENAI_IMAGE_MODEL` | Asset generation (`/v1/images/generations`) |
| `deepseek` | `DEEPSEEK_API_KEY` | Text, JSON mode, tools. Image input is off by default (`DEEPSEEK_IMAGE_INPUT`) |
| Gemini, OpenCode, Claude Desktop | — | Not available; see the capability matrix |

Role preferences (`IMPLEMENTER_PREFERENCE`, `DESIGNER_PREFERENCE`, `REVIEWER_PREFERENCE`) order the candidates.
The router only picks a connector that has every required capability. Otherwise the task fails with an explicit `unsupported` error and no silent downgrade.

**Unity**: point `CLAUDE_CODE_MCP_CONFIG` at an MCP config for [Unity-MCP](https://github.com/IvanMurzak/Unity-MCP), and add a Unity batch-mode build to `QA_COMMANDS`.
Without these, reports say Unity build and visual validation were not performed.

## REST API

All `/api/v1/*` endpoints need `Authorization: Bearer $API_TOKEN`. They're disabled (503) when the token is unset.

`GET /health` · `GET /api/v1/agents` · `GET /api/v1/sessions[/{id}]` · `POST /api/v1/attachments` (multipart `file`) · `POST /api/v1/messages` · `POST /api/v1/tasks` · `GET /api/v1/tasks/{id}` · `GET /api/v1/tasks/{id}/artifacts` · `POST /api/v1/actions/{token}/execute`

Example:

```bash
curl -s -H "Authorization: Bearer $API_TOKEN" -H 'content-type: application/json' -d '{"description":"Summarise the attached spec","workflow":"ask"}' http://127.0.0.1:8000/api/v1/tasks
```

## Attachments, artifacts, workspaces

- Accepted: PNG, JPEG, GIF, WebP, PDF, and UTF-8 text (txt/md/csv/json/cs/py), checked by content signature. Default limit is 20 MB (the Bot API download limit).
  Audio, video, and archives are rejected with an explicit message.
- Files live under `DATA_DIR` (`var/` by default): `media/<task>/`, `artifacts/<task>/`, `worktrees/<task>/`. None of it is committed, and cleanup is manual.
- Each writing task gets `git worktree add -b agw/<task_id>` from `WORKSPACE_ROOT` HEAD.

## Limitations

- In-memory task/session/approval state: **a restart loses it**. Worktrees and artifacts survive.
- `QA_SANDBOX=none` is not a sandbox. Claude Code runs on the host under its own permission system. See [SECURITY.md](docs/SECURITY.md).
- No streaming progress from agents yet. Reports cover start, approvals, QA, findings, and final state.

## Troubleshooting

- *Bot doesn't respond*: check the logs for `unauthorized message` (wrong user/chat ID), and check that privacy mode allows what you send.
  `A webhook is configured` means delete the webhook (`deleteWebhook`) if you want polling.
- *`claude_code error … authenticate`*: run `claude` and `/login`, or set `ANTHROPIC_API_KEY`.
- *`Claude Code cannot be launched inside another Claude Code session`*: the gateway already strips `CLAUDECODE`. Run the gateway from a normal terminal.
- *Task `unsupported`*: no enabled connector has the capability (for example, image input with only DeepSeek configured).

## Cost

Each agent call records tokens where the provider reports them. Claude Code also reports `total_cost_usd`, which its docs call a client-side estimate.
No other cost figures are computed. Use `CLAUDE_CODE_MAX_BUDGET_USD` to cap a Claude Code run. `parallel` doubles agent cost.
Unit tests never call paid APIs.
