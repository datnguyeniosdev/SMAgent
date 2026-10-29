# Game Intelligence Agent — Phase 0 audit

Status: **Phase 0 complete (audit only). No code, configuration, dependency or schema was changed.**
Repository: `local_agent_tool` at `develop` 62716ed (prod runs the same). Audited 2026-10-29.
Method: read-only inspection of `app/`, `config/`, `tests/`, `docs/`, `pyproject.toml`; Unity-MCP public docs
(release 1.5.0, 2026-10-24); local MCP configuration (names only). Line references are to `develop`.

## 0. Summary

The gateway already has most of the infrastructure the brief asks for: a durable DAG workflow engine with
per-step checkpoints, idempotency-aware recovery and bounded retries; an immutable, checksummed artifact
store; approvals and secure (passcode) operator confirmations over Telegram; a capability registry and an
MCP client that exposes allowlisted external tools as `mcp.<server>.<tool>`; workspace path confinement;
and a plugin framework. A first `game_research` plugin (645 lines, 15 tests plus an adapter contract test)
already covers inventory, evidence checksums, numeric-fact conflicts, per-level coverage, a runtime/playtest
*plan*, and a separate creative workspace.

**Recommendation:** build the Game Intelligence subsystem by **extending the `game_research` plugin and its
data model**, not by adding a second queue, session manager, run state machine or store. The missing parts
are domain-level (typed entities, claims with links and statuses, adapters with a real capability
contract, playtest runs, game/level models, reports), plus a few platform gaps (per-run budgets, store
migrations/versioning of record schemas, a Unity-MCP connection). Two operational findings need a decision
before Phase 1: the production retention is now **1 day** (it would delete research evidence), and
**auto-accept** can approve secure prompts, which must not count as permission to run game binaries.

## 1. Current architecture

```
 Telegram (operator)        Agents (Claude Code / Codex / OpenCode / Cursor / Antigravity)
      │  aiogram                    │ MCP (stdio proxy → HTTP /mcp)      │ agw wait (local HTTP)
      ▼                             ▼                                    ▼
 telegram_bot.py ◄──notifier── Orchestrator (orchestrator.py) ──── ChannelHub (channel.py) ─ AskService (asks.py)
      │                         │  intake → planner → engine → gates → report      │ sessions, mains/lanes,
      │ approvals / human input │                                                 │ leases, dispatch
      ▼                         ▼                                                 ▼
 collaboration.py         engine.py (DAG, retries, idempotency, recovery)    children: .agw-children/<lane>
 actions.py               workflow.py (WorkflowDef/StepDef/StepRecord/WorkflowRun)
                          plugins/ (software, documents, data, media, unity, game_research)
                          tools.py + builtin_tools.py + mcp_client.py (mcp.<server>.<tool>)
                          agent_registry.py + capabilities.py + capability_exec.py
                          connectors/ (claude_code, opencode, http providers, fal, meshy)
                                   │
                                   ▼
            Store (store.py, SQLite WAL: records / events / idempotency)
            ArtifactStore + AttachmentStore (storage.py, var/artifacts, var/media; sha256, create-only)
            WorkspaceManager (workspace.py, git worktree per task)
```

FastAPI (`main.py`) exposes `/health`, token-authenticated `/api/v1/*` (tasks, approvals, artifacts,
attachments, reports, human input, plugins, config, deployments), operator-token decision endpoints,
local-only `/api/v1/channel/*`, the Telegram webhook, and the MCP endpoint `/mcp`.

## 2. Module and interface inventory (relevant parts)

| Area | Module | What exists |
|---|---|---|
| Task model | `models.py:20-57,157-188` | `TaskState` (created … succeeded/failed/cancelled/unknown/expired) with `TRANSITIONS`; Task with parent/children, usage, history |
| Workflow model | `workflow.py` | `StepDef` (inputs refs, output_schema, timeout ≤7200 s, `RetryPolicy`, idempotent, requires_approval, on_failure, compensate_with); `WorkflowDef` (max_concurrency, timeout, `allow_partial`); `WorkflowRun` states incl. `partial`; `GateResult` |
| Engine | `engine.py` | DAG scheduling, per-attempt timeout, schema check of outputs, retries only for idempotent steps, `StepError(category, retryable, outcome=failed/timed_out/unknown)`, `prepare_recovery` (:167): idempotent → re-run, otherwise `unknown` |
| Orchestrator | `orchestrator.py` | `is_idempotent` (:1257), `needs_approval` (:1280), approvals (:1383/:1756), plugin op execution (:1551), `stop` (:676), `recover` (:2023), retention `purge_old` (:1883), `cleanup_artifacts` (:1835) |
| Persistence | `store.py` | `records(kind,id,task_id,run_id,state,body JSON,updated_at)`, `events`, `idempotency`; WAL, `synchronous=FULL`; no migrations (create-if-not-exists, JSON bodies) |
| Artifacts | `storage.py` | `<root>/<task_id>/<id>[_name]`, `open("xb")` create-only, sha256 verified on every read, symlinks refused, 50 MB artifact cap |
| Plugins | `plugins/base.py` | `DomainPlugin` (classify, plan, tools, operations, validators, idempotent_operations); trusted-module allowlist; per-plugin config (`PLUGIN_CONFIG`) |
| Tools / MCP | `tools.py`, `builtin_tools.py`, `mcp_client.py` | `resolve_in_workspace` (:22) confinement; MCP servers from settings or `config/mcp_servers/servers.yaml`; allowlisted tools only; non-read-only tools need `mcp:execute` and approval |
| Capabilities | `agent_registry.py`, `capabilities.py`, `capability_exec.py` | `CapabilityError` codes (unavailable, mcp_disconnected, forbidden …); status ladder declared → operational/degraded/unavailable; executor fallback |
| Human / Telegram | `collaboration.py`, `actions.py`, `telegram_bot.py` | approvals, `request_human_input` (secure = passcode), reminders, auto-accept, reports with Telegram feedback routed back |
| Logging | `logs.py` | JSON logs with correlation id, secret redaction, rotating `var/agw.log`; events table; **no metrics endpoint** |
| Tests | `tests/` | 410 collected (35 files + optional `tests/integration` gated by `RUN_INTEGRATION`/`RUN_PAID`); CI: `uv sync --locked`, ruff, pytest |
| Tooling | `pyproject.toml` | Python ≥3.12, `uv_build`, `uv.lock` present; deps: aiogram, cryptography, fastapi, httpx, jsonschema, mcp ≥2.3, pydantic-settings, pyyaml, uvicorn |

### Existing `game_research` plugin (`app/plugins/game_research.py`)

| Piece | What it does |
|---|---|
| `EngineAdapter` (:39) | data only: name, source/build marker globs, skip dirs, control-tool globs; built-ins `fixture`, `unity`; more via `PLUGIN_CONFIG` |
| `detect` (:79) | first adapter whose marker matches; source before build |
| tool `inventory` (:110) | sorted walk, symlinks/excludes skipped and counted, 5000-file cap, sha256 per file → `evidence_manifest.json` |
| tool `evidence` (:222) | diff against a baseline manifest; re-hash before read; regex numeric facts; conflict records keeping every claim with file/line/quote/sha256 |
| op `coverage` (:549) | merges transcribed captures per level: `?` unknown, `!` disputed (both readings kept); screenshots listed as untranscribed |
| op `runtime_plan` (:332) | capabilities present/missing; playtest mode automated / assisted / none |
| op `save_design` (:589) | creative workspace outside guarded roots, versioned files (`xb`), cited artifacts re-verified |
| Resume | engine checkpoints + idempotent ops; tested with a real `kill -9` (`tests/test_game_research.py:346`) |

## 3. Reuse / extend / create / deprecate

| Logical role (brief) | Decision | Where |
|---|---|---|
| AnalysisRunCoordinator | **Reuse** engine + Task/WorkflowRun; the run *is* a task. Extend with a run budget and a run summary record | `engine.py`, `orchestrator.py` |
| Analysis state machine | **Reuse** `TaskState`/`StepState`; map stages to steps (see §6). Do not add a parallel enum | `workflow.py` |
| AnalysisPlanner | **Extend** `GameResearchPlugin.plan()` to build the workflow from discovered capabilities | `game_research.py` |
| CapabilityRegistry | **Reuse** tool registry + capability status; add a per-adapter capability report (supported / unavailable / unverified) | `capabilities.py`, `tools.py` |
| GameAdapter | **Extend** `EngineAdapter` into a contract with declared capabilities, prerequisites, limits, version | `game_research.py` → new `app/plugins/game_intel/adapters.py` |
| RuntimeAdapter, InputControlAdapter | **Create** small protocols; implement via MCP tools (Unity-MCP) or a fake; no engine logic in core | plugin package |
| EvidenceRepository | **Extend**: evidence entries as records (`kind="game_evidence"`) pointing at ArtifactStore content | `store.py` kinds, `storage.py` |
| ArtifactStore | **Reuse** unchanged (immutable, checksummed) | `storage.py` |
| ObservationService, ClaimService (+links) | **Create** typed models + record kinds; replace untyped fact dicts | plugin package |
| CoverageTracker | **Extend** `level_coverage` into coverage per mechanic/level/capability | `game_research.py` |
| GameModelBuilder, LevelReconstructor | **Create** (graph / grid / spatial / hybrid level models; exact vs measured vs conceptual geometry) | plugin package |
| ReportGenerator | **Create** a small exporter (Markdown + JSON, schema-validated) replacing inline markdown | plugin package |
| CreativeDesignService | **Extend** `save_design` with typed `DesignProposal`, its own versions, links to abstract principles/claims | `game_research.py` |
| GameProjectService (+ GameBuild) | **Create** minimal registry: project id, roots, builds with fingerprints; record kinds | plugin package |
| Permissions / human checks | **Reuse** approvals + secure human input; add an explicit `game.execute` permission | `config.py`, `gateway_config.py` |
| Second queue/session manager/vector DB | **Do not create** | — |
| `EngineAdapter` glob-only `control_tools` | **Deprecate** after the contract lands (kept as a compatibility path) | `game_research.py` |

## 4. Data flow and persistence

Today: request → `plan()` → inventory (tool) → evidence/coverage/runtime (parallel) → artifacts
(`evidence_manifest.json`, `evidence_check.json`, `coverage.*`, `runtime_plan.md`) on the task. Step records
and events give resume and audit; artifacts are content-addressed by sha256 and create-only.

Proposed (additive): every entity is a JSON record kind keyed by its id and `task_id`/`run_id`, large
content stays in ArtifactStore and is referenced by artifact id + sha256:

| Entity | Storage | Notes |
|---|---|---|
| GameProject, GameBuild | records `game_project`, `game_build` | build fingerprint = sha256 of manifest |
| AnalysisRun | existing `task` + `run`; summary record `game_run` | budgets, plan, capability report, limitations |
| EvidenceArtifact | record `game_evidence` + ArtifactStore file | type, method, source revision/fingerprint, adapter+version, status, limitations; immutable, corrections are new versions with `derived_from` |
| Observation | record `game_observation` | what was directly seen, evidence ids required |
| Claim, ClaimEvidenceLink | records `game_claim`, `game_claim_link` | status machine below; links `supports` / `contradicts` |
| PlaytestRun | record `game_playtest` + artifacts | mode, actions, timeline, outcome |
| GameModel, LevelModel | artifacts (versioned JSON, schema-validated) + record index | exact / measured / conceptual geometry flags |
| DesignProposal | creative root files (existing) + record `game_design` | separate namespace and versions |
| ValidationResult | existing `GateResult` in the run + record `game_validation` | |

Claim statuses: `hypothesis → supported` (≥1 supporting evidence link, none contradicting),
`supported → verified` (an independent method or a controlled experiment confirms it),
`any → contradicted` (a contradicting link that is not refuted), `any → unresolved` (conflict with no
decisive evidence or budget exhausted). Confidence is a documented ordinal (evidence count and method
kinds), never presented as a probability.

Schema versioning: the store has no migrations; new record bodies carry `schema_version` and readers
accept older versions. No relational schema change is needed.

## 5. Unity and other engines (capability discovery)

Unity-MCP ("AI Game Developer", `com.ivanmurzak.unity.mcp`, release 1.5.0, Apache-2.0) exposes about 70
tools; relevant ones, by requested capability:

| Capability | Unity-MCP | Status |
|---|---|---|
| inspect_project, list_assets | `package-list`, `assets-find`, `assets-get-data` | available |
| read_source | `script-read` | available |
| inspect_scene, inspect_level_data | `scene-get-data`, `scene-list-opened`, `gameobject-find`, `gameobject-component-get`, `object-get-data` | available |
| launch_game / stop_game | `editor-application-set-state` (play mode) | available (Editor play mode, not a shipped build) |
| query_runtime_state | `editor-application-get-state`, `gameobject-component-get` in play mode | available |
| capture_screenshot | `screenshot-game-view`, `screenshot-camera`, `screenshot-scene-view` | available |
| read_logs | `console-get-logs` | available |
| run_build_or_test | `tests-run` (EditMode/PlayMode) | tests available; build only via `script-execute` (arbitrary code) |
| send_input | none in core; InputSystem extension or `script-execute` | partial |
| read_telemetry | `rql-query` (profiler) | partial |
| capture_video, collect_crash_report | — | not provided |

**On this machine it is not usable yet:** no Unity project has the package (FlappyBird and `unity` are
6000.4.0f1, learnD1 6000.3.24f1); `config/mcp_servers/servers.yaml` has a disabled `unity` placeholder
whose allowlisted names (`editor_status`, `get_scene_hierarchy`) are not Unity-MCP tool names; the only
`ai-game-developer` entry (in `~/.claude.json`) points at a deleted project. Tools that change or execute
(`script-execute`, `script-update-or-create`, `assets-delete`, `reflection-method-call`) must stay off the
allowlist or require approval. Other engines (Godot, Unreal, web builds): no MCP or runtime tooling is
configured; only the generic artifact adapter applies until a tool is discovered.

## 6. Workflow mapping (brief stages → existing machinery)

| Brief stage | Gateway mechanism |
|---|---|
| INTAKE | task `created/queued`; request schema via `OperationSpec.input_schema` |
| PERMISSION_CHECK | approval step (`requires_approval`) or secure human input when execution/real binaries are involved |
| CAPABILITY_DISCOVERY, PLAN_CREATED | `planning`: discover step (idempotent) + `plan()` builds the DAG from capabilities |
| COLLECTING_EVIDENCE … GENERATING_REPORTS | steps (idempotent where pure; collection with idempotency keys = evidence sha256) |
| CREATIVE_DESIGN | separate `design` task type (exists) |
| COMPLETED / PARTIAL / FAILED / CANCELLED | `succeeded` / WorkflowRun `partial` (`allow_partial`) / `failed` / `cancelled`; `unknown` for interrupted side effects |

Budgets: add a per-run budget (steps, wall time, playtest attempts, tool calls) checked by the plugin;
the engine already bounds retries and timeouts.

## 7. Risk register

| # | Risk | Impact | Mitigation |
|---|---|---|---|
| R1 | **Retention is 1 day in prod** (`DATA_RETENTION_DAYS=1`) and purges finished tasks, records and artifacts | research evidence deleted the next day | exempt `game_*` kinds and their artifacts, or a separate retention for research; decide before Phase 1 |
| R2 | **Auto-accept** answers secure prompts automatically | a "permission" to run a game binary could be granted with nobody present | `game.execute` approvals refuse auto-accept answers |
| R3 | Unity-MCP not installed; placeholder config wrong | Phase 4/5 Unity work blocked | install in one chosen project with the operator; fix allowlist to real tool names; keep fakes in CI |
| R4 | Unity-MCP `script-execute` / reflection run arbitrary code | uncontrolled side effects | never allowlist without approval; prefer read-only tools |
| R5 | Running untrusted builds | security | no execution without sandbox + explicit approval; assisted mode otherwise |
| R6 | No store migrations; JSON bodies | schema drift | `schema_version` in every new body; tolerant readers; tests over old bodies |
| R7 | 50 MB artifact cap | video capture fails | chunking or a raised cap for `video` type, decided when capture exists (none today) |
| R8 | Non-atomic hash-then-read; symlinked dir components; MCP path guard only checks listed argument keys | evidence integrity / path escape | re-hash after read and compare; reject symlinked components; extend `path_args` for game tools |
| R9 | No per-task budget; no metrics endpoint | runaway runs, little observability | plugin-level budget; counts in events and run summary |
| R10 | Claims from regex facts keyed by name only | false conflicts between levels | scope keys (level/entity) in the Claim model |
| R11 | Copyright / similarity of creative output | legal | provenance + similarity review step; never "legally cleared" |
| R12 | Store reads not locked (writes are) | rare torn reads under concurrency | single-writer per run; idempotent steps |

## 8. Implementation plan (phases, files, acceptance tests)

All phases add code under a new package `app/plugins/game_intel/` (or grow `game_research.py` into it),
register through the existing plugin loader, use `uv run pytest` / `uv run ruff`, and add no dependency.

| Phase | Deliverables | Acceptance tests |
|---|---|---|
| 1 Schemas + evidence | pydantic models (GameProject, GameBuild, EvidenceArtifact, Observation, Claim, ClaimEvidenceLink, PlaytestRun, DesignProposal, ValidationResult) with `schema_version`; EvidenceRepository over `Store` + `ArtifactStore`; claim status machine; retention exemption (R1) | serialization round-trip; checksum + provenance; evidence immutability (correction = new version); claim transitions; observation/claim separation |
| 2 Adapter contract | `GameAdapter` capability declaration (supported/unavailable/unverified, prerequisites, limits, version); generic artifact adapter (from today's inventory); capability report | contract tests for supported/unsupported, missing tool, malformed output, version change, timeout/cancel with a mock adapter |
| 3 Orchestration | planner builds the DAG from capabilities; run budget; idempotency keys; partial completion with limitations; cancellation/cleanup | full mock run; resume after `kill -9`; idempotent retry; partial report when capabilities missing; cross-run isolation |
| 4 One real adapter | Unity via Unity-MCP read-only tools (if installed in a chosen project) **or** a source-tree adapter for an existing repo; metadata extraction with provenance | optional `RUN_INTEGRATION` test against the real engine; fixture tests in CI |
| 5 Playtesting | playtest abstraction (instrumented / visual / assisted), fake game + fake capture adapters, outcome success/failure/timeout/crash/unknown, targeted experiments | deterministic action sequences, artifact registration, budget and retry bounds |
| 6 Models + reports | GameModel, LevelModel (graph/grid/spatial/hybrid; exact/measured/conceptual), coverage, conflicts, Markdown + JSON export validated against JSON Schema | schema validation of exports; no invented geometry from partial captures |
| 7 Creative separation | DesignProposal records + versions in the creative namespace; traceability to abstract principles/claims; similarity-review step | proposals never stored as observations; versions independent |
| 8 Hardening | permissions (`game.execute`, refuse auto-accept), path guard fixes (R8), logging/events, docs (install, config, adapter development, troubleshooting), rollback | security tests (path traversal, symlinks, untrusted binary refused), regression suite |

Migration: none relational; new record kinds and artifact types only. Rollback: disable the plugin in
`PLUGIN_CONFIG`/`plugins`; records of the new kinds are inert.

## 9. Decisions needed before Phase 1

1. Retention for research evidence (R1): exempt `game_*` data, or a separate retention period?
2. Should `game.execute` permissions refuse auto-accept answers (R2)? Recommended: yes.
3. Phase 4 target: install Unity-MCP into one Unity project (which one: FlappyBird, `unity`, or learnD1),
   or start with a source-tree adapter for an existing game repo (e.g. a project under Working)?
4. Package layout: grow `game_research.py` in place, or split into `app/plugins/game_intel/`?
   Recommended: split, keeping `game_research` as the plugin id so existing tasks and tests keep working.
