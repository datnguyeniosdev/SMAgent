# Design: one main session per project, child sessions for parallel work

Status: agreed with the operator on Telegram and built, 2026-10-27 (`app/channel.py` `dispatch`).

## Problem seen in prod (2026-10-27)

- **Answers wait behind the job that asked for them.** An agent asks a question mid-task. The
  operator's answer is queued behind that same task, so `get_messages` hands it nothing. After the
  60-minute lease the task is marked stuck and the agent looks "closed mid-way".
- **Work piles onto one session.** After a restart, every waiting message of a project went to the
  first session that reconnected (5 Glazed Gems messages sat in a Codex session that never pulled).
- **Nobody decides what can run in parallel.** Sessions take whatever they are handed.

The uncommitted routing fix on `develop` (`app/channel.py`) solves the first point and spreads work
over idle sessions. This design replaces the spreading with a decision made by an agent.

## Proposal

1. **Messages go to a project, not to a session.** `/send p#`, a reply on Telegram and `ask_agent`
   to a project all land in the project's **main session**. `/send s#` stays available to address
   one session directly.
2. **One main session per project.** It is the session the operator marks with `/main s#`. By
   default it is the oldest open app session of the project. The main session's agent does work
   typed into it directly. Requests that arrive through the gateway it only routes; it never does
   them itself.
3. **The main agent dispatches** with a new MCP tool:
   `dispatch(message_id, to="s#" | "new", lane="<task type>", brief="...")`.
   - `s#`: hand it to an existing child session, normally the one already on that lane.
   - `new`: the gateway opens a new child session with the main session's tool and the project id,
     in its own copy-on-write copy of the project (`<repo>/.agw-children/`) and branch, and pushes
     the brief into it.
4. **Lanes.** Each child session has one lane (a task type the main agent chooses: `ui`, `build`,
   `art`, `bugfix:<area>`...). Work on the same lane queues in that child, one job after another.
   Work on a different lane gets its own child and runs in parallel.
5. **Children keep today's session logic and answer the sender directly.** The dispatched message keeps its `message_id` and
   `ask_id`, so the child's `reply_message` goes straight to Telegram or to the agent that asked.
   The main session gets a one-line note ("m12 done by s7 (ui)") without being woken.
6. **Cleanup.** A child with an empty lane is closed after 30 minutes idle (`/close`, which exists
   today). Questions and approvals asked by a child use the same Telegram rules as any session.
7. **Answers always skip the queue**, in every session. This part of the uncommitted fix stays.

## What exists already

- Opening a session in a Terminal window: `app/launcher.py` `open_in_terminal`, used by `/open`.
- Push delivery into a CLI session: `agw session` plus the channel shim.
- Closing a session: `ChannelHub.close`. Stuck work handling: `stall`, `resume`, `active`.

## What was built

- A `main` flag per project (stored like `enabled`), `/main s#`, and routing of project messages to it.
- The `dispatch` tool, a `lane` and `parent` on child sessions, and a way to bind a child that is
  still starting to the brief waiting for it (a one-time token passed to `agw session`).
- A copy-on-write copy and branch per child (git worktree where cloning fails), reopened for its
  lane; closing idle children after 30 minutes; a temporary router while the main is stuck.
- Rule text for main and child sessions (repo block, agent instructions).

## Decisions (operator, Telegram)

1. **Where children run:** each child gets its own copy-on-write copy inside the project (no trust
   prompt, almost no disk) and branch. Branches are merged only
   with the operator's approval.
2. **Parallel children per project:** no limit. Rate limits go through the existing stuck-work flow.
3. **Children's tool:** the same tool as the project's open main session, opened with the project id.
4. **Main session offline:** ask the operator on Telegram, with buttons to wait or to open a new
   main session.
5. **Results:** children keep today's logic and report straight to the sender. The main session
   only routes.
