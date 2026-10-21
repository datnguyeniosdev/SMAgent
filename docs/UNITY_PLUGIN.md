# Unity Plugin (`unity`)

`app/plugins/unity.py`. All Unity knowledge lives here: project layout, the Unity-MCP system prompt, the asset folder (`PLUGIN_CONFIG.unity.asset_dir`, default `Assets/Generated`), and the design → assets → implement → review pipeline moved out of the Phase 1 core.

| Operation | Needs | Behaviour | Verified |
|---|---|---|---|
| `inspect_project` | `unity.project_info` (read-only) | editor version (`ProjectSettings/ProjectVersion.txt`), packages (`Packages/manifest.json`), scene/prefab/script/material counts, MCP package presence; then the `unity.project_valid` gate | **real, on a local Unity project (demo P5-F: editor 6000.4.0f1, 52 packages, 1 scene, 16 prefabs, 17 scripts, no MCP package)** |
| `editor_status` | MCP tool `mcp.unity.editor_status` | queries the running Editor through a discovered MCP tool | **NOT AVAILABLE**: see below |
| `feature` | `workspace.write` agent + `model.structured` designer | approval → design spec + asset prompts → image generation (if a provider exists) → manifest + validation → asset approval → assets copied create-only into the worktree → implement with the Unity-MCP guidance prompt → QA → independent review → bounded fixes | mocked in tests; real blocked (no working runtime/model) |

**MCP reality on this machine.**
- `~/.unity/bin/unity mcp` (server `unity-mcp` 1.0.0-beta.12) connects over real MCP stdio.
- It listed **160 tools once and 0 tools on every later run**: tools appear only while a Unity Editor with the Pipeline package is connected (`unity status` → `STATUS_NO_INSTANCES`).
- Calling `editor_status` without an Editor returned a real tool error ("Invalid response format from Pipeline server").
- The IvanMurzak Unity-MCP server (`ai-game-developer`, HTTP `localhost:26609`) is configured for another project, but nothing was listening.

**Enable.**
1. Open the project in Unity with the Unity-MCP / Pipeline package.
2. Add to `MCP_SERVERS`:

   ```json
   {"name":"unity","command":"~/.unity/bin/unity","args":["mcp"],"allowed_tools":["editor_status","get_scene_hierarchy"],"read_only_tools":["editor_status","get_scene_hierarchy"]}
   ```

3. Restart, then check `/tools`.

Use only tools that `tools/list` actually exposes; the registry refuses others. For builds, add a Unity batch-mode command to `QA_COMMANDS` (with `QA_ENV_PASSTHROUGH=UNITY_PATH` if needed). Otherwise reports say builds were not run.
