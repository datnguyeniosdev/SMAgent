# Phase 3 Architecture Audit (before migration, 2026-10-20)

Inputs: Phase 1 code (commit `adf75dd`), Phase 2 work in progress (tool registry `app/tools.py`, built-in tools `app/builtin_tools.py`, QA runner rewrite, `mcp` 2.3.0 dependency), and the environment findings in [PHASE_2_AUDIT.md](PHASE_2_AUDIT.md).

## Component map → target layer

| Existing component | Target layer | Verdict |
|---|---|---|
| `telegram_bot.py` | A. Interface | Keep. It calls `Orchestrator.submit(description, Workflow)` with a fixed enum of 5 workflows, so it must move to a generic `TaskRequest` |
| `main.py` REST | A. Interface | Keep. `TaskIn` has the same `Workflow` enum coupling |
| `models.Task` | B. Intake | Needs objective, domain/task_type, constraints, output format, mode, idempotency key. States lack `planning`, `validating` |
| — | C. Planner | **Missing**: workflow choice is a hard-coded enum chosen by the user |
| `router.py` + `connectors.base.Cap` | D. Capability registry | Partial: capability sets per connector, but no status lifecycle, health checks or evidence. `available` is a config boolean |
| `connectors/claude_code.py` | E. Agent runtime | Keep (adapter). Missing health check and status feedback |
| `connectors/http_providers.py` | F. Model provider | Keep (adapters). Tool calls are returned but never executed; there's no multi-turn API |
| `tools.py`, `builtin_tools.py`, `qa.py` | G. Tool subsystem | Phase 2 work; keep and extend (MCP, model tool loop) |
| `orchestrator.py` | H. Workflow engine + service | **Coupled**: workflows are procedural methods `_wf_code`, `_wf_delegate_review`, `_wf_parallel`, `_wf_design_implement_review`. No DAG, conditions, step retries or output contracts |
| — | I. Plugins | **Missing** |
| `storage.ArtifactStore` | J. Artifacts | Partial: id, kind, hash, size, task. Missing producing step, MIME type, validation status, content retrieval API |
| `qa.py`, `orchestrator._review` | K. Quality gates | Partial and coding-only. No generic success criteria |

## Domain-specific assumptions found in the core

| Location | Assumption | Action |
|---|---|---|
| `orchestrator.IMPLEMENTER_SYSTEM` | Mentions Unity-MCP | Move the Unity text to the Unity plugin's system prompt |
| `orchestrator._wf_design_implement_review`, `_generate_assets`, `DESIGN_SCHEMA`, `DESIGNER_SYSTEM` | Unity-style asset pipeline in core | Move to the Unity plugin |
| `config.asset_target_dir` (`Assets/Generated`) | Unity folder layout in core config | Move to `PLUGIN_CONFIG.unity.asset_dir` |
| `orchestrator._wf_code/_wf_delegate_review/_wf_parallel` | Coding workflows hard-wired | Become Software plugin task types. Generic "agent edits an isolated workspace, then independent review" helpers stay in core (`workspace_agents.py`) |
| `models.Workflow` enum, `WRITES_WORKSPACE` | The set of task kinds is closed | Keep only as a legacy alias mapped to `domain/task_type` |
| `AgentProfile` roles designer/implementer/reviewer | Coding-team roles | Keep as generic role preferences |
| `assets.py` | PNG/alpha rules (Unity-flavoured) | Image inspection is generic media validation; it stays in core and is reused by the Media and Unity plugins |

## Runtime reality (re-verified for this phase)

- Claude Code CLI 2.1.71: invocations fail with 401 (invalid stored login). `claude mcp serve` works without auth: 19 tools.
- `unity mcp` stdio server responds (`unity-mcp` 1.0.0-beta.12) with 0 tools: no Editor running. Unity-MCP HTTP (`localhost:26609`) is not listening.
- No OpenAI/DeepSeek/Gemini keys. No image provider. OpenCode is not installed.
- Telegram token valid (`@darren_connector_bot`). Group/channel IDs are placeholders and the bot has no updates.
- State is in memory only. There's no persistence for tasks, steps, records or approvals.

## Migration decisions

1. Keep `Orchestrator` as the **task service** (generic boundary used by Telegram/REST). Don't create a second engine:
   the procedural workflows become *procedure steps* owned by plugins and run by the new plan-based `WorkflowEngine`.
2. One capability registry for connectors, tools, MCP tools and plugins. `Router` keeps capability matching, but availability now comes from registry status.
3. The planner is deterministic (plugin classifiers + explicit constraints). Model-assisted planning is left as a hook: no model credential is available to verify it.
4. Persistence: in-memory state plus an append-only JSONL event log (`DATA_DIR/events.jsonl`) for observability. **No restart recovery** is claimed.
5. Characterization tests: the 69 existing tests stay green across the migration (legacy `Workflow` values map to plugin task types).
