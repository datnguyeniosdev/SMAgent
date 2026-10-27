# Phase 3 Implementation: Generic Platform

Audit: [PHASE_3_ARCHITECTURE_AUDIT.md](PHASE_3_ARCHITECTURE_AUDIT.md). Architecture: [GENERIC_PLATFORM_ARCHITECTURE.md](GENERIC_PLATFORM_ARCHITECTURE.md).

## Changes

| Area | Files | Result |
|---|---|---|
| Generic intake | `app/intake.py` | `TaskRequest` with constraints, preferences, output format, mode, direct tool, explicit workflow, deadlines, approval flag, idempotency key, correlation/parent ids; legacy workflows mapped to plugin task types |
| Classification and planning | `app/planner.py` | deterministic routing (explicit → classified → model fallback → clarification), explainable `RoutingDecision`, plan validation against registry and policy, constraints preserved. Model-assisted planning is **not** implemented (no verified model) |
| Capability registry | `app/capabilities.py` | manifests and status ladder with evidence and health checks; drives router availability |
| Workflow engine | `app/workflow.py`, `app/engine.py` | see [WORKFLOW_ENGINE.md](WORKFLOW_ENGINE.md) |
| Plugin system | `app/plugins/base.py` | manifests, permission ceiling, prefix rule, isolation, activate/deactivate |
| Plugins | `app/plugins/{software,documents,data,media,unity}.py` | see plugin docs |
| Unity extraction | removed from core: Unity prompt, design pipeline, asset dir, `UNITY_PATH` env | `tests/test_platform.py::test_core_has_no_domain_specific_dependencies` |
| Generic agent primitives | `app/workspace_agents.py` | implement / QA / independent review / fix loop, used by the software and unity plugins |
| Task service | `app/orchestrator.py` | rewritten around intake → plan → engine → gates → envelope; Phase 1 procedures removed from core |
| Artifacts | `app/storage.py`, `app/models.py` | MIME, producing step, producer, validation status, size limit, hash-checked reads, metadata persisted |
| Quality gates | `GateResult` + core validators (`core.artifacts_readable`, `core.json_schema`, `core.non_empty`) + plugin validators | only `passed` counts |
| Interfaces | `telegram_bot.py`, `main.py` | Auto/domain buttons, `/answer`, `/steps`, `/approvals`, `/plugins`, `/capabilities`; REST: generic task fields, cancel, answer, result envelope, workflow, events, approvals, tools, capabilities, plugins, artifact content |

**Removed from core:** `_wf_*` procedural workflows, `DESIGN_SCHEMA`, `DESIGNER_SYSTEM`, the Unity text in `IMPLEMENTER_SYSTEM`, and `ASSET_TARGET_DIR` (now `PLUGIN_CONFIG.unity.asset_dir`).

## Demonstrations

Three or more categories ran end to end through intake → planner → engine → validators → artifacts → envelope (all real, local tools):
- data transform/aggregate
- documents report
- software apply_files (real pytest)
- media inspect
- unity inspect of a real project
- cross-domain report

Media generation and the Unity editor show honest `unsupported` errors. See [DEMO_EVIDENCE.md](DEMO_EVIDENCE.md).
