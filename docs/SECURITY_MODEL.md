# Security Model (platform policy)

The central rule: **every** operation goes through the same chain. That includes interface commands, plugin operations, model tool calls, and MCP tools.

```
interface auth → TaskRequest validation → planner validation (registry + policy) →
engine (approval gates, limits) → ToolExecutor: schema → ToolPolicyEngine → handler → output checks → record/event
```

## Who can do what

| Boundary | Enforcement | Tested in |
|---|---|---|
| Telegram control | user ∈ `TELEGRAM_ALLOWED_USER_IDS` **and** chat = control group. Callbacks: allowlisted user + control group/report channel. Discussion group grants nothing | `test_gateway.py` |
| REST | `Authorization: Bearer API_TOKEN` (constant-time). Disabled (503) without a token. Artifact content only via authenticated `/artifacts/{id}/content` | `test_gateway.py`, `test_platform.py` |
| MCP `/mcp` | Bearer `API_TOKEN` by default. With `MCP_REQUIRE_TOKEN=false`, no token, but only same-machine requests: loopback client, `Host` of localhost/127.0.0.1/::1, and no non-local `Origin` (blocks DNS rebinding and browser CSRF). Any local process can then use the gateway. OAuth discovery probes get a quiet 404 | `test_config_gateway.py` |
| Quick Actions | 12-char random token, single use, TTL, allowed task states, grouped consumption (Approve/Reject) | `test_core.py`, `test_orchestrator.py` |
| Approvals | Persisted request; decision records approver and time. Expired → rejected. Duplicate or unknown → error. A step can't run until its approval is recorded | `test_engine.py`, demo E |

## Permission model

- **Plugin ceiling**: a plugin's tools may only require permissions listed in its manifest **and** in `PLUGIN_PERMISSIONS`. Installing a plugin grants nothing else.
  Tools must be prefixed with the plugin id. Load errors are isolated to that plugin.
- **Runtime grant**: a tool step gets `TOOL_PERMISSIONS ∩ step.permissions` (if the step narrows them). The policy engine denies any tool needing more (`forbidden`, with the missing permissions named).
- `mcp:execute` (non-read-only MCP tools) and network permissions are **not granted by default**.
- Tools flagged `requires_approval` (non-read-only MCP tools, image generation) and `workspace.write` agent steps always pause for approval. `require_approval=true` on a request adds approval to every side-effecting step.
- Model-generated tool calls are untrusted input. They are matched against the registry (no invented tools), schema-validated (`additionalProperties: false` on built-ins), policy-checked, and executed once per call id.

## Filesystem and data

- Workspace tools canonicalize paths, reject anything resolving outside the task workspace (including through symlinks), and block `.git/`.
  Reads are capped at 200 KB, writes at 200 KB, and are create-only.
- Writes land in a per-task git worktree (when `WORKSPACE_ROOT` is set) or a per-task scratch directory. The main checkout is never modified, committed or merged.
- Attachments and artifacts are stored under generated ids, with an allowlisted MIME type and a content signature check.
  - Attachments are bound to one task, and artifacts are readable only by their task family or when explicitly passed as input.
  - Artifact hashes are re-checked on read.
  - Inputs are never overwritten.
- Data/documents plugins parse with stdlib parsers, with row/column/size limits. No user code, SQL or formulas are executed.

## Execution

- QA commands come only from configuration (no shell, no model-supplied commands). They run with a scrubbed environment, a timeout and an output cap, and are killed on cancellation.
  `QA_SANDBOX=docker` adds `--network none` and only the worktree mounted.
  `QA_SANDBOX=none` is **not** a sandbox.
- The Claude Code runtime runs on the host under its own permission system:
  - read-only roles use `dontAsk` with Read/Grep/Glob
  - implementers use `acceptEdits` plus `CLAUDE_CODE_ALLOWED_TOOLS`
  - secrets are stripped from its environment
- MCP stdio servers get a minimal environment (`PATH, HOME, LANG, LC_ALL, TMPDIR, USER` + explicit passthrough) and the task workspace as cwd. Path-like arguments are confined to the workspace.
- Per-task limits: step `timeout_s`, workflow `timeout_s`, `max_concurrency`, retry attempts/elapsed time, tool output cap (20 000 chars), tool-loop iterations/time/calls.

## Secrets

Loaded only from the environment / `.env` (git-ignored). The log formatter redacts:
- registered secret values
- bot-token patterns
- `sk-` keys
- bearer/authorization values

`httpx` request logging is raised to WARNING because URLs contain the bot token. Telegram reports contain summaries only: no environment, no raw logs beyond bounded QA tails.

## Configuration

- YAML may not contain secrets: secret-like keys and values are rejected at validation, and providers reference `*_env` variables.
- Plugins load only from `policies.trusted_plugin_modules` when a configuration is present. A manifest never grants permissions.
- `policies.provider_allowlist` and `policies.tool_allowlist` restrict executor eligibility. `policies.approvals` add approval gates by executor id (`runtime:claude_code.new_execution`, `capability:*`, `tool:*`).
- Telegram feedback is accepted only from allowlisted users in the configured Discussion Group. It must resolve to a known report (otherwise the user is asked), and duplicate updates are ignored.
- Reports never include filesystem paths: artifact ids only.

## SSRF

Downstream MCP server URLs must use http(s) and a host from `policies.mcp_url_hosts` (default: loopback only); MCP servers come only from configuration, never from requests. There is no URL-fetching tool. `documents.web_research` requires a `search.web` capability that is not implemented, so it is refused (`NOT CONFIGURED`). Links found in documents are reported, never fetched.

## Known gaps

See [SECURITY.md](SECURITY.md#known-limitations).
