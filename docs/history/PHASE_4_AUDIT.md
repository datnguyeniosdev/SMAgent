# Phase 4 Audit

Taken when the Phase 4 request arrived, during Phase 3 implementation. At that point the generic intake, registry, planner and plugin contracts were in progress, and Phase 3 already required a workflow engine.

| Area | State at audit | Gap vs Phase 4 |
|---|---|---|
| Workflow execution | Phase 1 procedural coroutines plus a planned Phase 3 engine | needed: versioned definitions, graph validation, a step state machine, bounded concurrency, output contracts, retry/backoff, failure strategies, compensation |
| Persistence | **in memory only** (Phase 1 documented the loss on restart) | needed durable state + recovery → smallest extension: stdlib `sqlite3`, no new dependency |
| Approvals | in-memory futures keyed by task; tokens in memory | needed persisted requests and decisions, approver identity, multiple approvals per task |
| Cancellation | asyncio cancel + connector `cancel()` | QA subprocesses were **not** killed on cancel (bug found and fixed); in-flight side effects must be reported `unknown` |
| Idempotency | none at task or tool level | needed task idempotency keys, a tool call id, and never retrying non-idempotent steps |
| Events | log lines and Telegram messages only | needed structured, persisted events with correlation/task/run/step ids |
| Baseline | 69 tests passing (Phase 1), lint clean | – |

**Plan.** Build the engine once to Phase 4's bar and make the Phase 3 planner and plugins emit `WorkflowDef`s for it. Persist every transition before continuing. Prove recovery with a real process kill. Keep Phase 1 behaviour as characterization tests.
