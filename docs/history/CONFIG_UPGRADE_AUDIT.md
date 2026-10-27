# Config-Driven Upgrade: Repository Audit (2026-10-20)

Baseline: commit `adf75dd` plus uncommitted Phase 2–5 work, `uv run pytest` → **128 passed, 5 skipped** (opt-in integration), lint clean.

## 1. Implemented and verified

| Feature | Module | Evidence |
|---|---|---|
| Durable workflow engine: DAG, bounded parallelism, conditions, output contracts, retries (side-effect safe), timeouts, cancellation, approvals, compensation, crash recovery | `workflow.py`, `engine.py`, `store.py` (SQLite) | `test_engine.py`, `test_recovery.py` (real `os._exit`), demos |
| Tool subsystem: registry, schema, policy, records, events; workspace/git/QA tools; MCP **client** | `tools.py`, `builtin_tools.py`, `mcp_client.py` | real local MCP stdio, `claude mcp serve` Read |
| Capability status registry (declared → operational/degraded) | `capabilities.py` | tests |
| Generic intake + plugin classifier/planner | `intake.py`, `planner.py`, `plugins/*` | tests, demos |
| Telegram control group + report channel, Quick Actions | `telegram_bot.py` | fake-bot tests; real bot token valid, chat IDs are placeholders |
| REST on the task service | `main.py` | TestClient + uvicorn smoke |

## 2. Partially implemented

- **Routing:** plugin classifiers in Python code decide workflows. There's no configuration, so adding a domain means writing code.
- **"Agent" concept:** conflated with runtimes. `agent:claude_code` in the capability registry is really a runtime. No logical agents, no agent → capability mapping, no executor priority.
- **Providers:** OpenAI/DeepSeek adapters are configured only through env. There's **no Gemini adapter**.
- **Telegram:**
  - Reports go to the channel only. No report records, no message-id correlation.
  - **The Discussion Group isn't handled at all.** Replies and feedback can't be associated with a task.
  - No update-id dedupe.
- **Human input:** clarification exists (`/answer`, button), but there's no generic request/response record and no access from MCP.
- **Handoff:** the Claude Code connector runs headless `-p` with `--resume`, but there's no explicit handoff contract or record. Resume is unverified (401).

## 3. Missing

- YAML configuration (agents, capabilities, providers, MCP servers, routing, workflows, policies, Telegram) with schema versioning, reference validation and diagnostics; a validate command; reload.
- Capability router with multiple executors per capability; structured capability errors.
- Gateway **MCP server** (upstream tools such as `delegate_task`).
- Report/feedback/handoff/human-input persistence; visual-feedback workflow; deterministic Prompt Builder.
- Policy-driven approval rules and provider/tool allowlists; MCP URL host allowlist (SSRF).
- Artifact retention/cleanup.

## 4. Reuse (no rewrite)

The following are reused unchanged:
- **Engine and records:** engine, store, tool executor and policy, MCP client, capability status registry.
- **Task service:** `Orchestrator`, which stays the single orchestration core.
- **Plugins:** they become executors (operations, tools, validators) referenced by configuration.
- **Interfaces and agents:** Telegram gateway, REST, workspace agents.

**New, thin layers:**
- config loader
- agent/capability definitions and router
- `capability` step executor
- report/feedback service
- prompt builder
- Gemini adapter
- MCP server facade

## 5. Risks and migration

- Capability ids are renamed: connectors become `runtime:claude_code`, `provider:openai`, `provider:deepseek`. `agent:<id>` now means a **logical agent** from configuration. API consumers that read `agent:*` need updating. This is documented in the migration notes.
- Precedence: built-in defaults < YAML < environment. Secrets are **only** environment variables, and the YAML loader rejects inline secret-looking values.
- Telegram correlation depends on the channel being linked to a discussion group; replies must be real replies. Ambiguous feedback is asked about, never guessed.
- Claude Code: remote session resumption is **not verified** (auth fails). Handoff supports `new_execution` (fresh run with persisted context) and `return_to_caller` (pending manual continuation) only.

## 6. Order

1. Config schemas + loader + `configctl validate`.
2. Agent/capability registry + router + `capability` executor.
3. Config routing + YAML workflows (versioned, persisted with runs).
4. Reports/feedback/human input + Telegram discussion correlation + update dedupe.
5. Gemini adapter, prompt builder, handoff contract, visual-feedback workflow.
6. Gateway MCP server.
7. UC-01…09 tests, integration smoke, docs.
