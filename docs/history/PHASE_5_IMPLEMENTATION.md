# Phase 5 Implementation: Domain Plugins

Audit: [PHASE_5_AUDIT.md](PHASE_5_AUDIT.md). Contract: [PLUGIN_DEVELOPMENT.md](../PLUGIN_DEVELOPMENT.md).
Plugins:
- [software](../SOFTWARE_ENGINEERING_PLUGIN.md)
- [documents](../RESEARCH_DOCUMENTS_PLUGIN.md)
- [data](../DATA_PROCESSING_PLUGIN.md)
- [media](../MEDIA_PLUGIN.md)
- [unity](../UNITY_PLUGIN.md)

## Shared contracts

- **`GateResult`**: validator id, `passed/failed/unknown/skipped`, evidence refs, findings with severity, metrics, timestamp, error.
  - Mandatory validator steps block success.
  - A validator exception becomes `unknown`.
  - Plugins can't override a failed gate.
- **`ResultEnvelope`**: written as `result.json` for every task and served at `GET /api/v1/tasks/{id}/result`. Carries:
  - task id, domain, operation, status, summary
  - artifacts with provenance, validations, per-step evidence
  - failed/skipped steps, limitations, routing, next actions
- **Routing**: explicit domain, automatic classification (scores + reasons + all candidates recorded), model fallback only for read-only answers, clarification when ambiguous, `rejected` for inactive plugins. A task is never silently routed to an unrelated plugin.

## End-to-end results (real; see [DEMO_EVIDENCE.md](../DEMO_EVIDENCE.md))

| Example | Status | Evidence |
|---|---|---|
| A Software | **PASSED** | files created in an isolated worktree; real `pytest` exit 0; change report `files_match=true`; main checkout untouched. The independent review step is not run: no model/runtime |
| B Research/documents | **PASSED** | 2 Markdown docs extracted in parallel; facts cited `[S1, line 3]`; PDF listed as not processed; gate passed |
| C Data | **PASSED** | CSV aggregated by region; output re-parsed by the gate; metadata reproducible |
| D Media | inspect **PASSED**; generate **NOT CONFIGURED** | `missing capabilities ['image.generate', 'tool:media.generate']` |
| E Cross-domain | **PASSED** | documents report + `data.profile` of the CSV in one workflow |
| F Unity | inspect **PASSED** on a real project (read-only); editor **NOT AVAILABLE** | `missing capabilities ['tool:mcp.unity.editor_status']` (0 MCP tools without an Editor) |

## Tests

`tests/test_platform.py` (25) covers every plugin:
- manifest and permission enforcement
- routing
- real local workflows
- validator outcomes
- REST
- the core-independence check

Integration tests for external providers are opt-in and report `SKIPPED`/`NOT CONFIGURED`/`FAILED` honestly.
