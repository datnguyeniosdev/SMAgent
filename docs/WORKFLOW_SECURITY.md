# Workflow Security

Workflow-specific guarantees on top of the [platform security model](SECURITY_MODEL.md).

| Threat | Mitigation | Evidence |
|---|---|---|
| A submitted workflow references a tool/executor that doesn't exist or isn't available | `Planner.validate` rejects unknown tools, validators, internal ops and plugin operations, and non-routable tools, **before** anything runs | `test_engine.py::test_graph_validation_errors`, `test_platform.py::test_direct_tool_task` |
| A workflow bypasses policy by calling tools directly | Tool steps only run through `ToolExecutor` with `TOOL_PERMISSIONS ∩ step.permissions`. A definition can narrow permissions, never widen them | `test_tools.py::test_policy_permissions_and_workspace` (denial names the missing permission, e.g. `mcp:execute`) |
| Approval skipped | Approval-requiring tools, `workspace.write` agents and `approval` steps always wait for a persisted decision. `require_approval` on the request adds more gates. Decisions are single use and expire | `test_engine.py::test_approval_checkpoint_granted_and_denied`, demo E |
| Replaying a side effect | Non-idempotent steps are never auto-retried. A timeout/crash/cancel of one is `unknown`. Recovery marks in-flight non-idempotent steps `unknown` and never re-runs them. Tool call ids dedupe within an attempt | `test_engine.py::test_non_idempotent_never_blindly_retried`, `test_recovery.py` |
| A definition claims a side-effecting step is idempotent to get retries | `idempotent` can only be lowered: effective = derived ∧ declared | `Orchestrator.is_idempotent` |
| A downstream step consumes a missing or foreign artifact | Artifacts returned by a step must exist and belong to the task family (or be explicit inputs) before dependents unblock. Reads re-check the hash | `test_engine.py::test_output_contract_and_artifact_check` |
| Cross-task data access | `read_input` only resolves the task's own attachments and artifacts. Attachments are bound to one task | `test_core.py::test_attachment_task_isolation` |
| Concurrent writes to one workspace | Single writer per worktree for `workspace:write` tools. A per-task lock creates the worktree exactly once. Parallel agent candidates get separate worktrees | `test_orchestrator.py::test_single_writer_per_workspace`, `::test_parallel_separate_workspaces` |
| Runaway execution | Per-step timeout, per-workflow timeout, bounded concurrency, bounded retries with optional `max_elapsed_s`, 3 clarification rounds max | `test_engine.py` |
| Constraint loss | User constraints are copied into the validated workflow and can't be dropped by a plan | `test_platform.py::test_constraints_preserved_and_idempotency` |
| Duplicate submission | `idempotency_key` → the same task is returned | same test |

Not covered: multi-tenant isolation (there's one allowlist and no tenant separation), per-user quotas, and signed workflow definitions.
