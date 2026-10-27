# Agent Gateway

**Run your coding agents from Telegram.** Agent Gateway is a local server that connects the AI coding
tools on your machine (Claude Code, Codex, Cursor, Antigravity, OpenCode) to one Telegram group. You
send work from your phone. The agent session that has the project open picks it up, does it in the
repo, and replies in the group. Questions, approvals, reviews and generated art all come back the same
way.

It also runs plain workflows without an agent (data, documents, media inspection) through a
config-driven workflow engine with a REST API.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)
![Status: alpha](https://img.shields.io/badge/status-alpha-orange.svg)

- [Demo](#demo)
- [Features](#features)
- [Supported agents](#supported-agents)
- [Quick start](#quick-start)
- [How it works](#how-it-works)
- [Documentation](#documentation)
- [Contributing](#contributing), [Security](#security), [License](#license)

## Demo

### 1. A Telegram request handled by an agent session

The operator sends work to a session from the Telegram group. The agent is idle and costs nothing
until the message arrives. Then the gateway wakes it. The exchange below is a shortened example;
session numbers and wording vary.

```text
Telegram (operator)      /sessions
Bot                      s16  ClaudeCode(App)-AgentGateway   local_agent_tool   ready
                         s49  Antigravity(App)-MarioClone    gst-002-mario-snes ready
                         s52  Cursor(App)-MarioClone         gst-002-mario-snes ready
Telegram (operator)      /send s49 Add a pause button to the HUD
Bot                      📬 m561 delivered to Antigravity(App)-MarioClone
   ... the waiter in that session wakes within about a second; the agent calls get_messages,
   edits the repo, runs the tests, then calls reply_message ...
Bot                      Antigravity(App)-MarioClone: Added a Pause button to the HUD …
                         (changed / validated / not done, at most 500 characters)
```

If the agent needs a decision, it asks with `request_human_input`. The question shows up in the group
with one button per option. For risky steps (release, deploy, push) the agent asks with
`secure=true`, and the reply must be the operator's passcode. The bot deletes that reply at once.

### 2. A workflow over REST, no agent and no API keys

This runs on a fresh install. Copy the token from `.env`, but don't paste it into shared logs.

```bash
export API_TOKEN=$(grep '^API_TOKEN=' .env | cut -d= -f2-)
```

```bash
curl -s -H "Authorization: Bearer $API_TOKEN" -F "file=@examples/sales.csv;type=text/csv" http://127.0.0.1:8000/api/v1/attachments
```

```bash
curl -s -H "Authorization: Bearer $API_TOKEN" -H 'content-type: application/json' -d '{"objective":"aggregate the sales by region","attachment_ids":["att_…"],"data":{"group_by":["region"],"metrics":{"amount":["sum","count"]}}}' http://127.0.0.1:8000/api/v1/tasks
```

```bash
curl -s -H "Authorization: Bearer $API_TOKEN" http://127.0.0.1:8000/api/v1/tasks/tsk_…/result
```

The task is routed to the data plugin (`data/aggregate`). Its output is validated, and the result
lists the artifacts: `aggregated.csv` (validation `passed`), `data_summary.md` and `result.json`.
`GET /api/v1/artifacts/<id>/content` returns the CSV:

```text
region,amount_sum,amount_count
EU,17,2
US,13,2
APAC,12,1
```

More recorded runs (parallel steps, retries, crash recovery, approvals) are in
[docs/DEMO_EVIDENCE.md](docs/DEMO_EVIDENCE.md).

## Features

- **Telegram control.** `/send` work to a session, `/open` a project in a new session, list
  `/sessions` and `/agents`, `/cancel` work, `/deploy`, switch MCP tools on or off with `/mcp`, and
  manage provider `/keys`. Only allowlisted users in the control group can do this.
- **Wake on work, no polling.** Idle sessions wait on a long-poll (`agw wait`) or on a hook. No model
  runs until a message is queued for them.
- **Answers come back to the asker.** Results of delegated tasks, art reviews, project reviews and
  answered questions are pushed to the session that asked, in that same tool.
- **Agent to agent.** `ask_agent` sends a request to another session. Every request and answer is
  copied to the group, and the operator can step in.
- **Questions and approvals.** Questions with buttons are re-sent every 30 minutes until answered.
  Passcode confirmation covers risky steps. `close_question` closes the Telegram copy when the
  question was answered in the session.
- **Reviewed art.** `generate_art` makes images (fal, OpenAI) and 3D models (Meshy). Each one is
  approved and reviewed in Telegram before the agent can use it. Paid calls run only when requested.
- **Project reviews.** `review_project` runs independent QA or product reviews (benchmark, parity,
  playtest, ideation) on a fresh, read-only agent.
- **Deploys.** `/deploy` runs a project's `deploy.sh` for UAT or PROD in a separate worktree and
  reports the result.
- **Workflow engine.** Agents, capabilities, providers, routes, workflows and policies are declared in
  YAML under [`config/`](config/). Runs are durable, survive restarts, retry with bounds, pass quality
  gates and check every tool call against policy.
- **Local first.** Everything listens on `127.0.0.1`. Secrets live in `.env` or are encrypted in the
  database, and they are redacted from logs.

## Supported agents

| Agent | Connected by `agw setup` | How Telegram work reaches it |
|---|---|---|
| Claude Code (app or CLI) | user MCP servers, `agent-gateway` skill, SessionStart and UserPromptSubmit hooks | the session runs `agw wait` in the background; it exits with instructions when work arrives |
| Codex (desktop) | `~/.codex/config.toml`, `AGENTS.md`, skills, a hook that records the open thread | the gateway pushes queued work into the open Codex thread |
| Cursor | `~/.cursor/mcp.json`, `~/.cursor/hooks.json` | the `stop` hook waits and returns the work as the next message |
| Antigravity | `~/.gemini/config/mcp_config.json`, `GEMINI.md` | a detached `agw wait --agent antigravity` wakes the conversation through `agentapi` |
| OpenCode, CLI agents | their MCP config and `AGENTS.md` | `agw session --agent <name>` starts a run only when there is work |

Every agent uses the same MCP tools:

- **Messages:** `get_messages`, `reply_message`
- **Reports:** `submit_report`, `report_deployment`
- **Operator questions:** `request_human_input`, `get_human_response`, `close_question`
- **Other agents:** `ask_agent`, `get_ask`, `list_agents`
- **Tasks:** `delegate_task`, `get_task_status`, `get_task_result`, `cancel_task`
- **Art:** `generate_art`
- **Reviews:** `review_project`, `get_review`
- **Capabilities:** `list_capabilities`, `get_capability_details`

Agents that should install or connect themselves can follow
[docs/AGENT_CONNECT.md](docs/AGENT_CONNECT.md).

## Quick start

Requirements:
- macOS or Linux, with git.
- [uv](https://docs.astral.sh/uv/). The installer adds it if missing, and uv brings Python 3.12.
- A Telegram bot, for remote control (optional).

```bash
git clone https://github.com/datnguyeniosdev/SMAgent.git agent-gateway
```

```bash
cd agent-gateway && scripts/install.sh --agents detected
```

The installer is idempotent. It installs the dependencies and creates `.env` with generated local
secrets. Then it connects Claude Code and every other agent it finds, and ends with `agw doctor`. To
include Telegram, export its values first:

```bash
TELEGRAM_BOT_TOKEN=... TELEGRAM_CONTROL_CHAT_ID=-100... TELEGRAM_ALLOWED_USER_IDS=123 scripts/install.sh
```

Then start the gateway and keep it running:

```bash
uv run agw dev
```

```bash
curl -s http://127.0.0.1:8000/health
```

Restart your agents so they load the new MCP servers and hooks. Then open a project folder (one with
`.git`, `AGENTS.md` or `CLAUDE.md` under the projects folder, `~/Working` by default) and send `/menu`
to the bot.

| Command | Purpose |
|---|---|
| `uv run agw setup [--agents LIST\|detected\|all] [--remove]` | local secrets, plus connecting Claude Code and other agents |
| `uv run agw dev` | start the gateway (and OpenCode when it is enabled) |
| `uv run agw stop` | stop it |
| `uv run agw doctor` | show what is configured and reachable |
| `uv run agw repo-setup PATH` | refresh the gateway block in one repo's `CLAUDE.md` |
| `agw wait` | in a repo: wait for that folder's Telegram work (agents run this themselves) |
| `agw session --agent opencode\|codex\|antigravity` | run a CLI agent only when work arrives |
| `uv run agw validate` | validate `config/` |

Telegram setup (bot, group, IDs), environments (prod, dev, uat side by side), deploys, providers and
the REST API are covered in the [operator guide](docs/OPERATOR_GUIDE.md).

## How it works

```text
 Telegram group ──► Bot ──► Orchestrator ──► Channel hub ──► agent sessions (wait / hooks / push)
      ▲                         │                 ▲                   │
      │                         ▼                 └── MCP /mcp ◄──────┘  get_messages, reply_message, ...
      └──── reports ◄── Workflow engine ──► tools · plugins · MCP servers · model providers
                                    (config/: agents, capabilities, routes, workflows, policies)
```

- **Gateway:** a FastAPI app (`app/main.py`) that serves REST, Telegram (polling or webhook) and an
  MCP server at `/mcp`.
- **Channel hub (`app/channel.py`):**
  - One pull session per project folder and tool, for example `ClaudeCode(App)-MarioClone`.
  - Sessions have leases, expire when idle and are reported when dropped.
  - Messages are queued per session; answers are routed back to the tool that asked.
- **Workflow engine:** state is kept in SQLite (`var/state.db`), so runs survive restarts.
  - Steps are planned by domain plugins (software, documents, data, media, Unity).
  - Every tool call is checked against `config/policies`.
- **Details:** [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and
  [docs/GENERIC_PLATFORM_ARCHITECTURE.md](docs/GENERIC_PLATFORM_ARCHITECTURE.md).

## Documentation

| Topic | Doc |
|---|---|
| Running it (Telegram, environments, deploys, providers, REST) | [OPERATOR_GUIDE.md](docs/OPERATOR_GUIDE.md) |
| Connecting an agent | [AGENT_CONNECT.md](docs/AGENT_CONNECT.md) |
| Architecture | [ARCHITECTURE.md](docs/ARCHITECTURE.md) · [GENERIC_PLATFORM_ARCHITECTURE.md](docs/GENERIC_PLATFORM_ARCHITECTURE.md) |
| Config-driven gateway | [CONFIG_DRIVEN_GATEWAY.md](docs/CONFIG_DRIVEN_GATEWAY.md) |
| Capabilities | [CAPABILITY_REGISTRY.md](docs/CAPABILITY_REGISTRY.md) · [CAPABILITY_MATRIX.md](docs/CAPABILITY_MATRIX.md) |
| Workflows | [WORKFLOWS.md](docs/WORKFLOWS.md) · [WORKFLOW_ENGINE.md](docs/WORKFLOW_ENGINE.md) · [WORKFLOW_RECOVERY.md](docs/WORKFLOW_RECOVERY.md) |
| Plugins | [PLUGIN_DEVELOPMENT.md](docs/PLUGIN_DEVELOPMENT.md) · [software](docs/SOFTWARE_ENGINEERING_PLUGIN.md) · [documents](docs/RESEARCH_DOCUMENTS_PLUGIN.md) · [data](docs/DATA_PROCESSING_PLUGIN.md) · [media](docs/MEDIA_PLUGIN.md) · [unity](docs/UNITY_PLUGIN.md) |
| Security model | [docs/SECURITY.md](docs/SECURITY.md) · [SECURITY_MODEL.md](docs/SECURITY_MODEL.md) · [WORKFLOW_SECURITY.md](docs/WORKFLOW_SECURITY.md) |
| Demo evidence | [DEMO_EVIDENCE.md](docs/DEMO_EVIDENCE.md) |
| Design history (phase plans and audits) | [docs/history/](docs/history/) |

## Development

```bash
uv sync
```

```bash
uv run pytest -q
```

```bash
uv run ruff check . && uv run ruff format --check .
```

The tests make no network or paid calls. Real integrations are opt-in: add `RUN_PAID=1` to allow paid
calls. Anything not configured reports `SKIPPED`, never a pass.

```bash
RUN_INTEGRATION=1 uv run pytest tests/integration -rs
```

## Limitations

- **Single user, single machine:** one allowlist and one process. Not built for multi-tenant hosting.
- **Agents run as you:** agent runtimes and `QA_SANDBOX=none` execute on the host. Point the gateway
  only at repositories you trust.
- **Alpha:** interfaces and config may change between versions. See the [changelog](CHANGELOG.md).
- More in [docs/SECURITY.md](docs/SECURITY.md#known-limitations).

## Contributing

Issues and pull requests are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) and the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Security

Please report vulnerabilities privately, as described in [SECURITY.md](SECURITY.md). Do not open
public issues for them.

## License

[MIT](LICENSE) © 2026 Darren Nguyen
