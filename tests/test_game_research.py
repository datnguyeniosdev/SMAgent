"""MVP acceptance, end to end on the fake game in tests/fixtures/game_fixture (criteria 1-5):
source available -> files + metadata within scope; build only -> missing capabilities + a
runtime/playtest plan; no control tool -> assisted mode with the limitation spelled out;
changed evidence -> caught by its checksum; two sources disagree -> both kept in a conflict record;
levels not fully observed -> coverage and unknowns, never an invented map; a new design -> saved in
the creative workspace, research evidence untouched."""

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


async def research(orch, objective="research this game", baseline=None, captures=()):
    atts = [orch.attachments.save(data, name, mime, "api") for name, data, mime in captures]
    req = TaskRequest(
        objective=objective,
        domain="game_research",
        task_type="research",
        artifact_ids=[baseline] if baseline else [],
        attachment_ids=[a.attachment_id for a in atts],
    )
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


async def test_conflicting_sources_are_both_kept_in_a_conflict_record(tmp_path):
    """docs/design.md says speed 5, scripts/player.lua says speed = 6; jump 3 agrees in both."""
    orch, root = game(tmp_path, "source")
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}

    inv, _, steps = await research(orch)

    ev = steps["evidence"].output
    assert ev["integrity"]["ok"] and [c["fact"] for c in ev["conflicts"]] == ["speed"]
    claims = {(c["source"], c["value"]): c for c in ev["conflicts"][0]["claims"]}
    assert set(claims) == {("docs/design.md", "5"), ("scripts/player.lua", "6")}
    sha = {f["path"]: f["sha256"] for f in artifact_json(orch, inv["manifest_artifact"])["files"]}
    for (source, _), c in claims.items():  # each claim points at checksummed evidence
        assert c["sha256"] == sha[source] and c["quote"] and c["line"] >= 1
    assert ev["conflicts"][0]["status"] == "open"  # recorded, not resolved by picking a side
    record = artifact_json(orch, ev["evidence_artifact"])
    assert record["conflicts"] == ev["conflicts"]
    assert {(f["fact"], f["value"]) for f in record["facts"]} >= {("jump", "3"), ("speed", "5")}
    assert {
        p: p.read_bytes() for p in root.rglob("*") if p.is_file()
    } == before  # evidence untouched


async def test_changed_evidence_is_caught_by_its_checksum(tmp_path):
    orch, root = game(tmp_path, "source")
    first, _, _ = await research(orch)
    (root / "scripts" / "player.lua").write_text("local Player = {speed = 9, jump = 3}\n")
    (root / "docs" / "design.md").unlink()
    (root / "levels" / "level3.json").write_text('{"id": "level3"}')

    _, _, steps = await research(orch, baseline=first["manifest_artifact"])

    integrity = steps["evidence"].output["integrity"]
    assert integrity == {
        "ok": False,
        "changed": ["scripts/player.lua"],
        "missing": ["docs/design.md"],
        "added": ["levels/level3.json"],
        "baseline": first["manifest_artifact"],
    }
    record = artifact_json(orch, steps["evidence"].output["evidence_artifact"])
    assert not any(
        f["source"] == "scripts/player.lua" for f in record["facts"]
    )  # not used as evidence


async def test_a_manifest_edited_on_disk_fails_the_run(tmp_path):
    orch, _ = game(tmp_path, "source")
    first, _, _ = await research(orch)
    art = orch.artifacts.get(first["manifest_artifact"])
    art.path.write_text('{"files": []}')  # someone rewrote the baseline

    req = TaskRequest(objective="research this game", domain="game_research", task_type="research",
                      artifact_ids=[first["manifest_artifact"]])  # fmt: skip
    task = await orch.submit_request(req)
    await wait_done(orch, task, 30)
    step = {r.step_id: r for r in orch.load_steps(task.run_id).values()}["evidence"]
    assert task.state == TaskState.failed and "content changed on disk" in str(step.error)


def capture(name):
    return name, (FIXTURE / "captures" / name).read_bytes(), "application/json"


async def test_partly_observed_levels_report_coverage_and_unknowns_without_inventing(tmp_path):
    orch, _ = game(tmp_path, "source")
    shot = ("shot.png", b"\x89PNG\r\n\x1a\n" + b"0" * 32, "image/png")

    _, _, steps = await research(
        orch, captures=[capture("level1_a.json"), capture("level1_b.json"), shot]
    )

    cov = steps["coverage"].output
    (lv,) = cov["levels"]
    assert lv["level"] == "level1" and lv["sources"] == ["level1_a.json", "level1_b.json"]
    # Only what the captures show: (4,2) was never seen and stays "?", (2,2) is disputed.
    assert lv["map"] == ["...C..", "..C..G", "##!#?#"]
    assert lv["unknown"] == [[4, 2]] and (lv["known"], lv["coverage"]) == (16, 0.889)
    assert lv["disputed"] == [
        {
            "cell": [2, 2],
            "readings": [
                {"value": "#", "sources": ["level1_a.json"]},
                {"value": "^", "sources": ["level1_b.json"]},
            ],
        }
    ]
    assert cov["unobserved_levels"] == ["level2"] and cov["untranscribed"] == ["shot.png"]
    assert any("never observed: level2" in x for x in cov["limitations"])
    assert any("level1: 89% observed, 1 cells unknown, 1 disputed" in x for x in cov["limitations"])
    report = orch.artifacts.read(orch.artifacts.get(steps["coverage"].artifact_ids[1])).decode()
    assert "##!#?#" in report


async def test_a_build_with_no_level_list_says_the_level_count_is_unknown(tmp_path):
    orch, _ = game(tmp_path, "build")

    _, _, steps = await research(orch)

    cov = steps["coverage"].output
    assert cov["levels"] == [] and cov["expected_levels"] is None
    assert any("does not say how many levels exist" in x for x in cov["limitations"])


async def test_captures_of_one_level_with_different_sizes_fail_instead_of_merging(tmp_path):
    orch, _ = game(tmp_path, "source")
    bad = (
        "bad.json",
        b'{"level": "level1", "width": 4, "height": 3, "rows": ["...."]}',
        "application/json",
    )
    atts = [orch.attachments.save(d, n, m, "api") for n, d, m in [capture("level1_a.json"), bad]]
    req = TaskRequest(
        objective="research",
        domain="game_research",
        task_type="research",
        attachment_ids=[a.attachment_id for a in atts],
    )
    task = await orch.submit_request(req)
    await wait_done(orch, task, 30)
    step = {r.step_id: r for r in orch.load_steps(task.run_id).values()}["coverage"]
    assert task.state == TaskState.failed and "size differs" in str(step.error)


def snapshot(*roots):
    return {p: p.read_bytes() for r in roots for p in Path(r).rglob("*") if p.is_file()}


async def design(orch, title, content, refs=()):
    req = TaskRequest(
        objective=f"new creative design: {title}",
        domain="game_research",
        task_type="design",
        artifact_ids=list(refs),
        data={"title": title, "content": content},
    )
    task = await orch.submit_request(req)
    await wait_done(orch, task, 30)
    return task, {r.step_id: r for r in orch.load_steps(task.run_id).values()}


async def test_a_new_design_goes_to_the_creative_workspace_and_leaves_evidence_untouched(tmp_path):
    orch, root = game(tmp_path, "source")
    inv, _, steps = await research(orch)
    refs = [inv["manifest_artifact"], steps["evidence"].output["evidence_artifact"]]
    before = snapshot(root, orch.artifacts.root)

    task, out = await design(orch, "Pip Jump: ice levels", "Slippery tiles, speed 7 on ice.", refs)

    assert task.state == TaskState.succeeded, task.error
    d = out["design"].output
    path = Path(d["path"])
    assert path.parent == tmp_path / "var" / "creative" / "source"
    assert path.name == "pip-jump-ice-levels.md" and d["version"] == 1
    text = path.read_text()
    assert text.endswith("Slippery tiles, speed 7 on ice.\n")
    for ref in refs:  # cites research by id and checksum
        art = orch.artifacts.get(ref)
        assert art.sha256 in text and {"artifact_id": ref, "filename": art.filename,
                                        "sha256": art.sha256} in d["references"]  # fmt: skip
    assert d["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    after = snapshot(root, orch.artifacts.root)
    assert {p: after.get(p) for p in before} == before  # game and evidence unchanged
    assert not [p for p in after if p not in before and p.is_relative_to(root)]
    for ref in refs:
        orch.artifacts.read(orch.artifacts.get(ref))  # still verifies against its checksum

    _, again = await design(orch, "Pip Jump: ice levels", "Second take.")
    assert again["design"].output["version"] == 2 and path.read_text() == text  # never overwritten


async def test_a_creative_workspace_inside_the_game_or_evidence_is_refused(tmp_path):
    for inside in ("game", "artifacts"):
        root = tmp_path / inside / "src"
        shutil.copytree(FIXTURE / "source", root)
        target = root / "designs" if inside == "game" else tmp_path / inside / "var" / "artifacts"
        settings = Settings(
            _env_file=None,
            data_dir=tmp_path / inside / "var",
            workspace_root=root,
            max_attachment_bytes=1024 * 1024,
            action_ttl_s=60,
            config_dir=None,
            plugin_config={"game_research": {"creative_root": str(target)}},
        )
        orch, _ = build_orch(settings, [])
        task, out = await design(orch, "x", "y")
        assert task.state == TaskState.failed and "must be outside" in str(out["design"].error)
        assert not (root / "designs").exists()


async def test_a_design_citing_changed_evidence_is_refused(tmp_path):
    orch, _ = game(tmp_path, "source")
    inv, _, _ = await research(orch)
    art = orch.artifacts.get(inv["manifest_artifact"])
    art.path.write_text("{}")

    task, out = await design(orch, "x", "y", [inv["manifest_artifact"]])

    assert task.state == TaskState.failed and "content changed on disk" in str(out["design"].error)
    assert not (tmp_path / "var" / "creative" / "source" / "x.md").exists()


@pytest.mark.timeout(120)  # two real gateway processes
def test_an_interrupted_research_run_resumes_from_its_checkpoint_without_damage(tmp_path):
    """Criterion 6: kill -9 while the runtime step runs; the restart resumes only that step, keeps
    the checkpointed inventory/evidence/coverage (not re-run), and every artifact still verifies."""
    import os
    import subprocess
    import sys

    root = tmp_path / "game"
    shutil.copytree(FIXTURE / "source", root)
    before = snapshot(root)
    marker = tmp_path / "m"
    marker.mkdir()

    def run(phase):
        p = subprocess.run(
            [sys.executable, "tests/fixtures/game_research_driver.py", phase, str(tmp_path / "var"),
             str(root)],
            capture_output=True, text=True, timeout=120,
            env={**os.environ, "MARKER_DIR": str(marker), "PYTHONPATH": "."},
        )  # fmt: skip
        lines = [ln for ln in p.stdout.splitlines() if ln.startswith("{")]
        return p.returncode, json.loads(lines[-1]) if lines else None, p.stderr

    code, crashed, err = run("crash")
    assert code == 9, err[-2000:]
    assert crashed["steps"]["runtime"] == "running"
    code, out, err = run("recover")
    assert code == 0, err[-2000:]

    t = out["tasks"][crashed["task"]]
    assert t["state"] == "succeeded" and set(t["steps"].values()) == {"succeeded"}
    assert t["attempts"] == {"inventory": 1, "evidence": 1, "coverage": 1, "runtime": 2}
    assert (marker / "runtime.log").read_text() == "x"  # only the crashed attempt hit the slow path
    assert t["artifacts"].count("evidence_manifest.json") == 1  # checkpointed work not redone
    assert (
        t["outputs"]["evidence"]["integrity"]["ok"] and t["outputs"]["runtime"]["mode"] == "source"
    )
    assert snapshot(root) == before  # the game folder is untouched
    assert {r["action"] for r in out["recovery"]} == {"resumed workflow"}
