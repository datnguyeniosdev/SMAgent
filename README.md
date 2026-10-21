# Agent Gateway: Generic Multi-Agent Platform

One gateway accepts different kinds of tasks (from Telegram, REST or scripts) and handles each one end to end:
1. Picks verified capabilities through a central registry.
2. Plans a validated workflow (domain plugins provide the knowledge).
3. Executes it with a durable workflow engine and a policy-checked tool subsystem.
4. Validates real outputs with quality gates.
5. Returns an evidence-based result envelope.

> **What actually works today:** [CAPABILITY_MATRIX.md](docs/CAPABILITY_MATRIX.md), with run evidence in [DEMO_EVIDENCE.md](docs/DEMO_EVIDENCE.md).
> - **Verified locally:** workflow engine, crash recovery, tool/MCP subsystem, software (deterministic), documents, data, media-inspect and Unity-inspect plugins, REST.
> - **Verified with a real coding runtime:** OpenCode 2.1.0 (`task_type: code_change`), with gateway-enforced permissions.
> - **Not working here:** Claude Code (stored login returns 401), model/image providers (no API keys), Telegram group/channel (placeholder chat IDs), Unity Editor tools (no Editor running).

## Config-driven orchestration (new)

Agents, capabilities, executors, model providers, MCP servers, routes, versioned workflows and policies are declared in [`config/`](config/) (YAML, schema-versioned, validated at startup).
- Adding an agent with existing capabilities, or a new workflow plus route, needs **no code change**.
- Telegram reports, and replies in the linked Discussion Group, become correlated feedback that runs a configured follow-up workflow, e.g. visual feedback: Gemini design analysis, a deterministic implementation prompt, then a handoff to Claude Code or back to the caller.
- The gateway is also an **MCP server** (`/mcp`) for clients like Claude Code.

Details: [CONFIG_DRIVEN_GATEWAY.md](docs/CONFIG_DRIVEN_GATEWAY.md). Audit: [CONFIG_UPGRADE_AUDIT.md](docs/CONFIG_UPGRADE_AUDIT.md).

```bash
uv run python -m app.gateway_config config
```

```bash
claude mcp add --transport http --scope project agent-gateway http://127.0.0.1:8000/mcp --header "Authorization: Bearer $API_TOKEN"
```

## Documentation

| Topic | Doc |
|---|---|
| Architecture | [GENERIC_PLATFORM_ARCHITECTURE.md](docs/GENERIC_PLATFORM_ARCHITECTURE.md) |
| Capability registry | [CAPABILITY_REGISTRY.md](docs/CAPABILITY_REGISTRY.md) · [CAPABILITY_MATRIX.md](docs/CAPABILITY_MATRIX.md) |
| Workflow engine / recovery | [WORKFLOW_ENGINE.md](docs/WORKFLOW_ENGINE.md) · [WORKFLOW_RECOVERY.md](docs/WORKFLOW_RECOVERY.md) |
| Security | [SECURITY.md](docs/SECURITY.md) · [SECURITY_MODEL.md](docs/SECURITY_MODEL.md) · [WORKFLOW_SECURITY.md](docs/WORKFLOW_SECURITY.md) |
| Plugins | [PLUGIN_DEVELOPMENT.md](docs/PLUGIN_DEVELOPMENT.md) · [software](docs/SOFTWARE_ENGINEERING_PLUGIN.md) · [documents](docs/RESEARCH_DOCUMENTS_PLUGIN.md) · [data](docs/DATA_PROCESSING_PLUGIN.md) · [media](docs/MEDIA_PLUGIN.md) · [unity](docs/UNITY_PLUGIN.md) |
| Config-driven gateway | [CONFIG_DRIVEN_GATEWAY.md](docs/CONFIG_DRIVEN_GATEWAY.md) · [CONFIG_UPGRADE_AUDIT.md](docs/CONFIG_UPGRADE_AUDIT.md) |
| Phase reports | [P2 audit](docs/PHASE_2_AUDIT.md) · [P2](docs/PHASE_2_IMPLEMENTATION.md) · [P3 audit](docs/PHASE_3_ARCHITECTURE_AUDIT.md) · [P3](docs/PHASE_3_IMPLEMENTATION.md) · [P4 audit](docs/PHASE_4_AUDIT.md) · [P4](docs/PHASE_4_IMPLEMENTATION.md) · [P5 audit](docs/PHASE_5_AUDIT.md) · [P5](docs/PHASE_5_IMPLEMENTATION.md) |

## Quick start (uv commands)

```bash
uv sync
```

```bash
uv run agw init-env
```

`init-env` creates `.env` if needed and fills only missing secrets (`API_TOKEN`, `OPENCODE_PASSWORD`).

```bash
uv run agw dev
```

`dev` starts the OpenCode server (when `OPENCODE_ENABLED=true`) and the gateway on `127.0.0.1:8000`.

```bash
uv run agw mcp-setup
```

`mcp-setup` registers the gateway's MCP server (`agent-gateway`) in Claude Code at user scope. Check it with `claude mcp list`.

| Command | Purpose |
|---|---|
| `uv run agw serve [--port 8000] [--reload]` | gateway only (REST, Telegram, `/mcp`) |
| `uv run agw opencode` | `opencode serve` with `OPENCODE_PASSWORD`/`OPENCODE_URL` from `.env` |
| `uv run agw dev` | OpenCode + gateway together |
| `uv run agw validate` | validate `config/` |
| `uv run agw doctor` | what is configured and reachable (gateway, OpenCode, Telegram, Claude MCP) |
| `uv run agw mcp-setup [--scope user\|project\|local]` | (re)register the MCP server in Claude Code |
| `uv run agw init-env` | generate missing local secrets |

## Setup

Requires [uv](https://docs.astral.sh/uv/) (`brew install uv`) and git. uv installs Python 3.12 from `.python-version`.

```bash
uv sync --locked
```

```bash
cp .env.example .env
```

Edit `.env` (blank values are treated as unset). Then start the gateway:

```bash
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

At startup the gateway:
- discovers configured MCP servers
- runs free health checks (`claude --version`, `GET /models`)
- recovers interrupted tasks from `var/state.db`
- starts Telegram long polling if `TELEGRAM_BOT_TOKEN` is set

## Using it

**REST** (all `/api/v1/*` endpoints need `Authorization: Bearer $API_TOKEN`):

```bash
curl -s -H "Authorization: Bearer $API_TOKEN" -F "file=@sales.csv;type=text/csv" http://127.0.0.1:8000/api/v1/attachments
```

```bash
curl -s -H "Authorization: Bearer $API_TOKEN" -H 'content-type: application/json' -d '{"objective":"aggregate the sales by region","attachment_ids":["att_…"],"data":{"group_by":["region"],"metrics":{"amount":["sum","count"]}}}' http://127.0.0.1:8000/api/v1/tasks
```

Endpoints:

| Endpoint | Purpose |
|---|---|
| `GET /health` | health check (no auth) |
| `POST /api/v1/tasks` | generic `TaskRequest`; `workflow` = explicit definition; `tool` = direct tool; legacy `description` + `workflow` enum still works |
| `GET /api/v1/tasks/{id}` | task + workflow steps |
| `/api/v1/tasks/{id}/result` · `/workflow` · `/events` · `/artifacts` · `/cancel` · `/answer` | result envelope, definition + step records, events, artifacts, cancel, answer a clarification |
| `GET /api/v1/approvals` · `POST /api/v1/approvals/{id}` | pending approvals; `{"approve": true}` |
| `/api/v1/config/validate` · `POST /api/v1/config/reload` · `/api/v1/agents/registry` | configuration diagnostics, validated reload, agents/capabilities with executor eligibility |
| `/api/v1/tasks/{id}/reports` · `POST /api/v1/reports/{id}/feedback` · `/api/v1/tasks/{id}/handoffs` · `/api/v1/human-input` | reports with screenshots, feedback, handoff records, human input |
| `POST /api/v1/maintenance/cleanup-artifacts` | retention cleanup (never touches active or referenced artifacts) |
| `/mcp` | gateway MCP server (Streamable HTTP, same bearer token) |
| `GET /api/v1/artifacts/{id}/content` | download an artifact |
| `/api/v1/tools` · `/capabilities` · `/plugins` · `POST /api/v1/plugins/{id}/activate\|deactivate` | registry views and plugin control |
| `/api/v1/agents` · `/sessions` · `/attachments` · `/messages` · `POST /api/v1/actions/{token}/execute` | Phase 1 endpoints, unchanged |

**Telegram** (control group, allowlisted users):
- `/task <text>` with optional attachments, then pick a button: 🤖 Auto, a domain, or a legacy workflow.
- Reply to a report in the linked Discussion Group (or `/feedback <task_id|report_id> <text>`) to give feedback; it starts the configured follow-up workflow.
- `/tool <name> <json>` · `/tools` · `/status` · `/report <id>` · `/steps <id>` · `/stop <id>` · `/retry <id>` · `/answer <id> <text>` · `/approvals` · `/plugins` · `/capabilities` · `/agents` · `/agents_status` · `/sessions`

Reports, tool results, approvals and failures go to the report channel or control group.

### Telegram setup

1. Create the bot with @BotFather and set `TELEGRAM_BOT_TOKEN`.
2. Add it to a private **control group**. Disable privacy mode (`/setprivacy`) if you want albums.
3. Add it as **admin with "Post messages"** to the **report channel**. Linking the channel to the group (Discussion) is optional and grants nothing.
4. Send a message in each, then run this to get the IDs:

   ```bash
   uv run python -m scripts.smoke_telegram --discover
   ```

5. Set `TELEGRAM_CONTROL_CHAT_ID`, `TELEGRAM_REPORT_CHANNEL_ID` and `TELEGRAM_ALLOWED_USER_IDS`.
6. Verify with a real tool task whose reports go to Telegram:

   ```bash
   uv run python -m scripts.smoke_telegram
   ```

## Providers, runtimes, MCP

| What | Enable with |
|---|---|
| OpenCode runtime (**verified**) | `opencode serve` with `OPENCODE_SERVER_PASSWORD`, then `OPENCODE_ENABLED=true`, `OPENCODE_PASSWORD`, optional `OPENCODE_MODEL`, `OPENCODE_SHELL_ALLOWLIST` |
| Claude Code runtime | `CLAUDE_CODE_ENABLED=true` and a working `claude` login. Run the gateway outside Claude Code (nested sessions are refused) |
| OpenAI / DeepSeek | `OPENAI_API_KEY` + `OPENAI_MODEL` / `DEEPSEEK_API_KEY` |
| Image generation | `OPENAI_API_KEY` + `OPENAI_IMAGE_MODEL` |
| MCP servers | `MCP_SERVERS` JSON: only `allowed_tools` are registered; non-read-only tools need `mcp:execute` in `TOOL_PERMISSIONS` plus approval |
| Plugins | `PLUGINS`, `PLUGIN_PERMISSIONS`, `TOOL_PERMISSIONS`, `PLUGIN_CONFIG` |
| QA | `QA_COMMANDS` (JSON name → command, no shell), `QA_SANDBOX=docker` for isolation |

## Tests

Unit, regression, real-local and recovery tests (no network, no paid calls):

```bash
uv run pytest
```

```bash
uv run ruff check .
```

```bash
uv run ruff format --check .
```

Opt-in real integrations (free checks; add `RUN_PAID=1` for paid calls). Missing configuration reports `SKIPPED` / `NOT CONFIGURED`, never a pass:

```bash
RUN_INTEGRATION=1 uv run pytest tests/integration -rs
```

Re-run every demonstration for real and regenerate the evidence:

```bash
uv run python -m scripts.demo --unity-project /path/to/UnityProject
```

```bash
uv run python -m scripts.render_evidence
```

## Limitations

- **Not sandboxed:** the Claude Code runtime and `QA_SANDBOX=none` run on the host.
- **Process-local:** single process, single tenant. Quick Action buttons don't survive restarts (approvals are re-issued).
- **Rule-based plugins:** planning is deterministic (no model-assisted planning yet), and document extraction is rule-based; there's no web research.
- **Unverified integrations:** agent implementation, review, summarization and image generation are implemented but unverified here (credentials and runtime are blocked).

See [SECURITY.md](docs/SECURITY.md#known-limitations).
