# Workflow Engine

`app/workflow.py` (definitions, validation, state machine, contracts) and `app/engine.py` (scheduler). Executors are injected by the task service, so the engine itself knows no domain.

## Definition

```json
{
  "id": "demo.parallel", "version": "1", "domain": "general",
  "input_schema": null, "output_schema": null,
  "max_concurrency": 4, "timeout_s": 3600,
  "failure_policy": "fail_fast | continue", "allow_partial": false,
  "steps": [
    {"id": "a", "executor": "tool", "ref": "qa.run", "inputs": {"check": "sleep_a"}},
    {"id": "b", "executor": "tool", "ref": "qa.run", "inputs": {"check": "sleep_b"}},
    {"id": "agg", "executor": "internal", "ref": "aggregate", "depends_on": ["a", "b"],
     "inputs": {"a": "$steps.a.output.status", "b": "$steps.b.output.status"},
     "condition": {"ref": "$steps.a.output.status", "op": "eq", "value": "passed"},
     "output_schema": {"type": "object"}, "timeout_s": 60,
     "retry": {"max_attempts": 3, "delay_s": 1, "backoff": 2, "max_elapsed_s": 30, "retry_on": ["timeout", "transient"]},
     "idempotent": null, "requires_approval": false, "permissions": [],
     "on_failure": "fail_fast", "compensate_with": null, "required_capabilities": []}
  ],
  "compensations": [],
  "success_criteria": [{"kind": "step_succeeded | output_equals | artifact_exists | gate_passed", "step": "agg"}],
  "outputs": {"result": "$steps.agg.output"},
  "constraints": []
}
```

Submit one directly with `POST /api/v1/tasks {"objective": "...", "workflow": {...}}`. It is validated exactly like plugin-generated plans.

**Executors.**
- `tool`: registered tool, through policy.
- `agent`: role + required capabilities, routed via the registry.
- `plugin`: `<plugin>.<operation>`.
- `validator`: core or plugin gate.
- `internal`: `aggregate`, `select`, `assert`.
- `approval`: checkpoint.

**Mappings.**
- `$input.<path>` (request objective + `data`).
- `$task.objective|task_id|attachment_ids`.
- `$steps.<id>.output|artifacts|state.<path>`.

A `$steps` mapping may only read an **ancestor** step.

## Validation before execution

Rejected with actionable errors (`InvalidWorkflow.errors`):
- empty workflows
- duplicate ids or invalid step ids
- unknown dependencies, self-dependencies, cycles (the cycle path is printed)
- malformed mappings, or mappings that read a non-ancestor
- unknown compensation steps
- missing refs, unknown tools, executors, validators or internal ops
- tools that aren't routable

Missing capabilities are reported as `unsupported`. Constraints from the request are re-added if a plan dropped them.

## Step state machine

```
pending → ready → running → succeeded
   │        │        ├→ retry_waiting → running        (idempotent + retryable + attempts left + time left)
   │        │        ├→ failed | timed_out | unknown | cancelled
   │        ├→ waiting_for_approval → ready → running   (→ cancelled on denial/expiry)
   └→ skipped (condition false or dependency failed/skipped)
running/retry_waiting/waiting_for_approval → ready     (recovery only)
```

Every transition is enforced (`StepRecord.transition`) and persisted before the engine continues. Records track:
- attempts with timestamps, error and category
- executor ref, output, artifact ids
- approval id and approver
- start/finish time and duration

## Scheduling

- Deterministic `pending → ready` evaluation in definition order; dependents stay blocked until every prerequisite has **succeeded**.
- Ready steps start while fewer than `max_concurrency` are in flight. There is no unbounded queue: the engine only creates tasks for ready steps under the limit (backpressure).
- Outputs are validated against `output_schema`. Every returned artifact must exist and belong to the task before a dependent can unblock.
- `fail_fast` (default) cancels in-flight steps after a failure. With `continue`, independent branches keep running and dependents are skipped.
- The final state is `succeeded` only if:
  - no step failed
  - every mandatory validator step passed
  - every success criterion holds

  Otherwise it is `partial` (if `allow_partial`), `failed`, `cancelled` (cancel or approval denial) or `unknown`.

## Retry, timeout, idempotency

| Situation | Behaviour |
|---|---|
| Idempotent step, retryable error, attempts left | `retry_waiting`, sleep `delay_s × backoff^n`, re-run (a new tool call id per attempt) |
| Non-idempotent step (tool `side_effects`, `workspace.write` agent, plugin op not listed idempotent) | never auto-retried; the error says `not retried: step has side effects` |
| Step timeout, idempotent | `timed_out` |
| Step timeout or crash, non-idempotent | `unknown` (the operation may have happened) |
| Executor raises an unexpected exception | `failed` (idempotent) / `unknown`; logged |
| Runtime auth/provider error (e.g. Claude 401) | `failed` with the provider category |

A definition can only make a step *less* idempotent (`idempotent: false`), never more.

## Cancellation

Task cancellation (`/stop`, `POST /tasks/{id}/cancel`, Stop button) propagates in this order:
1. agent connector `cancel()`: SIGINT → SIGTERM → SIGKILL to the process group
2. pending approval/clarification futures
3. the engine task, which cancels each running step
4. QA subprocesses, killed on cancellation

States after cancellation:
- Steps that never started: `cancelled`.
- An in-flight **non-idempotent** step: `unknown`, and the run becomes `unknown`, because the side effect may already have happened.
- Idempotent in-flight steps: `cancelled`.

## Compensation

`compensate_with` names a step in `compensations`. Compensations run only when the workflow ends failed/unknown/cancelled/partial, newest completed step first. Each outcome (`succeeded`/`failed` + output/error) is recorded in `run.compensation` and published. Compensation is **not** a rollback guarantee.

## Events

Persisted to `events` and returned by `GET /api/v1/tasks/{id}/events`:
- `workflow.created/started/completed`
- `step.scheduled/started/completed/failed/retry_scheduled`
- `approval.requested/granted/denied`
- `artifact.produced`, `cancellation.requested`, `compensation.executed`
- `tool.started/finished`, `task.state`, `task.routed`, `task.recovered`, `clarification.answered`

Telegram only gets step failures, retries, approvals, tool results, and the final report (plus step completions when `TELEGRAM_PROGRESS_UPDATES=true`).

## Evidence

Unit tests: `tests/test_engine.py` (16). Real runs: [DEMO_EVIDENCE.md](DEMO_EVIDENCE.md) A–G. Recovery: [WORKFLOW_RECOVERY.md](WORKFLOW_RECOVERY.md).
