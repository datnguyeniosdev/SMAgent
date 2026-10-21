# Implementation Plan

> **Phase 1 plan.** Later phases: [PHASE_2_IMPLEMENTATION.md](PHASE_2_IMPLEMENTATION.md), [PHASE_3_IMPLEMENTATION.md](PHASE_3_IMPLEMENTATION.md), [PHASE_4_IMPLEMENTATION.md](PHASE_4_IMPLEMENTATION.md), [PHASE_5_IMPLEMENTATION.md](PHASE_5_IMPLEMENTATION.md). In-memory state (risk below) is resolved by the SQLite store in Phase 4.

## Repository state before work (2026-10-20)

- The project directory was **empty**: no git repo, code, tests, or config.
- Toolchain: Python 3.10 system, **uv not installed** (installed 0.13.2 via Homebrew); project pinned to 3.12.
- Found: Claude Code CLI 2.1.71, Claude.app, Unity 6000.3.24f1, `unity` CLI 1.0.0-beta.12, Docker 27.5.1.
- Missing: OpenCode, Gemini CLI. No provider API keys or Telegram credentials in the environment.
- No existing Telegram bot, so no webhook or polling mode to inspect. Polling refuses to start if a webhook is set.

## Milestones

| # | Milestone | Status | Acceptance criteria | Evidence |
|---|---|---|---|---|
| 1 | Foundation: uv project, config, models, structured logging, auth | **done** | `uv sync --locked`, `uv run uvicorn app.main:app` start; config validated; secrets redacted | server smoke test; `tests/test_gateway.py::test_config_validation` |
| 1b | Telegram group/channel delivery | **implemented, not verified live** | Command from allowlisted user in control group; report lands in channel | fake-bot tests only; needs a bot token |
| 2 | Connector contracts, capability router, one coding runtime | **done (partial verification)** | Router never downgrades silently; Claude Code CLI runs headless | router tests; real CLI run hit 401 auth |
| 3 | Model API provider | **done (mocked)** | Text, structured output, tool-call passthrough, retries, usage | `tests/test_connectors.py` |
| 4 | Asset pipeline | **done (mocked generation)** | Image provider adapter, manifest, validation, approval | `test_design_implement_review` |
| 5 | Multi-agent workflows | **done** | Design→Implement→Review, Parallel, Delegate+Review, Quick Actions | `tests/test_orchestrator.py` |
| 6 | QA + execution safety | **done (no secure sandbox by default)** | Configured commands only, timeout, output cap, docker option, approvals | `test_qa_*` |

## Dependencies

Telegram live verification needs a bot token, control-group ID, channel ID, and user IDs.
Real provider tests need `OPENAI_API_KEY`/`OPENAI_MODEL` or `DEEPSEEK_API_KEY`.
Claude Code runs need a valid login (`claude` → `/login`) or `ANTHROPIC_API_KEY`.
Unity validation needs a Unity-MCP server config and/or a batch-mode QA command.

## Risks

- **In-memory state**: a restart loses tasks, sessions, approvals, and action tokens. Worktrees and artifacts stay on disk.
- **No secure sandbox by default**: `QA_SANDBOX=none` runs configured commands on the host. Claude Code runs on the host with its own permission system.
- **Claude Code permission prompts** can't be answered from Telegram. Unapproved tools are denied in `-p` mode.
- **Provider schema drift**: HTTP adapters are tested only against mocks.
- **Cost**: Claude Code reports a client-side cost estimate. HTTP providers report tokens only.

## Remaining work (by impact)

1. Live-verify Telegram (needs credentials) and Claude Code (fix CLI auth). Rerun `scripts/smoke_claude_code.py`.
2. Real OpenAI/DeepSeek smoke test with a key (`tests` stay mocked).
3. Persist tasks/sessions/actions (SQLite) so approvals survive restarts.
4. Stream Claude Code progress (`--output-format stream-json`) into throttled Telegram updates.
5. Unity-MCP config + Unity batch-mode QA command for real build/visual checks.
6. Gemini and OpenCode connectors; OpenAI image edits.
7. Attachment/worktree retention policy (cleanup is manual today).
