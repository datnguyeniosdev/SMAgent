"""Contract every engine adapter must pass (MVP criterion 9). Built-in adapters and adapters added
through PLUGIN_CONFIG run the same end-to-end research workflow; adding an engine is data in config,
no change to the workflow core. A sample tree is generated from each adapter's own markers."""

import pytest

from app.config import Settings
from app.intake import TaskRequest
from app.models import TaskState
from app.plugins.game_research import ADAPTERS
from app.tools import ToolDefinition, ToolOutput
from tests.conftest import build_orch, wait_done
from tests.test_game_research import artifact_json, research

CONFIGURED = [  # what an operator adds for a new engine: config only
    {
        "name": "godot",
        "source_markers": ["project.godot"],
        "build_markers": ["*.pck"],
        "skip_dirs": [".godot"],
        "control_tools": ["mcp.godot.*"],
    }
]
ALL = [a.model_dump() for a in ADAPTERS] + CONFIGURED


def sample(glob: str) -> str:
    return glob.replace("*", "sample")


def orch_for(tmp_path, root, adapters=CONFIGURED):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "var",
        workspace_root=root,
        max_attachment_bytes=1024 * 1024,
        action_ttl_s=60,
        config_dir=None,
        plugin_config={"game_research": {"adapters": adapters}},
    )
    return build_orch(settings, [])[0]


@pytest.mark.parametrize("mode", ["source", "build"])
@pytest.mark.parametrize("adapter", ALL, ids=lambda a: a["name"])
async def test_adapter_contract(tmp_path, adapter, mode):
    root = tmp_path / "game"
    marker = sample(adapter[f"{mode}_markers"][0])
    (root / marker).parent.mkdir(parents=True, exist_ok=True)
    (root / marker).write_text("{}")
    (root / "notes.md").write_text("speed 5\n")
    for d in adapter["skip_dirs"]:  # generated folders are never evidence
        (root / d).mkdir(parents=True, exist_ok=True)
        (root / d / "cache.bin").write_bytes(b"x")
    orch = orch_for(tmp_path, root)

    inv, runtime, _ = await research(orch)

    assert (inv["engine"], inv["mode"], inv["marker"]) == (adapter["name"], mode, marker)
    paths = {f["path"] for f in artifact_json(orch, inv["manifest_artifact"])["files"]}
    assert paths == {marker, "notes.md"}
    assert runtime["control"] == "assisted" and "runtime_control" in runtime["missing_capabilities"]

    if adapter["control_tools"]:  # a matching control tool turns the playtest automated
        name = adapter["control_tools"][0].replace("*", "play")

        async def play(ctx, args):
            return ToolOutput("ok", {})

        orch.tools.register(ToolDefinition(name, "drive", {"type": "object"}, play, frozenset()))
        _, runtime, _ = await research(orch)
        assert runtime["control"] == "automated" and runtime["control_tools"] == [name]


async def test_an_invalid_adapter_config_fails_clearly(tmp_path):
    root = tmp_path / "game"
    root.mkdir()
    orch = orch_for(tmp_path, root, adapters=[{"name": "Bad Name", "source_markers": []}])
    task = await orch.submit_request(
        TaskRequest(objective="research", domain="game_research", task_type="research")
    )
    await wait_done(orch, task, 30)
    step = {r.step_id: r for r in orch.load_steps(task.run_id).values()}["inventory"]
    assert task.state == TaskState.failed and "invalid engine adapter" in str(step.error)


async def test_a_duplicate_adapter_name_is_refused(tmp_path):
    root = tmp_path / "game"
    root.mkdir()
    dup = {**CONFIGURED[0], "name": "unity"}
    orch = orch_for(tmp_path, root, adapters=[dup])
    task = await orch.submit_request(
        TaskRequest(objective="research", domain="game_research", task_type="research")
    )
    await wait_done(orch, task, 30)
    step = {r.step_id: r for r in orch.load_steps(task.run_id).values()}["inventory"]
    assert task.state == TaskState.failed and "duplicate engine adapter" in str(step.error)
