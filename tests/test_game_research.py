"""MVP acceptance, end to end on the fake game in tests/fixtures/game_fixture (criteria 1-3):
source available -> files + metadata within scope; build only -> missing capabilities + a
runtime/playtest plan; no control tool -> assisted mode with the limitation spelled out."""

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from app.config import Settings
from app.intake import TaskRequest
from app.models import TaskState
from app.tools import ToolDefinition, ToolOutput
from tests.conftest import build_orch, wait_done

FIXTURE = Path(__file__).parent / "fixtures" / "game_fixture"


def game(tmp_path, part: str):
    root = tmp_path / part
    shutil.copytree(FIXTURE / part, root)
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "var",
        workspace_root=root,
        max_attachment_bytes=1024 * 1024,
        action_ttl_s=60,
        config_dir=None,
    )
    orch, _ = build_orch(settings, [])
    return orch, root


async def research(orch, objective="research this game"):
    req = TaskRequest(objective=objective, domain="game_research", task_type="research")
    task = await orch.submit_request(req)
    await wait_done(orch, task, 30)
    assert task.state == TaskState.succeeded, task.error
    out = {r.step_id: r for r in orch.load_steps(task.run_id).values()}
    return out["inventory"].output, out["runtime"].output, out


def artifact_json(orch, artifact_id):
    return json.loads(orch.artifacts.read(orch.artifacts.get(artifact_id)))


async def test_source_game_is_collected_with_metadata_within_scope(tmp_path):
    orch, root = game(tmp_path, "source")
    outside = tmp_path / "secret.txt"
    outside.write_text("not part of the game")
    (root / "levels" / "link.txt").symlink_to(outside)  # out of scope: must not be collected
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]")

    inv, runtime, _ = await research(orch)

    assert (inv["engine"], inv["mode"], inv["marker"]) == ("fixture", "source", "game.json")
    assert inv["metadata"]["name"] == "Pip Jump" and inv["skipped"]["symlink"] == 1
    manifest = artifact_json(orch, inv["manifest_artifact"])
    paths = {f["path"] for f in manifest["files"]}
    assert paths == {
        "game.json",
        "docs/design.md",
        "levels/level1.json",
        "levels/level2.json",
        "scripts/player.lua",
    }
    for f in manifest["files"]:  # every file is evidence with its checksum
        assert f["sha256"] == hashlib.sha256((root / f["path"]).read_bytes()).hexdigest()
    assert runtime["mode"] == "source" and runtime["missing_capabilities"] == ["runtime_control"]


async def test_build_only_names_missing_capabilities_and_plans_an_assisted_playtest(tmp_path):
    orch, _ = game(tmp_path, "build")

    inv, runtime, steps = await research(orch)

    assert (inv["engine"], inv["mode"], inv["files"]) == ("fixture", "build", 2)
    assert runtime["missing_capabilities"] == ["source", "level_data", "scripts", "runtime_control"]
    assert runtime["control"] == "assisted" and runtime["control_tools"] == []
    assert any("no tool can drive fixture builds" in x for x in runtime["limitations"])
    assert any("build only" in x for x in runtime["limitations"])
    assert len(runtime["playtest_plan"]) == 3 and "operator" in runtime["playtest_plan"][0]
    report = orch.artifacts.read(orch.artifacts.get(steps["runtime"].artifact_ids[0])).decode()
    assert "control: assisted" in report and "- missing: source" in report


async def test_a_control_tool_makes_the_playtest_automated(tmp_path):
    orch, _ = game(tmp_path, "build")

    async def play(ctx, args):
        return ToolOutput("ok", {})

    orch.tools.register(
        ToolDefinition(
            "mcp.fixture.play", "Drive a fixture build.", {"type": "object"}, play, frozenset()
        )  # fmt: skip
    )

    _, runtime, _ = await research(orch)

    assert runtime["control"] == "automated" and runtime["control_tools"] == ["mcp.fixture.play"]
    assert "runtime_control" not in runtime["missing_capabilities"]
    assert not any("no tool can drive" in x for x in runtime["limitations"])


async def test_an_unknown_folder_reports_its_limits_instead_of_guessing(tmp_path):
    orch, root = game(tmp_path, "build")
    (root / "build_info.json").unlink()

    inv, runtime, _ = await research(orch)

    assert inv["engine"] is None and inv["files"] == 1
    assert runtime["control"] == "none" and runtime["playtest_plan"] == []
    assert runtime["limitations"] == [
        "no supported engine detected: only the file inventory is evidence"
    ]


@pytest.mark.parametrize(
    "objective", ["research and playtest this game", "build an evidence inventory of the game"]
)
async def test_research_requests_route_to_game_research(tmp_path, objective):
    orch, _ = game(tmp_path, "source")
    task = await orch.submit_request(TaskRequest(objective=objective))
    await wait_done(orch, task, 30)
    assert (task.domain, task.task_type) == ("game_research", "research"), task.error
