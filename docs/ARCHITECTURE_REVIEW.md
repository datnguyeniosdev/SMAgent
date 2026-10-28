# Architecture review: sessions, mains, lanes, routers, asks

Scope: `app/channel.py` (1540 lines, 91 functions), `app/asks.py`, `app/collaboration.py`,
`app/mcp_server.py`. Read-only review, 2026-10-28. Question: why do bugs keep appearing in this area?

Recent bugs it has to explain:
- a phantom session registered by `get_messages` without `cwd` (a folderless twin took the project's work);
- mains left "busy" because an ask answered through another path never closed its channel message
  (uncommitted fix in `app/asks.py` on develop);
- an ask resent after a gateway restart;
- Telegram's 4096-character limit (long replies; now split in `app/formatting.py`);
- `osascript` timeouts when opening or closing Terminal windows (`app/launcher.py`, 30 s, blocking).

## Root causes

### 1. State is split between memory and the store, with no single owner
- Memory: `ChannelHub.sessions` (each with its `queue`), `_stopping`, `_main_asked`, `LiveSession.busy_until`,
  `last_work`, `stalled`; `AskService.started`.
- Store: `channel_session`, `channel_msg`, `channel_main`, `channel_child`, `channel_model`, `agent_ask`, `human_input`, ...
- After a restart the memory half is gone and rebuilt from guesses: queued messages go back to `pending`
  (`ChannelHub.__init__`), sessions reappear only as they poll again, `_main_asked` is empty (the operator may be
  asked again). Every restart replays a slightly different history: **the resent ask is this class of bug.**

### 2. Two state machines for one piece of work
An ask has its own states (`agent_ask`: queued/delivered/escalated/answered/cancelled) and its request also
lives as a `channel_msg` (pending/queued/delivered/stalled/answered). They are kept in step by hand in each
code path (`reply_message`, `get_ask`, the operator's "answer it myself", cancel, escalation). Any path that
updates one and forgets the other leaves the session's lease open: **the "main stays busy" bug.** Human
questions (`human_input`) and reports/feedback are a third and fourth lifecycle on the same idea.

### 3. Many implicit rules choose "who is the main / who gets this"
`main_for` (50 lines) mixes: first session wins (app before CLI, claude_code before others, oldest),
twin matching by `cwd` + agent + kind (push session and its pull loop are "one main"), router sessions excluded
by `router_cwd`, children excluded by `channel_child[cwd]`, pending_new, router_failed, merge-back. Identity is
inferred from `(cwd, agent, client)` rather than given: **a poll without `cwd` became a phantom twin.**
Each fix adds one more rule; nobody can hold all of them in mind, and the operator cannot see why a session
was chosen. (The new `/main p# s#` pin and `/main auto off` give the operator an explicit override.)

### 4. Getters with side effects, called from everywhere
`main_for` is called 8 times in `channel.py` and may *write*: it sets a main, opens a router window
(osascript), merges a router back and flushes queues. It is called from `connect`, `pull`, `close`, `_route`,
`dispatch` and the flush itself (hence the `flush=False` guard: "may run inside a flush, a message queued
twice"). Order-dependent re-entrancy is where the hardest bugs come from.

### 5. Timers instead of explicit events
Nine durations interact: `PULL_TTL` 5m, `LEASE` 60m, `CHILD_IDLE` 30m, `BUSY` 30m, `START_WAIT` 10m,
`QUEUE_WAIT` 10m (channel), `UNREACHABLE` 2m, `RETRY` 5m, `HARD_CAP` 60m (asks), all evaluated by one 30 s
watchdog. Whether a session is "alive", "busy" or "stuck" is inferred from how recently it polled, so a slow
agent (a long build, a rate limit, a blocked osascript call) looks dead and triggers a router, a resend or an
escalation. External I/O (Telegram sends, osascript) also runs inline in some paths without one shared
limit/timeout policy.

## Simplification plan (in priority order)

1. **One lifecycle for work.** Make `channel_msg` the only record of a request; an ask becomes fields on it
   (`from_session`, `deadline`, `escalated_at`), not a parallel record. Closing a message (answered/cancelled,
   from any path) goes through one function that also releases the lease and notifies the asker.
   Removes bug class 2. Biggest win, medium effort (asks.py is 284 lines).
2. **Store is the source of truth; memory is a cache.** Persist the queue order, `busy_until`, `stalled` and
   `_main_asked` with the message/session rows; rebuild memory from the store on start and never "guess back".
   Make every outbound effect (resend, escalation) idempotent with a stored `sent_at`. Removes class 1.
3. **Explicit identity and an explicit main.** Require `cwd` (reject polls without it, as the latest fix does)
   and use a stable `session_id` the waiter keeps; drop twin heuristics where the id is known. Default the
   main to the operator's pin (`/main p# s#`, done in this change); keep the auto rule as one small,
   documented function used only when `/main auto on`.
4. **Pure lookups, one writer.** Split `main_for` into `main_of(project)` (read-only) and a single
   `reconcile(project)` run by the watchdog and on connect/close, which is the only place that assigns
   mains, opens routers or merges back. Removes the `flush=False` re-entrancy guard.
5. **Fewer timers, explicit heartbeats.** Treat `still_working` and the waiter's poll as the only liveness
   signals; collapse `BUSY`/`LEASE`/`HARD_CAP` into one "no heartbeat for N min" rule. Run osascript and
   Telegram sends in a background queue with one timeout/retry policy, so a slow window never blocks routing.

Suggested order: 1 and 3 first (they cover three of the five recent bugs), then 4, then 2 and 5.
Each step needs a regression test for the bug that motivated it (the tests in `tests/test_channel.py`
and `tests/test_asks.py` already cover most of the flows).

## Follow-up: unmanaged sessions and the cost of `/loop 1m`

### Why spawned sessions are left unmanaged
- **Only children have a lifecycle.** `_close_idle_children` closes a 🧭 child after `CHILD_IDLE` (30 min).
  Nothing does that for the main, the sessions opened with `/open`, or the temporary router outside its merge-back.
  A pull session that stops polling simply drops off `/list` after `PULL_TTL` (5 min),
  but its Claude process keeps running.
- **The gateway does not own the processes.** Only push sessions report a `pid`; for a pull session the gateway
  knows no process, so `/close` can only "tell its loop to stop at the next round" (`close`, `_stopping`). A loop
  that never polls again is never told, and `_stopping` is in memory, lost on restart.
- **Folders pile up.** Child worktrees are kept "for review" until `/children prune <lane>`; there is no expiry
  after the branch is merged or abandoned (here: `.agw-children/` holds `router`, `trust2-*`, `trusttest-*`,
  `progress-report-*`, and `.claude/worktrees/` an old agent worktree).
- **Identity is guessed** (root cause 3): the same window can show up as push + pull twins, a folderless twin,
  or a new session after a restart, so cleanup rules have to guess which records are the same process.

### Why `/loop 1m` burns tokens, and why it is still used
- Every cron firing is a full model turn: the whole conversation is sent again (mostly as cached input) plus a
  tool call and a reply, even when there is no work. That is 60 turns/hour, 1440/day per idle session, and the
  cost grows with the context. Rough estimate: a 50k-token context polled every minute reads ~3M cached input
  tokens per hour per idle session, before any real work.
- It is still used because it was the first pull mechanism (desktop app/IDE without channels), and
  `app/cli.py:508` still starts every fresh `agw session` with `/loop 1m <loop_prompt>` even though that session
  also has the push channel. The `agw wait` waiter (SessionStart hook) already replaces it at zero idle cost,
  but the old path was never removed (also mentioned in `app/mcp_server.py:544` and `app/channel.py:6`).

### Improvements (priority order)
1. **Drop `/loop 1m`.** `agw session` starts no loop (push delivers directly); pull sessions rely only on the
   `agw wait` waiter. Remove the loop wording from the tool description and docs. Biggest token saving, small change.
2. **One reaper for every session kind.** Every session registers a `pid` (the waiter reports its parent Claude
   process), and the watchdog closes any non-main session idle for N minutes (configurable, like CHILD_IDLE), with
   the same operator notice. Persist "closing" in the store, not in `_stopping`.
3. **Worktree expiry.** A closed child whose branch is merged (or idle for N days) is pruned automatically; the
   router folder is removed when the router closes.
4. **Stable identity** (plan step 3 above): one session id per Claude process, so cleanup never has to match twins.

## Proposed overall architecture (summary of the Telegram discussion, 2026-10-28)

```
Telegram ──► gateway (code, no model, store = source of truth)
               │  inbox · lanes · child state · routing rules · watchdog
               │
               ├─ new project work ──► 👑 main (cheap model, e.g. haiku): only picks the lane
               │                         └─ dispatch(lane) ─► gateway
               ├─ work for a lane / a child id / a reply to a child's report ──► 🧭 child directly
               └─ answers to a session's own question ──► that session (skips the line)

🧭 child (opus, own worktree + agw/ branch): does the work, answers with reply_message;
   gateway records start / still_working / done in the store and tells the operator.
```

Delivery (how a session is woken)
- Terminal sessions (`agw session`): **push** over the MCP channel, instant. Started with `--no-loop`.
- Desktop app / IDE / other tools: **`agw wait`** waiter (long-poll, 0 tokens idle), started by the
  SessionStart hook and restarted after each request; the UserPromptSubmit hook restarts it if it died.
- Safety net: one fixed 30-minute cron per pull session, created only when missing; it does nothing while
  the waiter is alive. `/loop 1m` is removed.

Roles and binding
- Main: chosen by the operator (`/main p# s#`, pinned) or automatically (`/main auto on`); it only routes.
  Children belong to the project's lanes in the store, not to a main, so changing the main keeps them.
- Temporary router (sonnet): opened only when the main is stuck; hands back when the main returns.
- Models per role: `/model p# main|child|router` (main role to be added); `dispatch(model=)` per task.

State and lifecycle
- Every session registers its pid; one watchdog reaper closes any non-main session idle for N min.
- One lifecycle per request (`channel_msg`); an ask is fields on it, not a second record.
- Worktrees of merged/abandoned children are pruned automatically.

What stays with the model vs. the gateway
- Gateway (code): delivery, tracking child state, forwarding child-addressed messages, timeouts, cleanup.
- Main model: only "which lane does this new request belong to" (and the brief).
- Child model: the work itself.

Order of work: drop `/loop 1m` + `--no-loop` for children → pinned main (done, uncommitted) → single request
lifecycle → reaper + pid → main model role → worktree expiry.
