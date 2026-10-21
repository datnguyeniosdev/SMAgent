# Workflow Recovery

## Storage guarantees

- Backend: SQLite at `DATA_DIR/STATE_DB` (default `var/state.db`), WAL journal, `synchronous=FULL`. Each `put`/`event` is its own transaction, committed before the engine moves on.
- Persisted:
  - tasks (incl. the normalized request and routing decision)
  - workflow runs (the full definition + version)
  - step records with attempt history
  - approval requests and decisions
  - artifact and attachment metadata
  - agent sessions, tool execution records
  - idempotency keys
  - all events
- **Not** persisted:
  - Quick Action tokens (Telegram buttons), because they're in-memory by design. After a restart, old buttons answer "unknown or expired action" and new approval requests are issued.
  - Capability status evidence: rebuilt on start.
- `STATE_DB=:memory:` disables durability (tests use it).
- Single process only: one gateway per database. There's no cross-process locking.

## Procedure (`Orchestrator.recover()`, called by `start()`)

For every non-terminal root task in the store:

| Task situation | Action |
|---|---|
| queued / planning / waiting_for_user, or no run yet | re-plan from the stored request (planning has no side effects) |
| run exists | load the run and step records, then `engine.prepare_recovery`, then resume `engine.execute` with those records |

`prepare_recovery` per step:

| Step state at crash | Idempotent? | Result |
|---|---|---|
| succeeded / failed / skipped / … | – | kept; never re-executed |
| running, retry_waiting | yes | → `ready`, runs again |
| running, retry_waiting | **no** | → `unknown` ("interrupted by restart; non-idempotent step may have completed"), **never replayed**; dependents are skipped and the task ends `unknown` for manual reconciliation |
| waiting_for_approval | – | → `ready`; approval is requested again unless the decision was already recorded |

## Proof

`tests/test_recovery.py` (real processes, no mocks) and demo F in [DEMO_EVIDENCE.md](DEMO_EVIDENCE.md):
1. The gateway process starts two tasks.
   - Task 1: a quick step, then an **idempotent** 30 s step.
   - Task 2: a quick step, then a **non-idempotent** step that writes `charged` to a log and sleeps 30 s.
2. When both long steps are running, the process is killed with `os._exit(9)` (no cleanup handlers).
3. A new process opens the same database and calls `recover()`.
4. Verified:
   - The idempotent task resumes and **succeeds**.
   - Its completed quick step is **not** re-run.
   - The non-idempotent step is **unknown** and the side-effect log still contains exactly one `charged`.

## Limitations

- In-flight agent/runtime sessions (Claude Code processes) die with the gateway. Their steps are non-idempotent and are reported `unknown`, never reconciled automatically.
- Git worktrees survive and are listed in the report, but they aren't re-attached to a fresh task automatically.
- There is no automatic reconciliation API yet. Inspect the task's events/workflow, then `/retry` (a new task) if appropriate.
