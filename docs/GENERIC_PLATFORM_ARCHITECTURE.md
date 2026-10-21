# Generic Platform Architecture

One gateway accepts any kind of task, selects verified capabilities, runs a validated workflow, checks real outputs and returns an evidence-backed result. Domain knowledge lives only in plugins.

```
 Interfaces (adapters)          Telegram (aiogram, polling)     REST (FastAPI, bearer)     scripts/ CLI
        │  normalized TaskRequest  ▲ normalized events/report envelope
        ▼                          │
 ┌──────────────────────────── Task service  app/orchestrator.py ─────────────────────────────┐
 │ intake (app/intake.py) → route + plan (app/planner.py) → validate → WorkflowEngine          │
 │ approvals · clarifications · cancellation · retry · idempotency · envelope · recovery        │
 └───────┬──────────────────────┬──────────────────────┬────────────────────┬──────────────────┘
         ▼                      ▼                      ▼                    ▼
  Capability registry    Workflow engine         Tool subsystem        Plugin manager
  app/capabilities.py    app/engine.py           app/tools.py          app/plugins/base.py
  status ladder +        DAG, concurrency,       registry, schema,     manifests, isolation,
  evidence, health       retries, timeouts,      policy, executor,     permission ceiling
                         approvals, compensation records, events
         │                      │                      │                    │
         ▼                      ▼                      ▼                    ▼
  Agents/providers        Store (SQLite)         Built-in tools        Plugins: software, documents,
  claude_code, openai,    app/store.py           MCP client            data, media, unity
  deepseek, images        records + events       app/mcp_client.py     (app/plugins/*.py)
         │                                              │
         ▼                                              ▼
  Workspaces (git worktrees, app/workspace.py)   Artifacts/attachments (app/storage.py)
```

## Layers and their contracts

| Layer | Module | Contract |
|---|---|---|
| Interface | `telegram_bot.py`, `main.py` | Build a `TaskRequest`, call `Orchestrator.submit_request / stop / answer / decide_approval / apply_action`, render `report()` / `envelope()`. No scheduling logic. |
| Intake | `intake.py` | `TaskRequest`: objective, user/tenant, conversation, domain/task_type, attachments, input artifacts, structured `data`, constraints, preferences, output format, mode, direct tool, explicit workflow, timeout/deadline, approval flag, idempotency key, parent/correlation ids. |
| Planner | `planner.py` | `route()` → `RoutingDecision` (mode, plugin, task type, score, reasons, candidates). `plan()` → `PlanResult` (workflow or clarification question). `validate()` rejects unknown tools/executors, unavailable capabilities, invalid graphs, and preserves constraints. |
| Capabilities | `capabilities.py` | `CapabilityManifest` per agent/provider/tool/MCP server/plugin; statuses declared → discovered → configured → validated → operational / degraded / unavailable. |
| Agent runtime | `connectors/claude_code.py` | `execute`, `cancel`, `health_status`, capability set. Desktop is not supported (no public API). |
| Model providers | `connectors/http_providers.py` | `execute`, multi-turn `chat`, free `health` (`GET /models`), per-provider capability sets (DeepSeek: json_object only, image input opt-in). |
| Tools | `tools.py`, `builtin_tools.py`, `mcp_client.py`, `tool_loop.py` | `ToolDefinition` (id, schema, permissions, risk, environment, timeout, approval, side effects, output schema) → `ToolExecutor` (validate → policy → run → validate output → record → publish). |
| Workflow engine | `workflow.py`, `engine.py` | `WorkflowDef` / `StepDef` / `StepRecord` / `WorkflowRun`; see [WORKFLOW_ENGINE.md](WORKFLOW_ENGINE.md). |
| Plugins | `plugins/` | See [PLUGIN_DEVELOPMENT.md](PLUGIN_DEVELOPMENT.md). |
| Artifacts | `storage.py` | Create-only files; id, kind, MIME, sha256, size, task, producing step, producer, validation status; hash re-checked on read. |
| Quality gates | `workflow.GateResult` + validators | `passed | failed | unknown | skipped`, evidence, findings, metrics, timestamp, error. Only `passed` unblocks success. |
| Persistence | `store.py` | SQLite (WAL, `synchronous=FULL`): tasks, runs, steps, attempts, approvals, artifacts, attachments, sessions, tool records, idempotency keys, events. |

## Task lifecycle

`created → queued → planning → (waiting_for_user ↔ planning) → running ⇄ waiting_for_approval → validating → succeeded | failed | cancelled | unknown`

1. Interface authenticates the caller and builds a `TaskRequest`.
2. `submit_request` binds attachments (one task only), checks the idempotency key, persists the task.
3. Planning: explicit workflow / direct tool / explicit domain / classification / model fallback / clarification.
4. Validation against registry + policy; constraints copied into the workflow.
5. Engine executes; every transition and event is persisted; Telegram receives concise events.
6. Validating: run outcome + mandatory gates + success criteria → terminal state. An LLM claim never counts as success.
7. A `ResultEnvelope` (`result.json` artifact, `GET /api/v1/tasks/{id}/result`) and a short report are delivered. Delivery failures are counted on the task and never change the execution outcome.

## What stayed from Phase 1

The Phase 1 workflows still exist and are covered by the original 69 regression tests, now routed generically:
`ask → general/answer`, `code → software/implement`, `delegate_review → software/implement_reviewed`, `parallel → software/parallel_implement`, `design_implement_review → unity/feature`.
