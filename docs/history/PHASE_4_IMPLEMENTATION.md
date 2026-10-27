# Phase 4 Implementation: Workflow Engine

Audit: [PHASE_4_AUDIT.md](PHASE_4_AUDIT.md).
- Engine: [WORKFLOW_ENGINE.md](WORKFLOW_ENGINE.md)
- Recovery: [WORKFLOW_RECOVERY.md](WORKFLOW_RECOVERY.md)
- Security: [WORKFLOW_SECURITY.md](WORKFLOW_SECURITY.md)

## Files

`app/workflow.py`, `app/engine.py` and `app/store.py` are new; `app/orchestrator.py` (engine host, executors, approvals, recovery) changed. Tests: `tests/test_engine.py`, `tests/test_recovery.py`, fixtures `tests/fixtures/slow_plugin.py`, `tests/fixtures/recovery_driver.py`.

## State and persistence guarantees

SQLite (WAL, `synchronous=FULL`). Every task, step and approval transition and every event is committed before execution continues. Recovery:
- resumes runs
- re-runs interrupted **idempotent** steps
- never re-runs completed steps
- marks interrupted **non-idempotent** steps `unknown` instead of replaying them

Quick Action tokens are in memory, so approvals are re-issued after a restart. Single gateway process per database.

## Bugs found while building and demonstrating (all fixed, with tests)

1. An executor exception left the step `running`, so a workflow could look successful. Now `failed`, or `unknown` for side effects.
2. A cancelled QA command kept running. The process group is now killed.
3. Two parallel steps raced to create the task worktree (`branch already exists`). Added a per-task creation lock.
4. Engine retries reused the tool executor's dedupe key, so retries returned the cached failure without re-executing. The key now includes the attempt number (`test_engine_retry_reexecutes_real_tool`).
5. Runtime errors inside plugin operations (Claude 401) were classified `unknown`. They now keep their real category and outcome (`failed`/`auth`).

## Demonstrations (real, see [DEMO_EVIDENCE.md](DEMO_EVIDENCE.md))

| Demo | Outcome |
|---|---|
| A sequential | succeeded; step outputs mapped into the next step; output validated |
| B parallel | two 1.5 s subprocess checks overlapped; aggregation started only after both finished |
| C1 retry | real MCP tool failed twice and succeeded on attempt 3 (bounded, backoff) |
| C2 failure policy | non-idempotent MCP tool executed once (`not retried: step has side effects`); failing check → gate failed → dependent skipped; independent branch still succeeded; workflow `failed` |
| D cancellation | running 60 s subprocess cancelled in 0.06 s, 0 processes left; step and task `unknown` (side-effect tool) |
| E approval | paused in `waiting_for_approval`; file absent before approval and present after; decision persisted (`approved`, `demo-user`); main checkout untouched |
| F recovery | real `os._exit(9)` mid-run; after restart the idempotent task succeeded, the non-idempotent task is `unknown` with exactly one side effect recorded |
| G multi-agent | real Claude Code invoked after approval → 401 → step `failed` (`auth`), capability `degraded`. Artifact handoff and independent review are verified only with mocked agents (`tests/test_orchestrator.py`) |

## Run

```bash
uv run python -m scripts.demo --unity-project /path/to/UnityProject
```

```bash
uv run python -m scripts.render_evidence
```
