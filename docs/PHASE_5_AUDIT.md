# Phase 5 Audit

Taken when the Phase 5 request arrived. The plugin contract, registry, engine and tool subsystem existed (Phases 2–4). The five plugins were being written.

| Plugin | Executable now? | Requires |
|---|---|---|
| Software: inspect, checks, apply files | **yes**: git, filesystem, local Python test runners | `WORKSPACE_ROOT`, `QA_COMMANDS` |
| Software: implement, review | no | a working coding runtime (Claude Code fails with 401; OpenCode not installed) or model credentials for review |
| Documents: extract, compare, report | **yes**: stdlib parsing | – |
| Documents: summarize | no | `model.text` provider (OpenAI/DeepSeek key) |
| Documents: web research | no | a search provider; none integrated, so SSRF-safe fetching is unnecessary today |
| Data: profile, validate, transform, aggregate, join, convert | **yes**: stdlib `csv`/`json` | – |
| Media: inspect/validate | **yes** | – |
| Media: generate/edit | no | `OPENAI_API_KEY` + `OPENAI_IMAGE_MODEL` (edit endpoint not implemented) |
| Unity: project inspection | **yes**: local Unity projects exist (`~/Working/FlappyBird` and others) | `WORKSPACE_ROOT` pointing at a Unity project |
| Unity: editor operations | no | Unity Editor running with the Pipeline / Unity-MCP package, plus an `MCP_SERVERS` allowlist |
| Unity: build/play-mode validation | no | a Unity batch-mode command in `QA_COMMANDS` |

Baseline before plugin work: 69 Phase 1 regression tests passing.
