# Changelog

All notable changes are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Stuck work goes to the operator. When an agent stops on a rate limit or API error, or misses its
  lease, its work is held on Telegram with buttons to move it to another session, wait, or cancel.
  When that agent runs again, the gateway asks before it goes on.
- ❌ Từ chối for generated art. Telegram passcode entry happens in a private chat with the bot, and 3
  wrong tries cancel the question.
- Cursor integration. A `sessionStart` hook gives each chat the gateway rules. A `stop` hook waits
  for Telegram work and submits it as the next message.
- Antigravity integration. A detached `agw wait --agent antigravity` wakes the conversation through
  `agentapi`.
- `agw wait` with a server long-poll (`/api/v1/channel/wait`). Idle sessions now cost nothing, and
  this replaces cron polling and health checks.
- The server pushes queued work into open Codex desktop threads.
- Results of delegated tasks, art reviews, project reviews and answered questions are pushed back to
  the session that asked, in the same tool.
- Secure questions (`request_human_input(secure=true)`) with passcode confirmation from Telegram.
  Unanswered questions are re-sent every 30 minutes. `close_question` closes the Telegram copy.
- Session titles that name the tool and project, for example `ClaudeCode(App)-MarioClone`. Telegram
  reports a dropped session with the reason.
- Open-source files: LICENSE (MIT), CONTRIBUTING, CODE_OF_CONDUCT, SECURITY, issue and PR templates,
  and an operator guide.

### Changed
- Detection of new or renamed project folders (`.git`, `AGENTS.md` or `CLAUDE.md`; renames are found
  by the README name).
- Phase plans and audits moved to `docs/history/`.

## [0.1.0] - 2026-10-20

### Added
- First version: the gateway with a REST API, Telegram control, an MCP server and a workflow engine,
  with software, documents, data, media and Unity plugins.
