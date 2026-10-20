# Capability Matrix

Status as of 2026-10-20, on this machine. **Verification status** is one of:
**verified** (real smoke test passed), **partially verified** (real run exercised part of the path),
**mocked** (implemented against official docs, tested only with mocked transports),
**unavailable** (not implemented / not installed / no supported API).

## Connectors

| Provider/runtime | Verified version | Official protocol | Text in | Image in | File in | Image gen/edit | Tool calling | Workspace access | Terminal | Session continuation | Streaming events | Cancellation | Approval support | Known limitations | Verification |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **Claude Code CLI** (`claude_code`) | 2.1.71 (installed) | Headless `claude -p --output-format json` | yes | via Read tool on `--add-dir` paths (documented, not run) | same (text, PDF, images) | no | runtime-native tools; MCP via `--mcp-config` | read (`--tools Read,Grep,Glob`, `dontAsk`) / write (`acceptEdits`) in an isolated worktree | only tools in `CLAUDE_CODE_ALLOWED_TOOLS` | `--resume <session_id>` (implemented, not run) | not used (JSON result only; `stream-json` exists) | SIGINT → SIGTERM → SIGKILL on process group | runtime permission prompts are **not** bridged to Telegram: `-p` denies unapproved tools; app approvals gate the whole run | CLI refuses to start inside another Claude Code session (`CLAUDECODE` is stripped). Local OAuth token was invalid: every real run returned 401. A 401 arrives as `subtype:"success"` with `is_error:true`. A `--json-schema` run did not exit within 40 s while auth was failing (the connector timeout covers it). | **partially verified**: argv accepted, JSON result schema and error path observed for real; no successful model turn |
| **OpenAI** (`openai`) | n/a (no key) | `POST /v1/chat/completions` (+ `json_schema` response_format) | yes | `image_url` data URLs | text/JSON inlined; PDF → explicit unsupported error | separate provider below | `tools` passthrough; tool calls returned, not executed | none | no | no (stateless) | no | client-side abort only | n/a | Model must be set explicitly (`OPENAI_MODEL`); no defaults invented | **mocked** |
| **OpenAI Images** (`openai_images`) | n/a (no key) | `POST /v1/images/generations`, `data[0].b64_json` | prompt | not used (edits endpoint not implemented) | no | generation; `background=transparent`, `output_format=png` | n/a | n/a | n/a | n/a | n/a | client-side abort | assets need user approval before integration | Exact size/alpha not guaranteed; output is validated after generation. Docs list size constraints (multiples of 16, ratio ≤ 3:1). | **mocked** |
| **DeepSeek** (`deepseek`) | n/a (no key) | `POST https://api.deepseek.com/chat/completions` | yes | off by default (`DEEPSEEK_IMAGE_INPUT=true` enables; docs mention `image_url`, unverified) | text inlined | no | function tools | none | no | no | no | client-side abort | n/a | Only `json_object` documented → schema goes in the system prompt and the JSON is parsed and checked | **mocked** |
| **Google Gemini** | — | Google Gen AI SDK | — | — | — | — | — | — | — | — | — | — | — | Not implemented: one HTTP provider was validated first, as the plan required | **unavailable** |
| **Claude Code Desktop** | Claude.app present | none found | — | — | — | — | — | — | — | — | — | — | — | No documented public API for driving Desktop sessions; reverse-engineering refused by design. Use the CLI runtime. | **unavailable** |
| **OpenCode** | not installed | Server API | — | — | — | — | — | — | — | — | — | — | — | `opencode` binary not found | **unavailable** |
| **Unity-MCP** | Unity 6000.3.24f1 + `unity` CLI 1.0.0-beta.12 present; MCP server not configured | MCP via `CLAUDE_CODE_MCP_CONFIG` | — | — | — | — | MCP tools (whatever the server exposes) | — | — | — | — | — | — | Implementer prompt says to list tools first and verify results. No MCP config exists on this machine, so nothing was exercised. | **unavailable** (wiring only) |

## Platform features

| Feature | Status |
|---|---|
| Telegram control group commands, inline keyboards, callbacks | **mocked** (fake bot in tests; no bot token available) |
| Telegram channel reports, photo/document delivery, parse-mode fallback, RetryAfter backoff | **mocked** |
| Telegram polling + webhook-conflict check | implemented, **not run** (no token) |
| REST API + bearer auth | **verified** locally (uvicorn started, `/health` 200, unauthenticated control → 401) |
| Git worktree isolation, diff collection, single-writer lock | **verified** (real git in tests) |
| QA runner (subprocess, timeout, output cap) | **verified** (real subprocesses in tests) |
| QA runner docker mode | argv only (unit test); not executed |
| Unity batch-mode build/visual validation | **unavailable** unless configured as a QA command |
| PNG/JPEG/WebP/GIF header inspection, manifest checks | **verified** (stdlib, tests) |
| Durable task state | **unavailable**: in-memory, lost on restart |
