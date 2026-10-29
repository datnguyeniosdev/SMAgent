"""Game Intelligence Phase 1: entities, immutable evidence with provenance, observations vs claims,
claim status rules, project/build registry confinement, and research data kept out of retention."""

import json
import os
from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.models import Task, TaskState, now
from app.plugins.game_intel.models import (
    SCHEMA_VERSION,
    Claim,
    ClaimEvidenceLink,
    EvidenceArtifact,
    Observation,
)
from app.plugins.game_intel.repository import (
    EvidenceRepository,
    GameIntelError,
    ProjectRegistry,
    manifest_fingerprint,
)
from tests.test_config_gateway import make


@pytest.fixture
def env(settings, cfg_dir, tmp_path):
    orch, _ = make(settings, cfg_dir, [])
    games = tmp_path / "games"
    (games / "mario").mkdir(parents=True)
    run = Task(title="research", description="d", domain="game_research", task_type="research")
    orch.tasks[run.task_id] = run
    orch.store.put("task", run.task_id, run, task_id=run.task_id, state=run.state.value)
    reg = ProjectRegistry(orch.store, [games])
    repo = EvidenceRepository(orch.store, orch.artifacts)
    project = reg.register_project("Mario", games / "mario")
    return orch, reg, repo, project, run


def test_entities_round_trip_with_their_schema_version():
    ev = EvidenceArtifact(project_id="gpr_1", run_id="tsk_1", evidence_type="screenshot", method="operator",
                          artifact_id="art_1", sha256="0" * 64, size_bytes=3)  # fmt: skip
    again = EvidenceArtifact.model_validate(json.loads(ev.model_dump_json()))
    assert again == ev and again.schema_version == SCHEMA_VERSION
    with pytest.raises(ValidationError):  # an observation always cites evidence
        Observation(project_id="p", run_id="r", statement="jump is high", evidence_ids=[])
    with pytest.raises(ValidationError):  # a link cites exactly one target
        ClaimEvidenceLink(claim_id="c", relation="supports")
    with pytest.raises(ValidationError):
        EvidenceArtifact(project_id="p", run_id="r", evidence_type="guess", method="m", artifact_id="a",
                         sha256="0", size_bytes=1)  # fmt: skip


def test_evidence_is_checksummed_immutable_and_registered_once(env):
    orch, _, repo, project, run = env
    kw = dict(project_id=project.project_id, run_id=run.task_id, evidence_type="level_data",
              method="inventory", filename="level1.json", data='{"w": 12}',
              provenance={"path": "levels/level1.json", "revision": "abc123"})  # fmt: skip
    ev = repo.register(**kw)
    assert len(ev.sha256) == 64 and ev.provenance["revision"] == "abc123" and ev.version == 1
    assert repo.register(**kw).evidence_id == ev.evidence_id  # a retried collection adds nothing
    assert len(orch.artifacts.for_task(run.task_id)) == 1
    assert repo.read(ev.evidence_id) == b'{"w": 12}'

    fixed = repo.correct(ev.evidence_id, '{"w": 13}', reason="re-exported with the editor")
    assert fixed.derived_from == ev.evidence_id and fixed.version == 2
    assert repo.read(ev.evidence_id) == b'{"w": 12}'  # the original stays as it was
    assert {e.evidence_id for e in repo.for_run(run.task_id)} == {ev.evidence_id, fixed.evidence_id}
    with pytest.raises(GameIntelError):
        repo.correct(ev.evidence_id, "x", reason=" ")

    art = orch.artifacts.get(ev.artifact_id)
    os.chmod(art.path, 0o644)
    art.path.write_bytes(b'{"w": 99}')  # changed on disk behind the store's back
    with pytest.raises(Exception, match="changed on disk"):
        repo.read(ev.evidence_id)


def test_observations_cite_evidence_and_claims_move_only_as_their_links_allow(env):
    _, reg, repo, project, run = env
    pid = project.project_id
    shot = repo.register(project_id=pid, run_id=run.task_id, evidence_type="screenshot",
                         method="capture.screenshot", filename="a.png", data=b"png1")  # fmt: skip
    data = repo.register(project_id=pid, run_id=run.task_id, evidence_type="level_data",
                         method="inventory", filename="l.json", data=b"{}")  # fmt: skip
    ob = repo.observe(project_id=pid, run_id=run.task_id, statement="Level 1 has 3 coins",
                      evidence_ids=[shot.evidence_id], scope={"level": "1"})  # fmt: skip
    c = repo.claim(project_id=pid, statement="Coins grant extra lives at 100", scope={"level": "*"})
    assert c.status == "hypothesis" and repo.store.get("game_observation", c.claim_id) is None

    with pytest.raises(GameIntelError, match="needs a supporting link"):
        repo.transition(c.claim_id, "supported", "x")
    repo.link(c.claim_id, "supports", observation_id=ob.observation_id)
    assert repo.transition(c.claim_id, "supported", "seen in play").status == "supported"
    with pytest.raises(GameIntelError, match="two independent methods"):
        repo.transition(c.claim_id, "verified", "x")
    repo.link(c.claim_id, "supports", evidence_id=data.evidence_id)
    assert repo.transition(c.claim_id, "verified", "level data agrees").status == "verified"

    repo.link(c.claim_id, "contradicts", evidence_id=data.evidence_id, note="v2 data says 50")
    assert repo.transition(c.claim_id, "unresolved", "conflict").status == "unresolved"
    with pytest.raises(GameIntelError, match="needs a reason"):
        repo.transition(c.claim_id, "hypothesis", "  ")
    final = repo.transition(c.claim_id, "hypothesis", "re-test with build 2")
    assert [h["to"] for h in final.history] == ["supported", "verified", "unresolved", "hypothesis"]

    other = reg.register_project("Other", reg.roots[0] / "mario")  # (same root: the same project)
    assert other.project_id == pid
    with pytest.raises(GameIntelError):
        repo.observe(project_id="gpr_elsewhere", run_id=run.task_id, statement="x happens",
                     evidence_ids=[shot.evidence_id])  # fmt: skip


def test_claim_transition_rules_without_storage():
    from app.plugins.game_intel.models import claim_transition

    c = Claim(project_id="p", statement="jump is variable")
    sup = ClaimEvidenceLink(claim_id=c.claim_id, relation="supports", evidence_id="e1", method="a")
    con = ClaimEvidenceLink(
        claim_id=c.claim_id, relation="contradicts", evidence_id="e2", method="b"
    )
    assert claim_transition(c, "verified", [sup]) == "only a supported claim becomes verified"
    assert claim_transition(c, "contradicted", [sup]) == "needs a contradicting link"
    assert claim_transition(c, "contradicted", [sup, con]) is None
    assert (
        claim_transition(c, "supported", [sup, con]) is not None
    )  # contradicted support is not support


def test_projects_and_builds_stay_inside_approved_roots(env, tmp_path):
    _, reg, _, project, _ = env
    (tmp_path / "secret").mkdir()
    with pytest.raises(GameIntelError, match="outside"):
        reg.register_project("x", tmp_path / "secret")
    (reg.roots[0] / "link").symlink_to(tmp_path / "secret")
    with pytest.raises(GameIntelError, match="outside"):  # an escape through a symlink
        reg.register_project("x", reg.roots[0] / "link")
    with pytest.raises(GameIntelError, match="outside"):
        reg.register_project("x", reg.roots[0] / ".." / "secret")

    files = [{"path": "a.txt", "sha256": "1" * 64}, {"path": "b.txt", "sha256": "2" * 64}]
    b1 = reg.register_build(project.project_id, "source", project.root, files, revision="abc")
    b2 = reg.register_build(project.project_id, "source", project.root, list(reversed(files)))
    assert b1.build_id == b2.build_id and b1.fingerprint == manifest_fingerprint(files)
    b3 = reg.register_build(project.project_id, "source", project.root, files[:1])
    assert b3.build_id != b1.build_id

    assert project.engine is None  # never guessed from the folder name
    p = reg.set_engine(project.project_id, "unity", "gev_marker")
    assert (p.engine, p.engine_evidence) == ("unity", "gev_marker")


def test_research_runs_and_their_evidence_survive_the_retention_pass(env):
    orch, _, repo, project, run = env
    ev = repo.register(project_id=project.project_id, run_id=run.task_id, evidence_type="log",
                       method="read_logs", filename="log.txt", data="boot ok")  # fmt: skip
    other = Task(title="chat", description="d", state=TaskState.succeeded)
    for t in (run, other):
        t.state = TaskState.succeeded
        t.updated_at = now() - timedelta(days=5)
        orch.tasks[t.task_id] = t
        orch.store.put("task", t.task_id, t, task_id=t.task_id, state=t.state.value)
    orch.artifacts.add(other.task_id, "report", "r.md", "old")
    for art in orch.artifacts.all():
        art.created_at = now() - timedelta(days=5)

    orch.purge_old(1)

    assert other.task_id not in orch.tasks and run.task_id in orch.tasks
    assert orch.store.get("game_evidence", ev.evidence_id) is not None
    assert repo.read(ev.evidence_id) == b"boot ok"  # its artifact was not cleaned up
    assert orch.artifacts.for_task(other.task_id) == []
