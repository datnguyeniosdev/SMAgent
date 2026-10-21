# Phase 2 Audit (before implementation, 2026-10-20)

## Repository

- Git: one commit (`adf75dd Initial commit`), clean working tree.
- Tests: `uv run pytest` → **69 passed** (all unit tests with mocked connectors/bot). Lint clean.
- Modules: gateway (`main.py`, `telegram_bot.py`), `orchestrator.py`, `router.py`, connectors (`claude_code.py`, `http_providers.py`), `workspace.py`, `qa.py`, `assets.py`, `storage.py`, `actions.py`.
- **No tool registry existed.** Tools were implicit: QA commands ran directly, and model `tool_calls` were returned but never executed.
  No MCP client, and no way to invoke a single tool from Telegram/REST.

## Environment (verified by running commands, not by reading config)

| Item | Finding | How verified |
|---|---|---|
| `uv` | 0.13.2 | `uv --version` |
| Python | 3.12 (venv) | `.python-version`, `uv run` |
| MCP SDK | not installed → added `mcp` **2.3.0** (FastMCP renamed `MCPServer`; result fields snake_case: `structured_content`, `is_error`, `input_schema`) | `uv add mcp`, signature inspection |
| Claude Code CLI | 2.1.71. **Model calls fail: `401 OAuth access token is invalid`**, also with a clean `env -i` environment, so the stored login itself is invalid | real `claude -p` runs |
| Claude Code as MCP server | `claude mcp serve` works with no model call. Server `claude/tengu` 2.1.71 exposes 19 tools (Read, Glob, Grep, Bash, Write, Edit, …) | real MCP `initialize` + `tools/list` |
| Unity CLI MCP | `~/.unity/bin/unity mcp` (stdio) → server `unity-mcp` 1.0.0-beta.12, **0 tools**, because no Unity Editor with the Pipeline package is running (`unity status`: `STATUS_NO_INSTANCES`) | real MCP `tools/list`, `unity status --json` |
| Unity-MCP (IvanMurzak, `ai-game-developer`) | configured in `~/.claude.json` for project `MiniGame2048` as HTTP `localhost:26609`; **nothing listening** on that port (Editor not running) | `lsof` |
| OpenCode, Gemini CLI, Codex | not installed | `which` |
| Docker | 27.5.1 CLI present | `which docker` |
| Telegram bot token | **valid**: `@darren_connector_bot` (`getMe` ok), no webhook, privacy mode on (`can_read_all_group_messages=false`) | real Bot API calls |
| Telegram chat/user IDs | `.env` still has the `.env.example` placeholders. `getUpdates` returned 0 updates, so the bot hasn't been added to a group/channel, or nobody has messaged it | real `getUpdates` |
| OpenAI / DeepSeek / Gemini keys | **blank** in `.env`, absent from the environment | `.env` key presence check (values never printed) |
| Image provider | not configured (needs `OPENAI_API_KEY` + `OPENAI_IMAGE_MODEL`) | config |
| `API_TOKEN`, `WORKSPACE_ROOT` | blank | config |

## Classification before Phase 2

| Tool / integration | State |
|---|---|
| QA runner (configured commands) | implemented, real subprocess; merged stdout/stderr; a missing executable crashed instead of reporting `skipped` |
| Workspace (worktrees, diff) | implemented, real git |
| Claude Code runtime | implemented; real invocation fails on auth |
| OpenAI/DeepSeek chat | implemented; mocked only; tool calls not executed (no loop) |
| OpenAI images | implemented; mocked only |
| MCP | **absent** |
| Tool registry / policy / execution records / events | **absent** |
| Telegram | implemented; mocked only; real token valid but no chat configured |

## Phase 2 priorities (chosen from what can actually be verified here)

1. Tool registry + policy + executor + records + events (needed for everything else).
2. Real workspace and QA tools (git and filesystem are available).
3. Real MCP client, verified against `claude mcp serve` (safe read-only tool), with discovery of `unity mcp`.
4. Model tool-calling loop. It can only be unit-tested: no model credentials are available.
5. Expose tools through Telegram (`/tools`, `/tool`) and REST. Run an E2E smoke through the real orchestrator.
   Real Telegram delivery is blocked until chat IDs exist.
