# Operator guide

Reference for running the gateway day to day: environments, releases, Telegram, providers, deploys and the REST API. Start with the [README](../README.md) for install and the demo.

## Agents and Telegram

**Reviews and research.** `review_project` runs the game-review skill's `run.sh` in the project's folder (a fresh read-only agent per role/agent, reports in `4_delivery/uat/reviews`), without the gateway's secrets, at most one run per project at a time; `get_review` follows it, and Telegram gets 🔎 at the start and ✅/❌ with the report names at the end. Switch it with `/mcp` like any tool.

**Operator controls.** Approving, running an action and answering a question are the operator's: over REST they need `OPERATOR_TOKEN` (unset = Telegram only), never `API_TOKEN`, which agents hold. `/off` on a window started with `agw session` switches its push and pull sessions together. `/cancel` of work in progress tells the session to stop. New work goes to an idle session of the project before a busy one. A reply to an agent's report reaches that agent (its session, else the project's), and the answer to an agent's question is pushed to the session that asked. `/deploy` shows 🛑 Stop for a running deploy; `/reviews` lists review runs with 🛑 Stop. A deploy left running across a restart still blocks a second one; stopping the gateway reports running deploys and reviews as interrupted. The gateway also logs to `var/agw.log` (5 × 5 MB).

**What Telegram shows.** Every request between agents (🤝 with ✍️ Answer myself / ❌ Cancel) and its answer (💡); `/asks` lists the open ones. Sessions are announced when they connect (🔌) and leave (⚪), and a queued `/send` says when it finally reaches a session (📬). An agent's question has one button per option, and replying to it answers it. A question or escalation Telegram refuses is reported back to the agent, or retried, instead of being lost.


**Questions asked in an agent's own chat.** Codex (its `Stop` hook) and Cursor (`afterAgentResponse`) send each final message to the gateway. When it ends on a question or a request to confirm, the question goes to Telegram too: ✅ Đồng ý / ❌ Không, or a passcode for commit, push, merge, release or deploy. The answer wakes that session. If the next turn ends first, the question was answered in the chat, so its Telegram copy closes. Questions the agent already asked with `request_human_input` are not sent twice. Antigravity has no such hook yet.

**Main session and child sessions.** Requests sent to a project (`/send p#`, `/send <project>`, `ask_agent` to a project) go to the project's 👑 main session: its first session, or the one chosen with `/main s#`; it stays the main across gateway restarts. The main only routes, with `dispatch`: work on the same lane (task type: ui, build, art…) goes to that lane's 🧭 child session in order; a new lane opens a new child session with the main's agent tool in its own copy of the project, `<repo>/.agw-children/<lane>-<id>` on a branch `agw/<lane>-<id>`, so lanes run in parallel. The copy is an APFS copy-on-write clone (almost no disk until files change; build caches such as an engine's import folder come along), reset to the last commit; elsewhere a git worktree in the same place. `.agw-children` ignores itself, so the repo's `git status` stays clean. Children commit on their branch and answer the sender directly; each answer fetches the branch into the project repo for review, and it is merged only with your approval. A child idle for 30 minutes is closed; its copy stays and is reopened when its lane gets work again. `/list` shows each project's lanes with status (working, idle, closed), current or last task (m#) and branch. When the main is stuck (rate limit, API error, routing work untaken for 10 minutes), a temporary router session opens in `<repo>/.agw-children/router` and routes with the same lanes; when the main works again it is told what was routed, takes over, and the router is closed. When the main is offline, the group is asked: 👑 make an open session the main, 🆕 open a new main, or ⏳ wait. Work for a named session (`/send s#`, a reply to its report) stays in that session in order. Answers to a session's own questions (Telegram buttons, `ask_agent` answers) reach it even while it is busy. Work a free session leaves untaken for 10 minutes is shown as stuck. Nothing waits forever for a session that never came: work held for a child that has not connected within 10 minutes is shown as stuck (move or cancel), a temporary router that has not connected within 10 minutes (or is stuck itself) is given up and you are asked what to do, and asks between two busy agents go to you at their deadline. A terminal session in the same folder as a desktop one is a separate session. Design: [DISPATCH_DESIGN.md](DISPATCH_DESIGN.md).

**Stuck work.** When an agent stops on a rate limit, usage limit or API error (Claude Code's `StopFailure` hook, Cursor's `stop` with an error), or nobody answers within the 60-minute lease, its unanswered and queued Telegram work is held as *stalled* instead of waiting silently. The group gets ⚠️ with the request and buttons: ↪️ another open session of the project, ⏳ wait, ❌ cancel. The stalled session takes no new work. When its agent runs again (a prompt in that session, or `get_messages`), the gateway asks once more: ✅ go on there, ↪️ move, or ❌ cancel. Codex and Antigravity have no failure hook yet, so for them only the lease applies.

## Environments (prod, dev, uat)

Each environment runs beside the others from this folder, with its own settings file, port and data:

| Env | Settings | Port | Data |
|---|---|---|---|
| prod (default) | `.env` | 8000 | `var/` |
| dev | `.env.dev` | 8001 | `var/dev/` |
| uat | `.env.uat` | 8002 | `var/uat/` |

```bash
uv run agw --env dev setup
```

```bash
uv run agw --env dev dev
```

- Agent configs (Claude Code, OpenCode, Codex, Antigravity, Cursor) always point at prod. In dev/uat, `setup --agents`, the global setup and the matching Telegram `/setup` buttons are refused.
- `uv run agw --env dev setup --repo PATH` connects one test repo to dev (its `.mcp.json` only).
- Telegram is off in dev/uat until their file has their own bot (and test group). Prod's bot token is refused there, because two gateways polling one bot would steal prod's messages.
- Paid providers (fal, ...) are off in dev/uat until you add their keys to that file.
- `AGW_ENV=dev` works too, in place of `--env dev`.

**Prod runs from its own copy.** `../.agw-prod` is a git worktree on `main`, with prod's `.env` and `var/`. The global Claude hook and channel run from its `.venv` too. Work happens here on `develop` (`--env dev`); prod only changes when you release:

```bash
git -C ../.agw-prod merge --ff-only develop
```

```bash
cd ../.agw-prod && uv sync
```

Then restart prod there (Ctrl-C, `uv run agw dev`). To take prod down on purpose, run `uv run agw stop` there; start it again with `uv run agw dev` in a terminal. Agent sessions never start the gateway: their waiter only waits for work and keeps waiting while the gateway is down.

```bash
uv run agw dev
```

`dev` starts the OpenCode server (when `OPENCODE_ENABLED=true`) and the gateway on `127.0.0.1:8000`. Then send `/menu` to the bot in your Telegram group.

## Art, tools, keys, deploys and models

**3D models (Meshy).** `generate_art(..., output="model3d")` makes a 3D model instead of an image: from text (preview mesh, then a textured refine) or from a reference image (`image_path`), as glb/fbx/obj/usdz/stl. Set `MESHY_API_KEY` with `/keys` (nothing else: Meshy always uses its newest model): the tool appears without a restart. It goes through the same approval and review as images: the operator sees Meshy's preview with ✅ Accept / ✏️ Feedback (Feedback makes a revised model), and agents get `get_task_result(...).models[0].path` only after Accept. Every connected agent (Claude Code, OpenCode, Codex, Antigravity, Cursor) uses it through the same `agent-gateway` MCP server. Meshy charges credits per task (needs a paid plan; failed tasks are refunded).

`/mcp` in Telegram switches each tool agents call (`generate_art`, `delegate_task`, `ask_agent`, ...) on or off while the gateway runs: the next call obeys, no restart. The choice is saved and survives restarts. A switch never touched starts from the config (`generate_art`: fal's `enabled` in `config/providers/models.yaml`; the rest: on). Turning `generate_art` off also stops image generation in workflows.

`/agents` lists every agent in three groups: 🖥 app (desktop app or IDE, e.g. Claude desktop, Cursor), ⌨️ CLI (terminal: `agw session`, Codex, OpenCode, ...) and ☁️ API (connectors the gateway calls for tasks: OpenAI, Gemini, DeepSeek, Claude Code headless, OpenCode server, image generation). Sessions get pause/resume and close buttons (close asks first); API agents switch on/off live, like `/mcp`. Claude Code sessions report app or CLI by themselves (from `CLAUDE_CODE_ENTRYPOINT`); other agents are grouped by name. The old role list is `/roles`.

`/keys` shows the provider keys and models that `config/providers/models.yaml` names (`api_key_env`, `model_env`): missing, or set (last 4 characters) and whether the value is saved or comes from `.env`. ✏️ asks for the value in a private chat with the bot (open it once with /start). The bot deletes that message at once, saves the value in the gateway database encrypted with `AGW_SECRET_KEY` from `.env`, and reloads the model and image providers without a restart. Saved values win over `.env` and are loaded again at every start. 🗑 clears a saved value (asks first); `.env` applies again. `uv run agw setup` adds `AGW_SECRET_KEY` when missing. Keep it: without it the saved values cannot be read (re-enter them), and a copy of the database alone reveals nothing. Only allowlisted users; values are never logged or shown back. Telegram keeps chats on its servers, so prefer keys you can rotate.

**Deploy from Telegram.** `/deploy` (or 🚀 in `/menu`) lists the projects that have `4_delivery/deploy/deploy.sh`, then asks for UAT or PROD, the branch (main/develop first, then newest), and dry run or real deploy, and shows a confirmation (PROD real deploys are flagged). It runs `deploy.sh <env> [--dry-run]` in a separate deploy worktree next to the project (`../.agw-deploy/<repo>`, checked out at that branch), so the project's own checkout and uncommitted work are never touched; gitignored `4_delivery/deploy/.env.deploy` is linked in. One deploy per project at a time. The start and the real outcome (exit code, commit, last error line) are posted with `report_deployment`, and the log file follows (kept in `var/deploys`). The first deploy of a project creates the worktree, so the engine imports everything once.

**Models per project.** Providers are set up once and shared; each project picks which models its workflow roles use.

1. `/providers` lists every provider (from `config/providers/models.yaml` plus ones added in Telegram). ➕ adds an OpenAI-compatible preset (OpenRouter, Groq, Mistral, xAI, Together, Fireworks, Qwen/DashScope, Moonshot, Zhipu, Z.AI, Ollama, LM Studio), kept in the database; no YAML edit, no restart. Set its key with `/keys`. (Preset URLs are the providers' documented OpenAI-compatible endpoints; check them against the provider if a call fails.)
2. A provider can run several models: `/findmodel openrouter qwen` lists the provider's own model ids (tap to copy), `/addmodel openrouter qwen/qwen3-coder` adds one.
3. `/plan p#` sets, per workflow role (designer, implementer, assistant, ...), an ordered list of models and runtimes for that project. A step for that role tries them first, then the role's defaults; a model that fails or is switched off in `/agents` falls through to the next. A project without a plan keeps the defaults.


## CLI

| Command | Purpose |
|---|---|
| `uv run agw setup` | secrets + global setup (`--agents LIST` other agents, `--repo PATH…` per-repo files, `--template` for template repos, `--remove`) |
| `uv run agw dev` | OpenCode + gateway together |
| `uv run agw doctor` | what is configured and reachable (also on Telegram: `/setup`) |
| `uv run agw open <name>` | open a project folder in a Claude session (Telegram: `/open`) |
| `agw session [--agent opencode\|codex\|antigravity]` | in a repo: Claude Code that takes work from Telegram (push + pull), or another agent run once a minute |
| `uv run agw serve [--port 8000] [--reload]` | gateway only (REST, Telegram, `/mcp`) |
| `uv run agw opencode` | `opencode serve` with `OPENCODE_PASSWORD`/`OPENCODE_URL` from `.env` |
| `uv run agw validate` | validate `config/` |

Older names still work: `init-env`, `mcp-setup`, `global-setup`, `repo-setup`.


## REST API and Telegram commands

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

## Telegram setup

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
