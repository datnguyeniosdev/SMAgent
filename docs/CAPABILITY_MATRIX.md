# Capability Matrix

Status as of 2026-10-20 on this machine, backed by commands actually run (see [DEMO_EVIDENCE.md](DEMO_EVIDENCE.md), [PHASE_2_AUDIT.md](PHASE_2_AUDIT.md), and the opt-in suite `RUN_INTEGRATION=1 uv run pytest tests/integration -rs`).

**Legend.**
- **VERIFIED**: real operation succeeded with checked output.
- **PARTIALLY VERIFIED**: real protocol/process exercised, but the full operation was not proven.
- **MOCKED**: implemented to official docs, tested only with fakes.
- **NOT CONFIGURED**: credentials/software missing.
- **FAILED**: real attempt failed.

## Runtimes, providers, servers

| Provider/runtime | Version | Protocol | Text | Image in | File in | Image gen | Tool calling | Workspace | Terminal | Sessions | Streaming | Cancel | Approval | Status | Evidence / limitation |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Claude Code CLI (`agent:claude_code`) | 2.1.71 | `claude -p --output-format json` | yes | via Read on `--add-dir` | yes | no | native tools | read (`dontAsk`+Read/Grep/Glob) / write (`acceptEdits`) in worktree | allowlisted tools only | `--resume` | not used | process-group SIGINT→TERM→KILL (tested with fake binary) | app-level only | **FAILED** (auth) | every real run: `401 OAuth access token is invalid`, also with a clean env. Reported as `subtype: success` + `is_error: true`. Refuses nested sessions (`CLAUDECODE` stripped) |
| Claude Code as MCP server (`claude mcp serve`) | 2.1.71 | MCP stdio | – | – | Read | – | 19–22 tools exposed | cwd = task workspace | Bash exposed but **not allowlisted** | per-call session | – | – | allowlist + approval for non-read-only | **VERIFIED** (Read) | real `tools/list` + `Read` returned file contents. `Glob` **FAILED**: server declares an output schema but returns no structured content (SDK rejects it) |
| Unity CLI MCP (`unity mcp`) | unity-mcp 1.0.0-beta.12 | MCP stdio | – | – | – | – | 0 tools now (160 listed once) | – | – | – | – | – | – | **PARTIALLY VERIFIED** | server connects; tools exist only while an Editor with the Pipeline package is connected. `editor_status` without an Editor → real tool error |
| Unity-MCP (IvanMurzak `ai-game-developer`) | – | MCP HTTP `localhost:26609` | – | – | – | – | – | – | – | – | – | – | – | **NOT CONFIGURED** | nothing listening (Editor not running) |
| OpenAI chat (`agent:openai`) | – | `/v1/chat/completions` | yes | data URLs | text inline | – | `tools` + full tool loop | – | – | – | – | client abort | – | **MOCKED / NOT CONFIGURED** | no `OPENAI_API_KEY` / `OPENAI_MODEL` |
| OpenAI images (`image_provider:openai_images`) | – | `/v1/images/generations` (b64_json) | – | – | – | yes (transparent PNG option) | – | – | – | – | – | client abort | approval required | **MOCKED / NOT CONFIGURED** | tool `media.generate` not registered without a key |
| fal.ai (`image_provider:fal`) | – | queue HTTP API `queue.fal.run/{model}`, `Authorization: Key` | prompt | – | – | yes (FLUX schnell default; no alpha) | – | – | – | – | – | `PUT cancel_url` on timeout/cancel | approval required (paid) | **MOCKED / NOT CONFIGURED** | adapter + `design.image` workflow tested against a mocked API; `FAL_KEY` not set |
| DeepSeek (`agent:deepseek`) | – | `https://api.deepseek.com/chat/completions` | yes | opt-in (`DEEPSEEK_IMAGE_INPUT`) | text inline | – | function tools; `json_object` only | – | – | – | – | client abort | – | **MOCKED / NOT CONFIGURED** | no key |
| Gemini (`provider:gemini`) | – | REST `models/{model}:generateContent` (`x-goog-api-key`) | yes | `inline_data` | text inline | – | not wired | – | – | – | – | client abort | – | **MOCKED / NOT CONFIGURED** | adapter + design-analysis executor tested with fakes; `GEMINI_API_KEY`/`GEMINI_MODEL` not set |
| Gateway MCP **server** (`/mcp`) | mcp 2.3.0 | Streamable HTTP, bearer auth | – | screenshots via `submit_report` | – | – | 9 tools | – | – | – | – | `cancel_task` | – | **VERIFIED** | real HTTP session: delegate/status/result/report/human input; 401 without token; `claude mcp list` (Claude Code 2.1.71) → `✓ Connected`. Tool use *by* Claude is blocked by its 401 login |
| OpenCode | – | Server API | – | – | – | – | – | – | – | – | – | – | – | **NOT CONFIGURED** | not installed |
| Claude Code Desktop | Claude.app | none public | – | – | – | – | – | – | – | – | – | – | – | **UNAVAILABLE** | no documented control API; not reverse-engineered |
| Telegram bot | aiogram 3.31 | Bot API, long polling | – | photos/docs in | docs in | – | – | – | – | – | – | – | Quick Actions | **PARTIALLY VERIFIED** | `getMe` ok (`@darren_connector_bot`), no webhook. Control/channel IDs are placeholders, so `getChat` → "chat not found"; no group→channel E2E yet. Handlers tested with a fake bot |

## Platform capabilities

| Capability | Status | Evidence |
|---|---|---|
| Tool registry / policy / executor / records / events | **VERIFIED** | `tests/test_tools.py`; real tools in all demos |
| Workspace tools (list/read/create, git status/diff, hash, image validate) | **VERIFIED** | real FS/git in tests; demo E, P5-A |
| QA runner (`qa.run`): exit codes, bounded stdout/stderr, timeout, skip on missing runtime, kill on cancel | **VERIFIED** | demos B, C2, D (0 processes left), P5-A (real pytest) |
| QA docker mode | **PARTIALLY VERIFIED** | argv only; not executed |
| MCP client (discover, allowlist, invoke, path confinement, errors, unwrap) | **VERIFIED** | local fixture server over real stdio (tests); Claude Code MCP Read (integration) |
| Model tool-calling loop (request → registry → policy → execute → tool result → continue, budgets) | **MOCKED** provider + **real** tools | `tests/test_tools.py`; no model credentials |
| Capability registry + health + status ladder | **VERIFIED** | tests; run-time table in DEMO_EVIDENCE |
| Planner/router (explicit, classified, fallback, clarification, rejection) | **VERIFIED** | `tests/test_platform.py`; routing in every demo |
| Workflow engine (DAG, parallel, conditions, retries, timeouts, cancel, approvals, compensation, partial) | **VERIFIED** | `tests/test_engine.py`; demos A–E |
| Durable state + crash recovery | **VERIFIED** | `tests/test_recovery.py` (real `os._exit`); demo F |
| Plugins: software (deterministic ops), documents, data, media inspect, unity inspect | **VERIFIED** | demos P5-A…F |
| Plugins: software agent ops, documents summarize, media generate, unity feature/editor | **MOCKED / NOT CONFIGURED / FAILED** | need a working runtime, model, image provider or Unity Editor |
| REST API on the generic service | **VERIFIED** | TestClient tests; uvicorn smoke |
| YAML configuration (schemas, references, secrets/SSRF checks, validate CLI, validated reload) | **VERIFIED** | `tests/test_config_gateway.py`; `uv run python -m app.gateway_config config` |
| Agent/capability registry + executor router (priority, preference, fallback, structured errors) | **VERIFIED** with fake providers/runtimes | UC-02, UC-07 tests |
| Telegram report → Discussion reply → feedback → follow-up workflow | **MOCKED** Bot API (handler code real) | UC-04, correlation/duplicate/auth tests; live group not configured |
| Visual feedback → deterministic prompt → handoff (`new_execution` / `return_to_caller`) | **MOCKED** Gemini + runtime; prompt builder/handoff records real | UC-05 tests; real Claude run → 401 → fallback verified in tests |
| Durable Quick Action tokens | **UNAVAILABLE** | in memory by design; approvals re-issued after restart |
