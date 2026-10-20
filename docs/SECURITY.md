# Security

## What is enforced

- **Telegram authorization**: control commands are accepted only from `TELEGRAM_ALLOWED_USER_IDS`, and only inside `TELEGRAM_CONTROL_CHAT_ID`.
  Callback buttons require an allowlisted user and a click inside the control group or the report channel.
  The linked discussion group is **not** an authorization boundary. Unauthorized updates are logged and ignored.
- **REST API**: every control endpoint needs `Authorization: Bearer $API_TOKEN` (constant-time compare).
  With no `API_TOKEN`, control endpoints return 503. Binds to `127.0.0.1` by default.
  Artifact and attachment files are never served over HTTP; only metadata is returned.
- **Quick Actions**: 12-character random tokens (`secrets.token_urlsafe`) carry no payload.
  Each is bound to a task, single-use, and expires (`ACTION_TTL_S`).
  It's valid only in specific task states. Approve and Reject share a group, so using one consumes the other.
  Check-and-set happens without an `await`, so it is atomic in asyncio.
- **Attachments**: MIME allowlist plus a content-signature check. Size limit is enforced before download (Telegram) and while reading (API).
  Stored under generated IDs, so user filenames never become paths. Root containment is checked, symlinks are refused on read, and writes are create-only.
  Archives aren't accepted, and nothing is extracted. Attachments are bound to one task.
- **Secrets**: never logged. The JSON log formatter redacts registered secret values, bot-token patterns, `sk-` keys, and bearer/authorization values.
  `httpx` logging is raised to WARNING because it logs request URLs, which contain the bot token.
  Secrets are removed from the Claude Code subprocess environment. The QA environment is reduced to `PATH`, `HOME`, `LANG`, `LC_ALL`, `TMPDIR`, and `UNITY_PATH`.
- **Workspaces**: agents work only in `git worktree`s under `DATA_DIR/worktrees`, one writer per worktree.
  Nothing is committed, merged, or pushed by the platform. Worktree removal never uses `--force`.
- **Approvals**: every workflow that writes files needs explicit approval before an agent runs.
  Generated assets need approval before they're copied into a worktree. Approved assets are never overwritten (create-only).
- **QA**: only commands named in `QA_COMMANDS` run, with no shell, plus a timeout and an output cap. Models can't add commands.

## Known limitations (be honest about these)

- `QA_SANDBOX=none` (default) is **not a sandbox**: commands run as your user on the host.
  Use `QA_SANDBOX=docker` (`--network none`, only the worktree mounted, 2 GB memory) for isolation. It can't run Unity.
- **Claude Code runs on the host.** The implementer uses `--permission-mode acceptEdits`, which lets Claude Code edit files and run common filesystem commands.
  Other shell commands run only if listed in `CLAUDE_CODE_ALLOWED_TOOLS`. Claude Code's own permission system, not this platform, decides which paths it may touch.
  The worktree is its working directory, but that is not an OS-level boundary.
  A `-p` run also loads `.claude/settings.json` hooks and `.mcp.json` servers from the target repository. Only point `WORKSPACE_ROOT` at repositories you trust.
- Runtime tool-permission prompts are **not** bridged to Telegram. The app-level "Approve" button approves running the workflow, not individual tool calls.
- User attachments and diffs are sent to the selected external provider (Anthropic, OpenAI, or DeepSeek). Don't attach material you aren't allowed to share.
- State is in memory: a restart drops pending approvals. Their tokens become invalid.
- No rate limiting on the REST API. Keep it on localhost or behind an authenticated reverse proxy.

## Do not commit

`.env`, `var/` (media, artifacts, worktrees), `.venv/`. These are in `.gitignore`.
