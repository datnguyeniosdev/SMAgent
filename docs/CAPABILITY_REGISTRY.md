# Capability Registry

`app/capabilities.py`. One `CapabilityManifest` per agent, model provider, image provider, tool, MCP server and plugin.
Browse it with `GET /api/v1/capabilities` (`?refresh=true` re-runs health checks) or `/capabilities` in Telegram.

## Manifest fields

`id` (stable, `<kind>:<name>`, e.g. `tool:data.transform`) · `kind` · `name` · `description` · `category` · `operations` · `provides` (abstract capabilities such as `model.text`, `model.vision`, `model.structured`, `workspace.write`, `image.generate`, `tool:<name>`) · `input_schema` / `output_schema` · `backend` · `artifact_types` · `permissions` · `side_effects` · `timeout_s` · `config_requirements` · `version` · `status` · `evidence` · `checked_at` · `last_error`.

## Status ladder

| Status | Meaning | How it is reached |
|---|---|---|
| declared | code exists | default |
| discovered | seen at runtime but not enabled | MCP `tools/list` returned it, nothing allowlisted (e.g. `mcp_server:unity` with 0 tools) |
| configured | configuration present, no check yet | image provider with key + model |
| validated | a free real check passed | `claude --version` ran; `GET /models` = 200; tool registered with a valid schema; MCP tool allowlisted and exposed |
| operational | a real execution succeeded in this process | `record_execution(ok=True)` after a real tool/agent/plugin run |
| degraded | a real execution failed after validation | e.g. `agent:claude_code` after the 401 |
| unavailable | not configured or check failed | missing key, disabled runtime, broken health check |

Only **validated, operational, degraded** are routable. A health check can never overwrite `operational` with `validated`. A broken health check marks only its own capability unavailable.

## Routing

`Planner.has(capability)`:
- `tool:<name>` → the tool is registered **and** its manifest is routable.
- An abstract capability (`model.text` …) → at least one routable manifest `provides` it.

Required capabilities are checked twice:
1. At plan time, from the plugin's `OperationSpec.required_capabilities`.
2. At validation time, for every step.

A missing capability fails the task with `error_category = unsupported` and names the capability, e.g. `missing capabilities ['image.generate', 'tool:media.generate']`. Nothing is silently downgraded.

## Current state on this machine

See [CAPABILITY_MATRIX.md](CAPABILITY_MATRIX.md) and the run-time table in [DEMO_EVIDENCE.md](DEMO_EVIDENCE.md).
Status is **in-process**: evidence is rebuilt on each start (health checks + real executions). Recording it durably is future work.
