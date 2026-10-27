# Phase 2 Implementation: Real Tool Execution

Baseline and environment: [PHASE_2_AUDIT.md](PHASE_2_AUDIT.md). Phases 2–5 were requested in sequence within one session and built on one codebase, so later phases extend these modules instead of replacing them.

## Implemented

| Component | Module | Notes |
|---|---|---|
| `ToolDefinition`, `ToolRegistry`, `ToolExecutor`, `ToolPolicyEngine`, `ToolExecutionRecord`, `ToolResult` (`ToolOutput`), `ToolEventPublisher` | `app/tools.py` | lifecycle `approval_required → running → succeeded/failed/timed_out/cancelled/unknown`; JSON-schema input validation (`jsonschema`); optional output schema; 20 000-char output cap; idempotent `call_id`; publisher failures never change outcomes |
| Workspace tools | `app/builtin_tools.py` | `workspace.list_files/read_file/create_file`, `git.status/diff`, `artifact.hash`, `image.validate`, `qa.run`; canonical paths, symlink and `.git` protection, create-only writes |
| QA runner | `app/qa.py` | run id, task id, cwd, timestamps, exit code, `passed/failed/timed_out/skipped`, separate bounded stdout/stderr; missing executable → `skipped` (never passed); killed on cancellation |
| MCP client | `app/mcp_client.py` | official SDK `mcp` 2.3.0 `Client`; stdio + Streamable HTTP; discovery; allowlist; read-only tools vs approval-required tools; path-argument confinement; per-call sessions; error unwrapping (`mcp_protocol` vs `mcp_connection` vs `mcp_tool_error`) |
| Model tool-calling loop | `app/tool_loop.py` | Chat Completions tools → registry lookup (unknown → structured error to the model) → policy → real execution → `role=tool` results → continue; iteration, time and call budgets; stops at approval |
| Provider additions | `app/connectors/http_providers.py` | multi-turn `chat()`, free `health()` via `GET /models` |
| Interfaces | `telegram_bot.py`, `main.py` | `/tools`, `/tool <name> <json>`; channel events `🔧 ✅/❌ tool …`; `GET /api/v1/tools` |

## Real execution evidence

| Tool/runtime | Operation | Result | Real? |
|---|---|---|---|
| `claude mcp serve` 2.1.71 | `tools/list`, `Read greet.py` | 19–22 tools; file contents returned | real |
| `claude mcp serve` | `Glob *.py` | FAILED: `Tool Glob has an output schema but did not return structured content` (server-side) | real |
| `unity mcp` 1.0.0-beta.12 | `tools/list`, `editor_status` | 160 tools once, later 0; `editor_status` → tool error (no Editor) | real |
| Local MCP fixture (SDK `MCPServer`) | echo / read_len / flaky / explode | correct outputs, path confinement, errors | real protocol, test server |
| Workspace + git + QA tools | list/read/create/diff/status/pytest | correct; see demos | real |
| Claude Code CLI | headless run | FAILED: 401 OAuth token invalid | real |
| OpenAI / DeepSeek / images | – | NOT CONFIGURED (no keys); unit-tested with mocked transports | mocked |
| Telegram | getMe / getWebhookInfo / getUpdates / getChat | bot valid; chats not found (placeholder IDs) | real, blocked |

## Repeat

```bash
RUN_INTEGRATION=1 uv run pytest tests/integration -rs
```

```bash
uv run python -m scripts.smoke_telegram
```

```bash
uv run python -m scripts.smoke_claude_code
```

The last one makes a real, paid Claude Code call; run it outside a Claude Code session.
