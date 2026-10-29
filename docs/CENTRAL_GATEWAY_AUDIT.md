# Central Gateway Agent refactor — Phase 0 audit

Status: **Phase 0 complete (audit only). No code, config or schema changed for this refactor.**
Repository `local_agent_tool`, `develop` 62716ed (= prod). Audited 2026-10-29 by reading `app/`, `config/`,
`tests/`, the prod store (counts only) and the agent setup on this machine. Line numbers refer to `develop`.
(Separate, still uncommitted: Game Intelligence Phase 1 in `app/plugins/game_intel/`, paused by the operator.)

## 0. Summary

The gateway already has two orchestration planes:

1. **Task plane** (`Orchestrator` + `engine.py`): durable tasks, DAG workflows, idempotent recovery,
   approvals, deterministic routing (`planner.route`, `config/routing/routing.yaml`, `dynamic_planning:
   false`), capability/agent registry, headless connectors (Claude Code, OpenCode, HTTP providers).
2. **Session plane** (`ChannelHub` + `AskService`): live agent conversations (Claude Code app/CLI, Codex,
   Cursor, Antigravity) fed through `get_messages`/push, with **one pinned "main" conversation per project**
   that only routes (it calls `dispatch` to open or reuse lane sessions). 11 of 14 projects have a pinned
   main; 47 session records; 33 lanes; auto-main is off.

The brief's "one Gateway Service + one Gateway Agent" maps onto this as: **make the service route
deterministically to lanes itself** (what each per-project main does today by LLM), and keep **one**
LLM session (the Gateway Agent) only for requests the rules cannot place. Most infrastructure is reusable;
the gaps are a project registry with stable ids, a versioned skill registry with recorded prompt
assembly, a durable event outbox/subscriptions, single-instance/lease fencing, and cross-session workflow
chaining. Smallest safe path: a routing mode switch (`main` → `central`) per project, so projects move one
at a time and can roll back.

## 1. Architecture map (today)

```
 Telegram ─► telegram_bot ─┐                       ┌─► Orchestrator ─► planner.route (rules) ─► engine (DAG)
                           │  /send, replies, asks │        │                                   │
 MCP tools (agents) ───────┼──────────────────────►│        └─► connectors (claude -p, opencode, http)
 /api/v1 (token) ──────────┘                       │
                                                   └─► ChannelHub.send(project, text)
                                                          │ _route: project → its pinned MAIN (route note)
                                                          ▼
                                     per-project MAIN conversation (LLM, hook-blocked from work)
                                                          │ dispatch(lane, brief)
                                                          ▼
                                     lane session in .agw-children/<lane>  (worker; replies to sender)
 Waiters (agw wait), Codex relay, Cursor stop hook ◄── long-poll /api/v1/channel/wait
 Store: SQLite records/events/idempotency · ArtifactStore · Telegram (aiogram polling/webhook)
```

Current request flow for project work: Telegram/agent → `send` → `_route` → project main (LLM turn) →
`dispatch` → lane child (new conversation or same lane) → `reply_message` → Telegram/asker.

## 2. Component inventory and classification

| Component | Where | Class | Note |
|---|---|---|---|
| SQLite store (records, events, idempotency; WAL) | `store.py` | reusable as-is | single process lock |
| Task model + state machine, DAG engine, recovery | `models.py`, `workflow.py`, `engine.py`, `orchestrator.py:2023` | reusable as-is | the durable control plane for headless work |
| Deterministic routing (rules → keywords → default) | `planner.py:90-170`, `config/routing/routing.yaml` | reusable as-is | LLM planning disabled |
| Agent/capability registry, executor selection | `agent_registry.py`, `capabilities.py`, `config/agents/agents.yaml`, `config/capabilities/*` | reusable as-is | for task plane; not used by session plane |
| Approvals, secure human input, reminders | `collaboration.py`, `actions.py` | reusable as-is | |
| ArtifactStore, workspaces, child worktrees/copies | `storage.py`, `workspace.py`, `channel.py:_worktree` | reusable as-is | |
| Lane mechanics (open/reuse/reopen child, carry rules/skills, fetch back, overlap report) | `channel.py:884-1000`, `_carry` | reusable with modification | callable only by a main session today |
| Session registry | `channel_session` records + in-memory `LiveSession` | reusable with modification | queues in memory |
| Ask service + partner pairing | `asks.py` | reusable with modification | routes through the target's main |
| Per-project main routing (`main_for`, pins, #Main, router takeover, PreToolUse block) | `channel.py:526-700`, `cli.py:632` | **conflicting / to replace** | an LLM per project doing deterministic work |
| Project identity | `projects.py:40-76` (README name), keys `_norm(name)`, `channel_pnum` | reusable with modification | name-keyed: a rename splits a project; no `project_id` |
| Event log | `store.event` | reusable with modification | append-only; no `event_id` dedupe, subscriptions, replay or delivery status |
| Telegram delivery | `telegram_bot._send` (3 tries, :306) | **unreliable** | no outbox; failures mostly only logged; duplicate on timeout |
| Incoming Telegram dedupe | `telegram_update` records | partial | only 2 handlers check it |
| Background loops (30 s watchdog: retention, asks, reminders, leases) | `main.py:308-326` | reusable as-is | cursors in memory / timestamps |
| Client polling (waiter long-poll, 30-min safety cron, Codex relay 2 s, Cursor hold) | `waiter.py`, `repo_setup.py:222`, `codex_relay.py`, `cursor_hooks.py` | redundant in part | cron duplicates the waiter by design |
| Instructions: CLAUDE.md block, global skill, AGENTS.md/GEMINI.md, hooks, appended notes, `core_skills` | `repo_setup.py`, `global_setup.py`, `agents_setup.py`, `cli.py`, `channel.py` | reusable with modification | **no versions, no record of what a task received** |
| Connector system prompts | `capability_exec.py`, `workspace_agents.py`; `--append-system-prompt` | reusable as-is | only headless runtimes take a dynamic system prompt |
| Leases/fencing/leader election | — | **missing** | only a port probe in `agw dev` (`cli.py:254`) |
| Workflow chaining across live agents | — | **missing** | DAG exists only for headless tasks |
| Progress aggregation | — | missing | every report is its own message |
| Auto-accept answering secure prompts | `collaboration.py:827-856` | **unsafe** for approvals of external side effects | |
| Late reply from an expired lease holder | `channel.py:1208` | unreliable | not fenced |

## 3. Runtime capabilities (prompt injection)

| Runtime | Dynamic system prompt | Instruction channels today |
|---|---|---|
| Claude Code headless (`claude -p`) | yes (`--append-system-prompt`) | connector request |
| Claude Code interactive / desktop | no | CLAUDE.md block, global skill, hooks (SessionStart, UserPromptSubmit, PreToolUse), first prompt, message text |
| OpenCode | no (text prepended) | AGENTS.md, message text |
| Codex | no | AGENTS.md, hook `additionalContext`, queued prompt |
| Cursor | no | hook `additional_context`, `followup_message` |
| Antigravity | no | GEMINI.md, waiter notify text |

Consequence: for live sessions, "injection" can only be **per-message context** (skill references + task
context in the delivered message, which is already where `skills_note` and the routing note go) plus
static files. The design must record what was delivered rather than assume system-prompt control.

## 4. Data flow and persistence findings

- Project state is keyed by normalized display name (`channel_main`, `channel_pnum`, `channel_model`).
- Messages (`channel_msg`) are the only durable record of session-plane work; text holds the appended
  instructions; no task/workflow id ties a lane job to an Orchestrator task.
- Events carry `seq`, `ts`, `type`, `task_id`, `run_id`, `step_id`, `correlation_id`, body; nobody consumes
  them except the GET API.
- Retention is 1 day in prod and deletes finished messages/events/tasks (research tasks excepted in the
  uncommitted Game Intelligence work).

## 5. Risk register

| # | Risk | Impact | Mitigation in plan |
|---|---|---|---|
| R1 | Removing per-project mains breaks 11 live projects at once | work stops | per-project `routing_mode` (`main`/`central`), migrate one project at a time, rollback = flip back |
| R2 | Deterministic rules misroute (no LLM reading the request) | wrong lane | rules only for explicit cases (lane named, reply to a report/lane, ask partner, task type); else Gateway Agent |
| R3 | Single Gateway Agent becomes a bottleneck or stuck | routing delays | it only handles unmatched requests; watchdog + standby runtime (Phase 5) |
| R4 | No fencing; two gateway processes on one DB | double dispatch | single-instance lease row with epoch in the store; refuse a second instance |
| R5 | Name-keyed projects; rename splits state | lost routing | stable `project_id` with name aliases |
| R6 | Instruction drift (unversioned SECTION/skills) | irreproducible behaviour | skill registry with content hashes; record per message |
| R7 | Telegram failures lost | missed approvals/results | outbox records + retry in the watchdog |
| R8 | Prompt injection via repo/ask/Telegram content | policy bypass | keep stable instructions (files/hooks) separate; deliver task data fenced as data |
| R9 | Auto-accept approving risky actions | unattended side effects | exclude external side effects from auto-accept |
| R10 | Live-session queues in memory | lost queue order on restart | already re-pended from records; keep, document |
| R11 | Agent-to-agent loops | runaway cost | hop limit + per-workflow budget |

## 6. Migration plan (smallest safe path)

| Phase | What | Reuses | Tests |
|---|---|---|---|
| 1 Contracts + registries | `project` registry (stable `project_id`, aliases, scope config, routing mode); session records gain `project_id`, `agent_id`, health; message/task envelope fields (`workflow_id`, `correlation_id`, `skill_refs`, `idempotency_key`, priority, deadline) — additive JSON fields | store records, `channel_pnum` | multi-project isolation; rename keeps state |
| 2 Central routing | `RoutingTable`: explicit lane → reply-to-lane → ask partner → task-type/lane rules (`config/routing/channel.yaml`) → Gateway Agent; service calls the existing lane dispatch itself; `routing_mode=central` per project; main path unchanged for `main` projects | `dispatch`/lane code, planner rule style | correct lane per rule; fallback to Gateway Agent; compatibility of `main` mode |
| 3 Skills + prompt assembly | `config/skills/*.yaml` (id, version = content hash, scope, triggers, capabilities, deps, conflicts, allowed agents, prompt_source path); eligibility filter → deterministic order; `PromptAssembly` record per delivery (layers L0–L5 as references, skill versions, omitted content); per-runtime adapter (headless: system prompt; live: message context) | `core_skills`, `skills_note`, `_carry` | version pinning, deps/conflicts, missing skill, runtime without system prompt, no duplicate injection |
| 4 Events + notifications | `event_id` + dedupe on ingest; subscriptions (task/session/workflow); Telegram outbox with delivery status and retry; progress aggregation | events table, watchdog | duplicates, out-of-order, retry/replay, aggregation |
| 5 Scheduler + recovery | instance lease row (epoch, fencing on dispatch/reply writes); persisted loop cursors; Gateway Agent watchdog + standby; stale-reply fencing | watchdog loop | lease expiry, concurrent coordinator refused, recovery without restarting workers |
| 6 Workflow chaining | session-plane workflows: config-defined stages (e.g. PO → Art → Code → QA) as linked lane messages with dependencies, conditions, idempotent creation, approval gates, hop limits | engine concepts, asks pairing | loops, cancellation, deadlines, downstream idempotency |
| 7 Migration + cleanup | move projects to `central` one by one; then retire pins/#Main/router takeover/PreToolUse main block once no project uses `main` | — | no remaining consumers check |
| 8 Validation | e2e, recovery, security, concurrency; deploy/rollback/monitoring docs | — | full suite |

## 7. Decisions needed before Phase 1

1. **Which session is the Gateway Agent?** Recommended: the existing "Gate Main" conversation (Agent
   Gateway project), with a standby runtime chosen in Phase 5.
2. **Rollout:** per-project `routing_mode` and migrate one pilot project first (suggest UIUXAgent or
   Mochi Sky), keeping the others on `main` until the pilot works.
3. **Routing rules source:** a new `config/routing/channel.yaml` next to the existing `routing.yaml`
   (same config system), or Telegram commands (`/route`)? Recommended: YAML plus `/route` to view.
4. **Skill registry source:** YAML in `config/skills/` pointing at existing SKILL.md files (version =
   content hash), covering project skills in each repo's `.claude/skills/`. Recommended.
5. **Paused Game Intelligence Phase 1** (uncommitted): commit it first, park it, or discard it?
