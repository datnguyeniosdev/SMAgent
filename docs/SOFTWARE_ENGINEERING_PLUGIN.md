# Software Engineering Plugin (`software`)

`app/plugins/software.py` · permissions: `workspace:read, workspace:write, process:execute, artifact:write`.

| Operation | Needs | Workflow | Verified |
|---|---|---|---|
| `inspect_repo` | `git.status`, `workspace.list_files` | status ∥ files → `software.repo_report` (Markdown + counts) → `core.artifacts_readable` | real (tests) |
| `run_checks` | `qa.run` + `QA_COMMANDS` | every check in parallel → `software.checks_passed` gate | real (tests) |
| `apply_files` | `workspace.create_file`, `git.diff`, data `{"files": {path: content}}` | approval → sequential create-only writes in the task worktree → diff ∥ status ∥ checks → `software.change_report` → gate | **real (demo P5-A: pytest passed in worktree, main checkout untouched)** |
| `implement` | `workspace.write` agent | approval → agent edits isolated worktree → diff artifact → QA | blocked: Claude Code 401 (demo P4-G) |
| `implement_reviewed` | `workspace.write` + `model.structured` | implement → QA → independent reviewer (prefers a different provider; sees the real diff + QA output, never the implementer summary) → bounded fix rounds (same implementer, resumed session) | blocked: no working model/runtime; mocked in tests |
| `parallel_implement` | `workspace.write` | two implementers, two worktrees, objective comparison | mocked in tests |

**Guarantees.**
- No commit, push, merge or deploy.
- Unrelated user changes are preserved, because work happens in `git worktree add -b agw/<task>`.
- The change report lists `git status` files, check statuses and exit codes, whether the files match the request, and diff size.
- A model's explanation is never treated as proof: success needs the checks gate (when checks exist) and `files_match`.
- With no QA configured, the envelope states "tests were NOT run".

**Example.**

```bash
curl -s -H "Authorization: Bearer $API_TOKEN" -H 'content-type: application/json' -d '{"objective":"add these files and run the tests","data":{"files":{"mathx.py":"def subtract(a, b):\n    return a - b\n"}}}' http://127.0.0.1:8000/api/v1/tasks
```

**Limitations.** Repository analysis is structural, not semantic, unless a model is available. Review and implementation need a working runtime; today Claude Code fails authentication on this machine.
