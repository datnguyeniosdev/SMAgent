# Game research plugin

`app/plugins/game_research.py`. Studies a game from what is available (source tree or shipped build)
and keeps research evidence apart from new creative work. Engine knowledge is data (adapters); the
workflow is the same for every engine.

## Tasks

**research** (`domain=game_research, task_type=research`, or a request mentioning research/playtest/evidence).
`WORKSPACE_ROOT` points at the game. Steps:

| Step | What it does | Output |
|---|---|---|
| `inventory` (tool) | Detects the engine and whether this is `source` or `build`; records every file in scope with size and sha256; reads metadata from the marker file. Symlinks, `.git` and adapter `skip_dirs` are left out; `exclude` globs from config also. | `evidence_manifest.json` |
| `evidence` (tool) | Re-checks files against this run's manifest, and against an earlier manifest when the request passes it in `artifact_ids` (changed / missing / added). Extracts numeric facts from unchanged text and code files; where sources disagree it writes an **open** conflict record keeping every claim (file, line, quote, sha256). | `evidence_check.json` |
| `coverage` (plugin) | Merges playtest captures (request attachments, JSON `{"level", "width", "height", "rows"}` with `?` for unseen cells) into per-level maps. Unseen cells stay `?`; cells captures disagree on become `!` with both readings. Reports coverage, unknown cells, levels never observed, and captures (e.g. screenshots) not transcribed yet. | `coverage.json`, `coverage.md` |
| `runtime` (plugin) | What the evidence covers, the missing capabilities (`source`, `level_data`, `scripts`, `runtime_control`), and a playtest plan: **automated** when a tool matching the adapter's `control_tools` exists, otherwise **assisted** (a person plays and shares captures) with the limitation stated. | `runtime_plan.md` |

**design** (`task_type=design`, `data={"title", "content"}`, research artifacts to cite in `artifact_ids`).
Saves the design as a new versioned Markdown file in the creative workspace
(`DATA_DIR/creative/<game>/`, or `PLUGIN_CONFIG.game_research.creative_root`). Cited artifacts are
verified by checksum and listed with their sha256. It never writes into the game folder or the
artifact/attachment stores; a creative root inside any of them is refused.

## Adding an engine

No code change: add an adapter to `PLUGIN_CONFIG`:

```json
{"game_research": {"adapters": [{"name": "godot", "source_markers": ["project.godot"],
  "build_markers": ["*.pck"], "skip_dirs": [".godot"], "control_tools": ["mcp.godot.*"]}]}}
```

Every adapter, built-in or configured, must pass `tests/test_game_adapter_contract.py`, which builds a
sample tree from the adapter's own markers and runs the full research workflow on it.

## MVP acceptance (tests on `tests/fixtures/game_fixture`)

| Criterion | Test |
|---|---|
| Source available | `test_source_game_is_collected_with_metadata_within_scope` |
| Build only | `test_build_only_names_missing_capabilities_and_plans_an_assisted_playtest` |
| No control tool | same, plus `test_a_control_tool_makes_the_playtest_automated` |
| Evidence changed | `test_changed_evidence_is_caught_by_its_checksum`, `test_a_manifest_edited_on_disk_fails_the_run`, `test_a_changed_input_or_artifact_is_refused_by_its_checksum` |
| Two sources conflict | `test_conflicting_sources_are_both_kept_in_a_conflict_record` |
| Agent interrupted | `test_an_interrupted_research_run_resumes_from_its_checkpoint_without_damage` (real `kill -9`) |
| Level not fully observed | `test_partly_observed_levels_report_coverage_and_unknowns_without_inventing` |
| New design | `test_a_new_design_goes_to_the_creative_workspace_and_leaves_evidence_untouched` |
| New adapter | `tests/test_game_adapter_contract.py` |

## Known limits

- Facts are numeric `name = value` / "name value" claims keyed by name only, so per-level values of
  the same name can show up as conflicts.
- Coverage reads transcribed captures; screenshots are listed as untranscribed, not read.
- An inventory stops at 5000 files (counted as `over_limit`).
