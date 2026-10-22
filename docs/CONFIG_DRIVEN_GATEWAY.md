# Config-Driven Gateway

Audit: [CONFIG_UPGRADE_AUDIT.md](CONFIG_UPGRADE_AUDIT.md).

This upgrade puts a declarative layer on top of the existing engine, store, tool subsystem and task service. Nothing was rewritten. Telegram, REST, CLI and the new MCP server all call the same `Orchestrator` (task service) and `CollaborationService`.

## Concepts (kept separate)

| Concept | Where | Example |
|---|---|---|
| **Agent**: a logical worker/role | `config/agents/*.yaml` | `designer` declares `design.layout_analysis` and `general.answer` |
| **Capability**: a typed ability with input/output JSON Schemas | `config/capabilities/*.yaml` | `design.layout_analysis` |
| **Executor**: what performs a capability | inside a capability, `executors: [...]` | `provider:gemini` (priority 10), `provider:openai` (20) |
| **Tool**: a concrete operation in the tool registry | built-in, plugin or MCP | `workspace.create_file`, `mcp.unity.editor_status` |
| **Model provider**: a model API | `config/providers/*.yaml` | Gemini REST, OpenAI-compatible |
| **Agent runtime**: an external coding agent | built-in adapter | `claude_code` (headless CLI) |
| **Workflow**: a versioned step graph | `config/workflows/*.yaml` | `visual_feedback` v1 |

Executor types:
- `tool`: registered tool, through the policy engine.
- `provider`: `generate` or `generate_structured`; images are passed from artifact refs.
- `runtime`: `new_execution` or `return_to_caller`.
- `plugin`: `<plugin>.<op>`.
- `internal`: `context.load`, `prompt.build`, `report.publish`, `aggregate`, `select`, `assert`.

## Configuration

```
config/app.yaml          schema_version, app, telegram (discussion_chat_id, max_report_photos)
config/agents/           agents: [{id, name, enabled, capabilities[], executor_preference[]}]
config/capabilities/     capabilities: [{id, input_schema, output_schema, executors[], timeout_s, retry, side_effects, requires_approval}]
config/providers/        providers: [{id, type: gemini|openai_compatible, api_key_env, model|model_env, base_url, capabilities[], json_mode}]
config/mcp_servers/      mcp_servers: [{name, enabled, command|url, allowed_tools[], read_only_tools[]}]
config/routing/          routing: {default_workflow, plugin_fallback, dynamic_planning: {enabled: false}, routes[], feedback_routes[]}
config/workflows/        workflows: [WorkflowDef + enabled]   (step executor `capability` with optional `agent`)
config/policies/         policies: {approvals[], provider_allowlist, tool_allowlist, mcp_url_hosts, trusted_plugin_modules, artifact_retention_days}
```

**Validation.** Every file needs `schema_version: 1`, and each item is validated with pydantic and JSON Schema. Checks:
- **Ids and references:** duplicate ids are reported with both files; agent → capability, capability → provider/runtime/internal op, route → workflow, workflow step → capability, and step agent declares the capability.
- **Graph checks:** cycles, unknown dependencies, and mappings to non-ancestor steps.
- **Secrets:** any `*key/token/secret/password` value or secret-looking literal is rejected; use `*_env`.
- **SSRF:** MCP URLs whose host isn't in `policies.mcp_url_hosts` are rejected.
- **Not supported:** `dynamic_planning.enabled: true` is rejected (no verified planner model).

```bash
uv run python -m app.gateway_config config
```

At startup an invalid configuration aborts with these diagnostics. Over REST: `GET /api/v1/config/validate` and `POST /api/v1/config/reload` (validates first, swaps only if valid).

**Precedence:** built-in defaults < YAML < environment.
- Secrets and deployment values (keys, tokens, chat IDs, `DATA_DIR`, `WORKSPACE_ROOT`) stay in the environment.
- `TELEGRAM_DISCUSSION_CHAT_ID` overrides `telegram.discussion_chat_id`.
- `CONFIG_DIR` (default `config`) selects the directory; without `app.yaml` the gateway runs in legacy plugin-only mode.

**Reload semantics:**
- New tasks use the new registry, routes and workflows.
- Running workflows keep the definition persisted with their run; every run stores `workflow.id` + `workflow.version`.
- Reloading does not re-discover MCP servers or rebuild provider clients; restart for those.

## Routing

1. `workflow` (explicit definition) or `workflow_id` → that workflow.
2. `tool` → a direct tool step.
3. **Configured routes**: a known `task_type` uses its route. Otherwise, the first route (by priority) whose `match` (keywords / domains / attachment MIME prefixes / sources) fits.
4. Explicit plugin `domain`, then plugin classification (`plugin_fallback`).
5. `default_workflow`.
6. Otherwise clarification. AI planning is never used silently.

The routing decision (mode, workflow id/version, reasons, candidates) is stored on the task and in events.

## Capability router

`AgentCapabilityRegistry.select(capability, agent)` orders executors by the agent's `executor_preference`, then by `priority`, and returns the first **eligible** one. Eligibility checks:
- required env vars
- policy allowlists
- granted permissions
- the provider/runtime is configured and routable
- the tool is registered and routable (MCP tools must have been discovered)
- the plugin operation exists

Structured errors: `unknown_agent`, `agent_disabled`, `unknown_capability`, `capability_disabled`, `capability_unavailable`, `missing_credentials`, `mcp_disconnected`, `invalid_input`, `invalid_output`, `forbidden`.

At run time, a `capability` step:
1. Validates input against the capability schema.
2. Runs the selected executor.
3. On availability/transport failures (`auth`, `network`, `timeout`, `unavailable`, …), falls back to the next eligible executor (at most 3). Every attempt is recorded in `_executor_attempts`.
4. Validates the output schema.

Capability `retry` and `timeout_s` apply unless the workflow step sets its own. Side-effecting capabilities are never auto-retried.

Statuses appear in the central registry as `capability:<id>` and `agent:<id>` (`validated` / `operational` / `degraded` / `unavailable`, with evidence). Runtimes and providers are `runtime:<id>` and `provider:<id>`.

**Adding an agent with existing capabilities.** Drop a file into `config/agents/`, then validate and reload (or restart). No code changes. Tested in `test_uc02_config_only_agent_and_workflow`, which also adds a workflow and a route through config only.

## Telegram report → reply → follow-up

1. A report is created by `submit_report` (MCP), `POST /api/v1/tasks/{id}/reports`, or the `report.publish` capability.
   - Screenshots are validated (format/size), stored as artifacts and marked `passed`.
   - `agent_status` (what the agent claims) is kept separate from `verified_status`. `verified` requires a succeeded task with at least one passed gate, no failed gate, and no pending handoff.
2. `publish_report` posts the text to the channel, then the photos as replies to it, and stores `channel_chat_id` / `channel_message_id` / `photo_message_ids`.
3. When the channel auto-forwards the post into the linked Discussion Group, the copy (`is_automatic_forward`, `forward_origin`) is linked to `discussion_message_id`.
4. A reply in the Discussion Group resolves through the replied-to message: discussion copy, channel post, or report photo.
   - If it can't be resolved, the bot asks the user to reply to a report or use `/feedback <task_id|report_id> <text>`. It never guesses.
   - Unauthorized users are ignored.
   - Duplicate `update_id`s are processed once.
5. The feedback is persisted (`feedback` record with task, report, user, update id and message id). Then `routing.feedback_routes` picks a follow-up workflow (e.g. `visual` needs report images plus layout keywords; otherwise `acknowledge`).
6. The follow-up task carries `parent_task_id`, the original constraints, and the report's screenshots as explicit input artifacts, so other tasks' artifacts stay inaccessible.

Correlation records: `report`, `tg_msg` (message-id index), `feedback`, `telegram_update`, `handoff`, `human_input`, all in the existing SQLite store.

> Anonymous admins: a group admin with "Remain anonymous" posts as `GroupAnonymousBot` (id 1087968824), which is never on the allowlist. Turn it off for operators.

### Live sessions: push work into an open Claude Code session

Uses Claude Code **channels** (research preview, Claude Code ≥ 2.1.80, stdio servers only).

```
Telegram /send 2048 fix the pause menu
   → gateway inbox (per project) ─SSE─▶ agent-gateway-channel shim (stdio, inside the session)
   → notifications/claude/channel → the open session starts a turn
   → Claude calls the shim's `reply` tool → report posted to Telegram
   → you reply to that post (text + photos) → pushed into the same session
```

- **Start a session:** run `agw session` in a repo. It checks the Claude Code version and launches `claude --dangerously-load-development-channels server:agent-gateway-channel` with `AGW_CHANNEL=1`.
- **Shim (`app/channel_shim.py`, stdlib only):**
  - only listens when `AGW_CHANNEL=1` is set; in an ordinary session it stays idle, so nothing is routed to a session that can't receive it
  - negotiates MCP protocol `2025-06-18` (the docs say a channel on `2026-07-28` is not registered)
- **Gateway endpoints:** `/api/v1/channel/stream`, `/reply` and `/sessions` are **local-only, no token**: loopback client, local `Host`, no foreign `Origin`.
- **Telegram:**
  - `/send <project> <text>` matches the project by name, tag or a unique fragment
  - `/live` lists open sessions
  - with no session open, messages wait and are delivered when one opens
- **Session record:** each session gets a record-only task. Claude's answers are its reports, and replies to them route back by `channel_session` instead of starting a workflow.
- **Delivery is at most once:** a message read off the queue just as the shim drops is lost.
- **Telegram stays the gate:** only allowlisted users can `/send` or reply. Channel content is otherwise treated as a request from the user, and the session still confirms commits, pushes and deploys.

### Pull mode, IDs, queue and auto-start

- **Pull mode (any session, including the desktop app):** a recurring job calls the MCP tool `get_messages(project)` and answers with `reply_message(session_id, message_id, text)`. Push and pull share one inbox, and push is preferred when both are open.
- **Short IDs** (stable; shown by `/list` and `/queue`):
  - `s#` for sessions
  - `p#` for projects
  - `m#` for requests

  Commands that take them: `/send s1|p1 <text>`, `/off s1`, `/on s1` (persisted per session), `/queue`, `/cancel m3`.
- **One job at a time:**
  - a handed-over request is *in progress* until `reply_message` answers it, or for at most 60 minutes
  - meanwhile `get_messages` returns `busy` and hands out nothing new, so a session never runs two requests in parallel, and duplicate loops in one session can't double up
  - the default `limit` is 1
- **Cancel:** `/cancel` on a waiting request drops it. On one already in progress it only marks it cancelled: the session isn't interrupted, and its answer is labelled cancelled.
- **Auto-start when a project opens:**
  - `repo-setup` installs a `SessionStart` hook (`agw hook-session-start`) in `.claude/settings.local.json`. It tells Claude to create exactly one every-minute job (checked with `CronList` first). Hooks can't wake an idle session, so in the desktop app this happens with the session's first message.
  - `agw session` starts a fresh session with the loop as its first prompt (`--no-loop` to skip). A resumed session (`--continue` / `--resume`) restores its recurring job instead.

### Receiving updates: polling or webhook

Both modes run the same handlers (aiogram `Router`, e.g. `Command("start")`, the discussion-group listener); only the transport differs.

| `TELEGRAM_MODE` | How updates arrive | Exposure |
|---|---|---|
| `polling` (default) | the gateway long-polls `getUpdates` | none: outbound only |
| `webhook` | Telegram POSTs to `TELEGRAM_WEBHOOK_URL` + `/telegram/webhook` | a public HTTPS endpoint |

Webhook mode in detail:
- **Startup:** `setWebhook` is called with `TELEGRAM_WEBHOOK_SECRET` as `secret_token` (`agw init-env` generates the secret).
- **Each request:** the endpoint accepts only requests whose `X-Telegram-Bot-Api-Secret-Token` matches (constant time) and whose body is valid JSON with an integer `update_id`, at most 1 MB. It answers 200 at once and processes the update in the background.
- **Shutdown:** `deleteWebhook` is called. If a crash leaves a stale gateway webhook behind, polling mode removes it on the next start; it refuses to touch a foreign webhook.
- **No public URL:** with `TELEGRAM_WEBHOOK_URL` empty, `uv run agw dev` starts a Cloudflare quick tunnel (`cloudflared`, no account) and passes its `https://….trycloudflare.com` URL to the gateway. The URL changes on every start, and the gateway re-registers it each time.
- **The tunnel publishes the endpoint on the internet.** The secret token is the only thing protecting it, so keep `.env` private.

### Single-group mode

Set `TELEGRAM_REPORT_CHANNEL_ID` to the control/discussion group id and every message goes to that one group: reports, deploys, questions, approvals.
- Replies resolve directly through the `tg_msg` index (chat + message id), with no channel copy involved.
- A channel linked to the group is no longer posted to, so there are no channel→group duplicates.

**Feedback with screenshots:** any of these attaches the images to the feedback:
- reply to a report with a photo (caption = your note)
- reply with an album (caption on any part)
- photos captioned `/feedback <task_id|report_id> <text>`

How the images are handled:
- They are validated: PNG/JPEG/WebP/GIF, `MAX_ATTACHMENT_BYTES`, at most 10.
- They are stored as `screenshot` artifacts on the source task and listed in `feedback.artifact_ids`.
- They are passed first in the follow-up task's input artifacts and in `context.load` `screenshots`, so the visual workflow analyses them.
- They also satisfy the `requires_report_images` routing condition.

Feedback is acknowledged once, by the bot's threaded `💬 FEEDBACK` reply. The `feedback.acknowledge` workflow only records the feedback and posts nothing.

### Message format

Every outgoing message uses `formatting.envelope`:

```
📋 REPORT · #2048_Shooter_K05 · #tsk_173c88c055     ← prefix · project tag · task tag
Pause menu added                                     ← title
<body: only what the reader needs>
🆔 project 2048 Shooter (K05) · session ses_… · ref rep_…
↩️ Reply to this post to send feedback               ← reply template
```

- **Prefixes:**
  - outcomes: `✅ DONE`, `❌ FAILED`, `⏹ CANCELLED`, `⌛ EXPIRED`, `❓ UNKNOWN`
  - `📋 REPORT`, `💬 FEEDBACK`, `🙋 QUESTION`, `🔐 APPROVAL`, `🚀 DEPLOY`
  - progress: `⚙️ STEP`, `🔧 TOOL`, `🧪 QA`, `🔍 REVIEW`, `⚠️ WARN`, `ℹ️ INFO`
- **Hashtags:** tapping one in Telegram lists every message for that project or task.
- **Project:** `data.project` of the task or of its parent / feedback source. Otherwise the `WORKSPACE_ROOT` folder name, then `gateway`.
- **Session:**
  - the `session_id` the caller passed (MCP `delegate_task`, `submit_report`, `request_human_input`, `report_deployment`)
  - otherwise the latest agent session
  - otherwise the correlation id, which a task shares with its feedback follow-ups
- **Ref:** the id to act on (report, approval, human-input request, deployment).
- **Noise removed:**
  - no "accepted", "started" or "waiting for approval" posts
  - the final message shows the result or error, failing steps, duration, verification and image count; the "View report" button gives the full report
  - a workflow with `final_report: false` (e.g. `feedback.acknowledge`) posts no end-of-run message when it succeeds without images

**Templates:**
- `/templates` lists tap-to-copy commands.
- Sent as a reply to any gateway message, it fills in that message's `tsk_` / `rep_` / `hin_` ids (`/report`, `/steps`, `/feedback`, `/retry`, `/stop`, `/answer`).
- The bot also registers its "/" command menu for the control group at startup.

> Telegram privacy mode: the bot only sees replies to its own messages and commands. Replies to the *automatic forward* copy also include `forward_origin`, which is how they resolve. For reliable correlation, disable privacy mode or make the bot an admin of the discussion group.

## Visual feedback workflow (`visual_feedback` v1)

```
context.load ─► design.layout_analysis (designer: Gemini → OpenAI fallback, images from report artifacts)
             └─► prompt.implementation (deterministic) ─► implementation.handoff (implementer) ─► report.publish
```

- **Design output** is validated against the design-spec schema: summary, issues[element/problem/fix], layout, acceptance_criteria, assumptions.
- **Prompt builder** (`collaboration.build_implementation_prompt`):
  - It has fixed section order and sorted artifact refs, so the same inputs produce a byte-identical prompt.
  - It contains the verbatim feedback, all constraints, and a statement that the design is NOT yet implemented.
  - It references only artifacts that exist for the task, and never invents file paths.
- **Handoff contract** (`Handoff` record): `task_id`, `workflow_run_id`, `target`, `mode`, `prompt` + sha256, `context_refs`, `session_ref`, `resumed_session`, `status`, `result_summary`, `artifact_ids`.
  - `new_execution`: starts a **new** Claude Code headless run in the task workspace, with the persisted context in the prompt. Policy requires approval.
  - `return_to_caller`: status `pending_manual_continuation`. The MCP caller fetches the prompt with `get_task_result` and later calls `submit_report` with the `handoff_id`.
  - **Claude Code session resumption is never claimed.** `resumed_session` is always false: remote resume and wake-up are not verified for the installed runtime, and real runs return 401 on this machine.

## OpenCode runtime (verified live)

`app/connectors/opencode.py` drives `opencode serve` (v2.1.0 API under `/api/*`, verified from the server's own `/openapi.json`; the public docs describe the older v1 paths).
- `implementation.handoff` prefers `runtime:opencode.new_execution` (priority 5), then Claude Code, then `return_to_caller`. Policy requires approval for `runtime:opencode.new_execution`.
- **Sessions** are created with `location.directory` = the task's git worktree and an explicit deny-by-default `permissions` ruleset:
  - read/grep/glob/list allowed
  - `edit` allowed (writes outside the directory are OpenCode's `external_directory` action and stay denied)
  - `shell` = ask
- **Shell:** the gateway answers every shell permission request itself. A command is approved `once` only if every command in the request matches `OPENCODE_SHELL_ALLOWLIST` by argv prefix with no shell operators; everything else is `reject`ed. All decisions are recorded in the handoff output (`runtime_evidence.permission_decisions`).
- **Results:** session outcome, tokens, cost, the last assistant message, and the server-side file list (`/diff`). The gateway still runs its **own** QA and git status; the agent summary is never treated as verification.
- **Continuation:** prompting an existing session id is supported and verified (`resume_session`).
- **Cancel:** `cancel_task` → `/api/session/{id}/interrupt`. The task becomes `unknown` and the handoff `unknown`, because edits may already have happened.

Run it:

```bash
OPENCODE_SERVER_PASSWORD=change-me opencode serve --port 4096
```

```bash
OPENCODE_ENABLED=true OPENCODE_PASSWORD=change-me WORKSPACE_ROOT=/path/to/repo QA_COMMANDS='{"tests":"python3 -m pytest -q"}' uv run uvicorn app.main:app --port 8000
```

Then submit `{"objective": "...", "task_type": "code_change"}` and approve the pending approval.

**Observed risk:** OpenCode's default `build` agent permission is `* * allow`. In an unrestricted first test it ran `pip3 install pytest` on the host. Never run OpenCode sessions without the gateway's ruleset.

## Gateway MCP server

Streamable HTTP at `http://127.0.0.1:8000/mcp`, using the same `Authorization: Bearer $API_TOKEN` as the REST API.

Tools:
- `delegate_task`, `get_task_status`, `get_task_result`
- `list_capabilities`, `get_capability_details`
- `cancel_task`, `request_human_input`, `get_human_response`
- `submit_report`, added so agents can report back with screenshots and close a handoff

Register it with Claude Code:

```bash
claude mcp add --transport http --scope project agent-gateway http://127.0.0.1:8000/mcp --header "Authorization: Bearer $API_TOKEN"
```

The gateway is also an MCP **client** for `config/mcp_servers` (allowlisted tools only, URL host allowlist, path confinement). Model APIs such as Gemini use provider adapters, not MCP.

## Use cases

| UC | How | Test | Real-run status here |
|---|---|---|---|
| 01 General task | `delegate_task` or Telegram `/task` → `default_workflow` | `test_uc01_*` | MCP over HTTP verified; the task fails `unsupported` (no model key), as reported |
| 02 Agent via config | add `config/agents/x.yaml`, validate, reload | `test_uc02_*` | REST reload verified in tests |
| 03 Unity task report | `submit_report` with screenshots | `test_uc03_*` | verified over real MCP HTTP (report stored, image validated, `unverified`) |
| 04 Visual feedback reply | Discussion Group reply → feedback → follow-up | `test_uc04_uc05_*`, `test_feedback_correlation_*` | Telegram handler code tested with fake Bot API; live group not configured |
| Code change on OpenCode | task_type `code_change` (`code.implement_and_test`) | `tests/test_opencode.py`, integration `test_real_opencode_runtime` | **verified live**: worktree edits, gateway pytest 2 passed, gate passed, main checkout untouched; cancel → `unknown` |
| 05 Gemini design + Claude prompt | `visual_feedback` | `test_uc04_uc05_*`, `test_handoff_new_execution_*` | Gemini mocked (no key); Claude runtime 401 → falls back to `return_to_caller` |
| 06 Research/document | task_type `research_report` | `test_uc06_*` | real local tools; web search capability shows `mcp_disconnected` |
| 07 Failure/recovery | provider timeout retried; auth error → next provider; nothing eligible → `unsupported` | `test_uc07_*`, `test_recovery.py` | engine recovery verified with a real process kill |
| 08 Human approval | task_type `write_file` (policy approval) | `test_uc08_*`, `test_expired_approval_*` | file absent until approved; duplicate decision rejected; expiry → `expired` |
| 09 Concurrent tasks | two reports + two feedbacks in parallel | `test_uc09_*` | contexts, screenshots and artifact access stay separate |

## State mapping

| Requested | Implemented task state |
|---|---|
| PENDING | `created`, `queued` |
| RUNNING | `planning`, `running`, `validating` |
| WAITING_FOR_HUMAN | `waiting_for_user` |
| WAITING_FOR_APPROVAL | `waiting_for_approval` |
| SUCCEEDED / FAILED / CANCELLED | same |
| EXPIRED | `expired` (approval or clarification timeout) |
| (outcome unknowable) | `unknown` |

Step states: see [WORKFLOW_ENGINE.md](WORKFLOW_ENGINE.md).

## Migration from the previous version

- **Capability ids:** `agent:claude_code` is now `runtime:claude_code`, and `agent:openai` / `agent:deepseek` are now `provider:openai` / `provider:deepseek`. `agent:<id>` now means a configured logical agent.
- **Providers:** with a `config/` directory, providers come from `config/providers`. The legacy env-only provider setup is used only when no config is present.
- **Plugins:** a config enables the trusted-plugin policy, so modules outside `policies.trusted_plugin_modules` are not loaded.
- **Tasks:** `TaskRequest` gains `workflow_id` and `source`. Task state gains `expired`.
- **REST, Telegram and MCP:**
  - **REST:** `/api/v1/config/*`, `/api/v1/agents/registry`, `/tasks/{id}/reports`, `/reports/{id}/feedback`, `/tasks/{id}/handoffs`, `/human-input`.
  - **Telegram:** `/feedback`; `/answer` accepts `hin_` request ids.
  - **MCP:** `/mcp`.
- **New dependency:** `pyyaml` (it was already installed transitively by uvicorn).

## Limitations

- Gemini, OpenAI and DeepSeek adapters are unit-tested only (no keys). Claude Code runs fail auth (401), so `new_execution` is unverified live; the fallback to `return_to_caller` is.
- The Telegram Discussion Group flow is not live-verified: chat IDs are placeholders and the bot has no group membership yet.
- Artifact retention runs on demand (`POST /api/v1/maintenance/cleanup-artifacts`). It deletes only artifacts of finished tasks older than `artifact_retention_days` that no active task references. There's no scheduler; run it from cron if needed.
- Telegram buttons are in memory; approvals are re-issued after a restart.
- Single process, single tenant.
