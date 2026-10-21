# Plugin Development

A domain plugin is a Python module with `create_plugin()` returning an object that implements `app.plugins.base.DomainPlugin`. It depends only on core contracts (`intake`, `workflow`, `engine.StepOutput/StepError`, `tools`, `plugins.base`), never on Telegram, REST or other plugins. It may *reference* any registered tool by name in a workflow; that goes through the registry.

## Contract

```python
class MyPlugin:
    manifest = PluginManifest(
        id="mydomain", name="My Domain", version="1.0.0", description="...",
        categories=["..."], keywords=["..."],                 # classification hints
        permissions=["data:read", "artifact:write"],          # ceiling for this plugin's tools
        artifact_types=["report"],
        operations=[OperationSpec(
            task_type="do_thing", description="...",
            required_capabilities=["tool:mydomain.tool", "model.text"],   # checked before planning
            optional_capabilities=[], input_schema={...},                  # validates TaskRequest.data
            side_effects=False, example={"data": {...}})],
    )

    def classify(self, req, attachments) -> Classification | None   # score 0..1 + reasons; None = not mine
    def plan(self, req, task_type, env: PlanningEnv) -> PlanResult  # WorkflowDef, or a clarification question
    def tools(self, services) -> list[ToolDefinition]               # names must start with "mydomain."
    def operations(self) -> dict[str, async fn(ctx: StepContext, inputs) -> StepOutput]
    def validators(self) -> dict[str, async fn(ctx, inputs) -> GateResult]
    def idempotent_operations(self) -> set[str]                      # everything else is never auto-retried

def create_plugin(): return MyPlugin()
```

`PlanningEnv` tells the plugin:
- `has(capability)`, the registered tool names and the configured QA checks
- whether a workspace is configured
- attachment metadata (id, filename, MIME, size)
- its `PLUGIN_CONFIG` entry

It gets no execution handles.

`StepContext` (operations and validators) offers:
- `read_input(ref)` (task-isolated attachments/artifacts)
- `add_artifact()`, `read_artifact()`, `artifact()`
- `run_tool()` (through the policy engine)
- `approval()`, `notify()`, `workspace()` (task scratch dir), `plugin_config()`
- `agents()`, for workspace-agent primitives

## Rules the core enforces

- Manifest validated (pydantic). The id is `^[a-z][a-z0-9_]{1,30}$`; duplicates are rejected.
- Tool names must be prefixed `<id>.`. Tool permissions must be ⊆ manifest permissions ∩ `PLUGIN_PERMISSIONS`. At runtime they're further limited by `TOOL_PERMISSIONS`.
- Every plan is validated (graph, executors, capabilities, constraints) before execution.
- A validator that couldn't run returns `unknown`/`skipped` (exceptions become `unknown`). Only `passed` succeeds, and mandatory validator steps block success. A plugin can't mark a task successful past a failed gate.
- Import, classify or plan errors are isolated to that plugin, and reported in `/plugins` and `GET /api/v1/plugins`.
- Activate or deactivate at runtime: `POST /api/v1/plugins/{id}/activate|deactivate`. A deactivated plugin is never routed to (explicit domain → `not active`).

## Enabling

`PLUGINS=software,documents,data,media,unity,mypackage.my_plugin` (bare names resolve to `app.plugins.<name>`).
Per-plugin options: `PLUGIN_CONFIG={"mydomain": {...}}`.

## Shared result envelope

Every task gets `result.json` (`GET /api/v1/tasks/{id}/result`):
- task id, domain, operation, status (incl. `partial`), summary
- artifacts (id, kind, filename, sha256, size, MIME, producer, step, validation status)
- validations (`GateResult`s), per-step evidence (executor, state, attempts, duration, error)
- failed and skipped steps, limitations, routing decision, next actions

## Testing a plugin

Unit-test `classify` / `plan` / validators directly, and run real workflows through `tests.conftest.build_orch` + `TaskRequest`. See `tests/test_platform.py` (all five plugins) and `tests/fixtures/slow_plugin.py` (a minimal plugin used by the recovery test).
