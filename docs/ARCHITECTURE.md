# Architecture

> **Superseded (Phase 1 snapshot).** Current architecture: [GENERIC_PLATFORM_ARCHITECTURE.md](GENERIC_PLATFORM_ARCHITECTURE.md). Phase 1 workflows now run as plugin task types.

```
Telegram (control group) ─┐                      ┌─> Telegram (report channel)
REST API (bearer token) ──┴─> FastAPI app/main.py │
                                 │                │
                     Orchestrator (app/orchestrator.py) ── Notifier (TelegramGateway | LogNotifier)
             ┌───────────┬───────┴─────┬──────────────┬──────────────┐
          Router      WorkspaceMgr   QARunner     Attachment/Artifact   ActionRegistry
       (router.py)   (workspace.py)  (qa.py)      stores (storage.py)    (actions.py)
             │
  Connectors (app/connectors): ClaudeCodeConnector · ChatCompletionsProvider(openai, deepseek)
  ImageGenerationProvider: OpenAIImageProvider        Assets/manifest: app/assets.py
```

One process runs uvicorn (REST) plus aiogram long polling as a background task. No webhook endpoint is exposed.

## Responsibilities

| Concern | Module | Notes |
|---|---|---|
| Gateway | `main.py`, `telegram_bot.py` | Auth on every control action; structured errors |
| Orchestrator / workflow engine | `orchestrator.py` | asyncio tasks; child `Task`s per step with `depends_on`; validated state transitions |
| Model providers | `connectors/http_providers.py` | Chat Completions wire format; retries on 429/5xx; usage recorded |
| Agent runtimes | `connectors/claude_code.py` | Subprocess per run; read-only vs write modes; resume; process-group cancel |
| Tool executors | `qa.py` | Only named, pre-configured commands |
| Workspace manager | `workspace.py` | `git worktree add -b agw/<task>`; one writer per worktree; never merges, never `--force` removes |
| QA pipeline | `qa.py` + `_review` in orchestrator | Automated checks, then independent reviewer |
| Artifact manager | `storage.py` | `var/artifacts/<task>/<id>_<name>`, sha256, create-only (no overwrite) |
| Assets | `assets.py` | Stdlib image header parsing, Unity-oriented rules, manifest |

## Contracts

- `AgentConnector`: `caps`, `available`, `verification`, `execute(AgentRequest) -> AgentResult`, `cancel(task_id) -> bool`.
  Model providers and coding runtimes share it, but each declares its capabilities honestly.
  An HTTP provider rejects `read_only=False` and `resume_session` with `UnsupportedCapability`.
- `ImageGenerationProvider.generate(prompt, size, transparent) -> bytes` is separate from model inference.
- `Notifier.report(task, html, actions, files, photos, control)`.

## Task model

States: `created → queued → running ⇄ waiting_for_approval/waiting_for_user → succeeded | failed | cancelled | unknown`.
`unknown` means the outcome can't be established, for example a writer runtime killed mid-run.
Every agent call records a `UsageRecord` (provider, model, timestamps, status, error category, tokens, provider-estimated cost) and an `AgentSession`.

## Persistence

All task, session, and action state is **in memory and lost on restart**. Files (media, artifacts, worktrees) stay under `DATA_DIR`.
The orchestrator only touches state through its own methods, so a SQLite store can replace the dicts without changing connectors.
