"""Workflow engine against fake connectors and a real temporary git repository."""

import asyncio
import json
import subprocess

import pytest

from app.actions import ActionError
from app.connectors.base import AgentResult, Cap, ConnectorError, UnsupportedCapability
from app.models import Task, TaskState, Workflow
from app.workspace import WorkspaceError
from tests.conftest import MODEL, WRITER, FakeConnector, build_orch, make_png, wait_done, write_file

CLEAN = {"summary": "looks fine", "findings": []}
BLOCKER = {
    "summary": "bug",
    "findings": [
        {
            "severity": "high",
            "description": "off by one",
            "location": "feature.py:1",
            "evidence": "loop bound",
            "remediation": "use <=",
        }
    ],
}


def reviewer(*responses):
    queue = list(responses)

    def h(req):
        assert req.read_only and req.json_schema
        return AgentResult("", "reviewer", "rev-model", structured=queue.pop(0))

    return FakeConnector("reviewer", MODEL, h)


async def test_ask_text_only(settings):
    model = FakeConnector("openai", MODEL, lambda r: AgentResult("42", "openai", "m"))
    orch, notes = build_orch(settings, [model])
    task = await wait_done(orch, await orch.submit("meaning of life?"))
    assert task.state == TaskState.succeeded and task.result == "42"
    assert task.provider == "openai" and task.usage[0].status == "ok"
    assert any(a.kind == "report" for a in orch.artifacts.for_task(task.task_id))
    assert "accepted" in notes.texts() and "Started" in notes.texts()


async def test_ask_with_image_routes_to_vision(settings):
    text_only = FakeConnector("deepseek", {Cap.text})
    vision = FakeConnector("openai", MODEL)
    orch, _ = build_orch(settings, [text_only, vision])
    att = orch.attachments.save(make_png(2, 2), "ref.png", "image/png", "api")
    task = await wait_done(orch, await orch.submit("describe", attachment_ids=[att.attachment_id]))
    assert task.provider == "openai" and not text_only.calls
    assert vision.calls[0].attachments[0][0].attachment_id == att.attachment_id


async def test_ask_unsupported_capability_fails_explicitly(settings):
    orch, _ = build_orch(settings, [FakeConnector("deepseek", {Cap.text})])
    att = orch.attachments.save(make_png(2, 2), "ref.png", "image/png", "api")
    task = await wait_done(orch, await orch.submit("describe", attachment_ids=[att.attachment_id]))
    assert task.state == TaskState.failed and task.error_category == "unsupported"
    assert "image_input" in task.error


async def test_attachment_isolation_between_tasks(settings):
    orch, _ = build_orch(settings, [FakeConnector("openai", MODEL)])
    att = orch.attachments.save(b"x", "a.txt", "text/plain", "api")
    await wait_done(orch, await orch.submit("one", attachment_ids=[att.attachment_id]))
    with pytest.raises(Exception, match="another task"):
        await orch.submit("two", attachment_ids=[att.attachment_id])


async def test_code_workflow_isolated_and_not_merged(settings, repo):
    writer = FakeConnector("claude_code", WRITER, write_file("feature.py", "x = 1\n"))
    orch, notes = build_orch(settings, [writer])
    task = await wait_done(orch, await orch.submit("add feature", Workflow.code))
    assert task.state == TaskState.succeeded, task.error
    ws = orch.workspaces.get(task.task_id)
    assert (ws.path / "feature.py").exists()
    assert not (repo / "feature.py").exists()  # main checkout untouched
    log = subprocess.run(
        ["git", "log", "--oneline", "main"], cwd=repo, capture_output=True, text=True
    )
    assert len(log.stdout.splitlines()) == 1  # nothing merged/committed
    diff = [a for a in orch._all_artifacts(task) if a.kind == "diff"][0]
    assert "+x = 1" in diff.path.read_text()
    assert "NOT run" in notes.texts()
    assert any(c["control"] and "Approve" in c["html"] for c in notes.calls)


async def test_approval_reject_cancels(settings):
    writer = FakeConnector("claude_code", WRITER)
    orch, _ = build_orch(settings, [writer], auto="reject")
    task = await wait_done(orch, await orch.submit("add feature", Workflow.code))
    assert task.state == TaskState.cancelled and not writer.calls


async def test_duplicate_quick_action(settings):
    writer = FakeConnector("claude_code", WRITER, write_file("a.py", ""))
    orch, notes = build_orch(settings, [writer], auto=None)
    task = await orch.submit("x", Workflow.code)
    await asyncio.sleep(0.05)
    assert task.state == TaskState.waiting_for_approval
    approve = next(a for c in notes.calls for a in c["actions"] if a.kind == "approve")
    reject = next(a for c in notes.calls for a in c["actions"] if a.kind == "reject")
    assert await orch.apply_action(approve.token, 1) == "Approved"
    with pytest.raises(ActionError):
        await orch.apply_action(approve.token, 1)
    with pytest.raises(ActionError):
        await orch.apply_action(reject.token, 1)
    await wait_done(orch, task)
    assert task.state == TaskState.succeeded and len(writer.calls) == 1


async def test_delegate_review_fix_loop(settings):
    calls = []

    def impl(req):
        calls.append(req)
        content = "for i in range(n+1): pass\n" if len(calls) > 1 else "for i in range(n): pass\n"
        return write_file("feature.py", content)(req)

    writer = FakeConnector("claude_code", WRITER, impl)
    rev = reviewer(BLOCKER, CLEAN)
    orch, notes = build_orch(settings, [writer, rev], qa_commands={"check": "true"})
    task = await wait_done(orch, await orch.submit("implement loop", Workflow.delegate_review))
    assert task.state == TaskState.succeeded, task.error
    assert len(calls) == 2 and calls[1].resume_session == "sess-1"
    assert "off by one" in calls[1].prompt
    # reviewer saw the real diff, not the implementer's summary
    assert "+for i in range(n): pass" in rev.calls[0].prompt
    assert "wrote feature.py" not in rev.calls[0].prompt
    roles = [t.role for t in orch.tasks.values() if t.parent_id == task.task_id]
    assert roles.count("implementer") == 2 and roles.count("reviewer") == 2


async def test_delegate_review_unresolved_blocker_fails(settings):
    writer = FakeConnector("claude_code", WRITER, write_file("f.py", "bad\n"))
    orch, _ = build_orch(settings, [writer, reviewer(BLOCKER, BLOCKER)])
    task = await wait_done(orch, await orch.submit("x", Workflow.delegate_review))
    assert task.state == TaskState.failed and task.error_category == "review_blocked"
    assert task.findings[0].severity == "high"


async def test_review_prefers_independent_provider(settings):
    writer = FakeConnector("claude_code", WRITER | {Cap.structured_output}, write_file("f.py", "1"))
    rev = reviewer(CLEAN)
    settings.reviewer_preference = "claude_code,reviewer"
    orch, _ = build_orch(settings, [writer, rev])
    await wait_done(orch, await orch.submit("x", Workflow.delegate_review))
    assert len(rev.calls) == 1 and len(writer.calls) == 1


async def test_qa_failure_reported(settings):
    writer = FakeConnector("claude_code", WRITER, write_file("f.py", "1"))
    orch, notes = build_orch(
        settings, [writer], qa_commands={"build": "sh -c 'echo BUILD FAILED; exit 1'"}
    )
    task = await wait_done(orch, await orch.submit("x", Workflow.code))
    assert task.state == TaskState.failed and task.error_category == "qa_failed"
    assert "BUILD FAILED" in notes.texts()


async def test_parallel_separate_workspaces(settings):
    async def slow_write(req):
        await asyncio.sleep(0.05)
        return write_file("out.txt", str(req.workspace))(req)

    writer = FakeConnector("claude_code", WRITER, slow_write)
    orch, _ = build_orch(settings, [writer])
    task = await wait_done(orch, await orch.submit("x", Workflow.parallel))
    assert task.state == TaskState.succeeded, task.error
    dirs = {str(c.workspace) for c in writer.calls}
    assert len(dirs) == 2
    comparison = [
        a for a in orch.artifacts.for_task(task.task_id) if a.filename == "comparison.json"
    ]
    assert len(json.loads(comparison[0].path.read_text())) == 2


async def test_single_writer_per_workspace(settings):
    orch, _ = build_orch(settings, [])
    ws = await orch.workspaces.create("tsk_lock")
    orch.workspaces.acquire_write(ws, "a")
    with pytest.raises(WorkspaceError, match="being written"):
        orch.workspaces.acquire_write(ws, "b")
    with pytest.raises(WorkspaceError, match="in use"):
        await orch.workspaces.remove("tsk_lock")
    orch.workspaces.release_write(ws, "a")
    orch.workspaces.acquire_write(ws, "b")


async def test_stop_running_task(settings):
    started = asyncio.Event()

    async def hang(req):
        started.set()
        await asyncio.sleep(30)

    model = FakeConnector("openai", MODEL, hang)
    orch, _ = build_orch(settings, [model])
    task = await orch.submit("slow")
    await asyncio.wait_for(started.wait(), 5)
    assert await orch.stop(task.task_id) is True
    assert task.state == TaskState.cancelled and model.cancelled == [task.task_id]
    assert await orch.stop(task.task_id) is False


async def test_failure_retry_and_limit(settings):
    def boom(req):
        raise ConnectorError("upstream 500", "provider", retryable=True)

    orch, notes = build_orch(settings, [FakeConnector("openai", MODEL, boom)])
    task = await wait_done(orch, await orch.submit("x"))
    assert task.state == TaskState.failed and task.usage[0].error_category == "provider"
    with pytest.raises(KeyError):
        await orch.retry("nope")
    t2 = await wait_done(orch, await orch.retry(task.task_id))
    assert t2.attempt == 2 and t2.correlation_id == task.correlation_id
    t3 = await wait_done(orch, await orch.retry(t2.task_id))
    with pytest.raises(ValueError, match="limit"):
        await orch.retry(t3.task_id)


async def test_timeout_outcome_unknown(settings):
    def timeout(req):
        raise ConnectorError("timed out", "timeout", outcome="unknown")

    orch, _ = build_orch(settings, [FakeConnector("openai", MODEL, timeout)])
    task = await wait_done(orch, await orch.submit("x"))
    assert task.state == TaskState.unknown


async def test_dependency_ordering_enforced(settings):
    orch, _ = build_orch(settings, [])
    parent = Task(title="p", description="d")
    orch.tasks[parent.task_id] = parent
    dep = orch._child(parent, "designer", "design")
    with pytest.raises(ValueError, match="dependency"):
        orch._child(parent, "implementer", "impl", [dep])
    dep.transition(TaskState.succeeded)
    assert orch._child(parent, "implementer", "impl", [dep]).depends_on == [dep.task_id]


async def test_workspace_workflow_requires_root(settings):
    settings.workspace_root = None
    orch, _ = build_orch(settings, [FakeConnector("claude_code", WRITER)])
    with pytest.raises(UnsupportedCapability, match="WORKSPACE_ROOT"):
        await orch.submit("x", Workflow.code)


class FakeImages:
    name = "fake_images"
    model = "img-1"
    verification = "mocked"

    def __init__(self):
        self.prompts = []

    async def generate(self, prompt, size, transparent):
        self.prompts.append((prompt, size, transparent))
        w, h = map(int, size.split("x"))
        return make_png(w, h, alpha=transparent)


async def test_design_implement_review(settings):
    design = {
        "acceptance_criteria": ["coin spins"],
        "design_spec": "A spinning coin",
        "assets": [
            {"name": "coin", "prompt": "gold coin", "width": 32, "height": 32, "transparent": True}
        ],
    }
    designer = FakeConnector(
        "openai", MODEL, lambda r: AgentResult("", "openai", "m", structured=design)
    )
    seen = {}

    def impl(req):
        seen["asset"] = (req.workspace / "Assets/Generated/coin.png").read_bytes()[:4]
        seen["prompt"] = req.prompt
        return write_file("Coin.cs", "class Coin {}\n")(req)

    writer = FakeConnector("claude_code", WRITER, impl)
    images = FakeImages()
    settings.reviewer_preference = "reviewer"
    orch, notes = build_orch(settings, [designer, writer, reviewer(CLEAN)], image=images)
    att = orch.attachments.save(make_png(4, 4), "ref.png", "image/png", "api")
    task = await orch.submit(
        "make a coin", Workflow.design_implement_review, attachment_ids=[att.attachment_id]
    )
    await wait_done(orch, task)
    assert task.state == TaskState.succeeded, task.error
    assert images.prompts == [("gold coin", "32x32", True)]
    assert seen["asset"] == b"\x89PNG" and "coin spins" in seen["prompt"]
    manifest = [a for a in orch._all_artifacts(task) if a.kind == "manifest"][0]
    entry = json.loads(manifest.path.read_text())["assets"][0]
    assert entry["validation_status"] == "passed" and entry["approval_status"] == "approved"
    assert entry["reference_ids"] == [att.attachment_id]
    steps = {t.role: t for t in orch.tasks.values() if t.parent_id == task.task_id}
    assert steps["implementer"].depends_on == [
        steps["designer"].task_id,
        steps["asset_processor"].task_id,
    ]


async def test_design_without_image_provider_reports_missing(settings):
    design = {
        "acceptance_criteria": [],
        "design_spec": "s",
        "assets": [{"name": "coin", "prompt": "p", "width": 8, "height": 8, "transparent": True}],
    }
    designer = FakeConnector(
        "openai", MODEL, lambda r: AgentResult("", "openai", "m", structured=design)
    )
    writer = FakeConnector("claude_code", WRITER, write_file("a.cs", ""))
    settings.reviewer_preference = "reviewer"
    orch, notes = build_orch(settings, [designer, writer, reviewer(CLEAN)])
    task = await wait_done(orch, await orch.submit("x", Workflow.design_implement_review))
    assert "No image-generation provider" in notes.texts()
    assert "missing asset: coin" in notes.texts()
    assert task.state == TaskState.succeeded
