# Security

Overview and known limitations. Details:
- [SECURITY_MODEL.md](SECURITY_MODEL.md): platform policy, permissions, filesystem, execution, secrets.
- [WORKFLOW_SECURITY.md](WORKFLOW_SECURITY.md): workflow-specific threats and evidence.

## Enforced

- **Telegram:** commands only from allowlisted users inside the control group. Buttons only from allowlisted users in the control group or report channel.
- **REST:** bearer token on all `/api/v1/*` endpoints; disabled without `API_TOKEN`. Binds to `127.0.0.1` by default.
- **Quick Actions:** opaque, single-use, expiring, state-checked.
- **Approvals:** persisted with the approver, and cannot be bypassed by plugins or workflow definitions.
- **Central policy:** schema validation and the permission ceiling for every tool call. That includes model tool calls, plugin tools and MCP tools. Unknown tools are rejected, never improvised.
- **Workspace confinement:** canonical paths, symlink-escape rejection, `.git` blocked, create-only writes in isolated worktrees or scratch dirs, nothing merged or pushed.
- **Uploads:** MIME allowlist plus content signature, size limits, task-bound attachments, artifact hashes re-checked on read.
- **Execution:** only configured QA commands (no shell). Scrubbed environment, timeouts, output caps, and process-group kill on cancellation. Docker mode for real isolation.
- **Secrets:** environment only. Redacted in logs; Claude Code and MCP child processes get minimal environments.
- **Side effects:** never blindly retried or replayed after a crash (`unknown` instead).

## Known limitations

1. `QA_SANDBOX=none` (default) and the Claude Code runtime execute on the host as your user. They are **not** sandboxed.
   The worktree is a working directory, not an OS boundary. A `claude -p` run also loads hooks and `.mcp.json` from the target repo, so only point `WORKSPACE_ROOT` at trusted repositories.
2. Claude Code's per-tool permission prompts are not bridged to Telegram. The app-level approval covers a whole agent run.
3. Plugins are Python code loaded in-process. The permission ceiling governs what their **tools** may do through the policy engine.
   A malicious plugin module could still call Python APIs directly, so enable only plugins you trust (`PLUGINS`).
4. MCP servers run as local processes with the workspace as cwd. Allowlist only tools you have reviewed; non-read-only tools also need `mcp:execute` plus approval.
5. Single-tenant: one user allowlist, no per-user quotas or tenant isolation.
6. Quick Action tokens are in memory, so after a restart old buttons stop working (approvals are re-issued). See [WORKFLOW_RECOVERY.md](WORKFLOW_RECOVERY.md).
7. No REST rate limiting. Keep the API on localhost or behind an authenticated proxy.
8. Attachments, diffs and documents are sent to whichever external provider handles a step (Anthropic, OpenAI, DeepSeek). None is configured and working on this machine today.

## Do not commit

`.env`, `var/` (state DB, media, artifacts, worktrees, scratch), `.venv/`. All are git-ignored. `docs/evidence/demo_results.json` contains only synthetic demo data.
