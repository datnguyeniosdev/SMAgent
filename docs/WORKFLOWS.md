# Workflows

Choose one from the inline keyboard after `/task …`, or pass `workflow` to `POST /api/v1/tasks`.

| Workflow | Steps | Needs |
|---|---|---|
| `ask` | One model/runtime call; images and files routed by capability | any text connector |
| `code` | approval → worktree → Agent A → diff artifact → QA | `WORKSPACE_ROOT`, a writer (`claude_code`) |
| `delegate_review` | approval → Agent A → QA → Agent C review → fix rounds (same implementer, resumed session) → re-QA → re-review | writer + reviewer with structured output |
| `parallel` | approval → two implementers in two worktrees, concurrently → QA each → objective comparison | writer (one runtime runs twice if only one is available) |
| `design_implement_review` | approval → Agent B design (criteria, spec, asset prompts) → image generation → manifest + validation → asset approval → assets copied into the worktree → Agent A → QA → Agent C → fix loop | designer (image input if references are attached), writer, reviewer; image provider optional |

## Outcomes

- `succeeded` (delegate/design flows) means **no critical/high findings remained and all configured QA passed**.
  With no QA configured, the report says tests/builds were **not run**.
- `failed` with `review_blocked`: blocking findings or failed QA remained after `MAX_FIX_ROUNDS`.
- `unknown`: a writer was interrupted (timeout) and may have left partial edits. Inspect the worktree.
- Nothing is merged. The final report names the branch `agw/<task_id>`. Review the diff (Quick Action "Review diff"), then merge yourself.

## Independence of Agent C

The reviewer gets the requirements, the **real diff against the base commit**, and the QA output. It never gets the implementer's summary.
It runs in a fresh session. It prefers a different connector than the implementer, and falls back to the same one only if nothing else qualifies.
It needs structured output (findings schema: severity, description, location, reproduction, evidence, remediation, verification_status).
A clean review does not mean the build or runtime works, and the report keeps those apart.

## Assets

Every generated image gets a manifest entry: asset ID, task, reference attachment IDs, provider/model, prompt artifact, dimensions, format, color mode, alpha, sha256, path, validation status/issues, approval status.
Checks: PNG format, expected size, alpha when requested, max dimension, optional power-of-two, duplicate hashes, missing/duplicate names.
Providers don't guarantee exact size or transparency; mismatches are reported, not hidden.

## Unity

The implementer prompt tells the agent to list Unity-MCP tools before use and verify each result.
Configure the server with `CLAUDE_CODE_MCP_CONFIG` (see https://github.com/IvanMurzak/Unity-MCP).
For build validation, add a batch-mode command to `QA_COMMANDS`, for example a `unity build …` invocation for your project.
Without one, reports say build and visual validation were not performed.

## Not implemented

Steps run sequentially even when their dependencies would allow parallelism (dependencies are still recorded and enforced).
No automatic task splitting by a planner model. The designer's acceptance criteria act as the plan.
