"""Models, storage, actions, formatting, routing, assets, QA."""

import os

import pytest

from app import formatting as fmt
from app.actions import ActionError, ActionRegistry
from app.assets import AssetRules, check_manifest, inspect_image, manifest_entry
from app.connectors.base import Cap, UnsupportedCapability
from app.models import InvalidTransition, ReviewFinding, Task, TaskState
from app.qa import QARunner
from app.router import Router
from app.storage import AttachmentError, AttachmentStore
from tests.conftest import MODEL, WRITER, FakeConnector, make_png

# ---------- task states ----------


def test_valid_and_invalid_transitions():
    t = Task(title="t", description="d")
    t.transition(TaskState.queued)
    t.transition(TaskState.running)
    t.transition(TaskState.waiting_for_approval)
    t.transition(TaskState.running)
    t.transition(TaskState.succeeded)
    with pytest.raises(InvalidTransition):
        t.transition(TaskState.running)
    with pytest.raises(InvalidTransition):
        Task(title="t", description="d").transition(TaskState.succeeded)


# ---------- attachments ----------


@pytest.fixture
def store(tmp_path):
    return AttachmentStore(tmp_path / "media", max_bytes=1000)


def test_attachment_valid_png_and_text(store):
    png = store.save(make_png(2, 2), "a.png", "image/png", "api", task_id="tsk_1")
    txt = store.save(b"hello", "notes.md", "text/markdown", "api", task_id="tsk_1")
    assert png.is_image and not txt.is_image
    assert png.storage_path.name.startswith(png.attachment_id)
    assert store.read(png).startswith(b"\x89PNG")


@pytest.mark.parametrize(
    "data,name,mime,err",
    [
        (b"not a png", "x.png", "image/png", "does not match"),
        (b"\x89PNG\r\n\x1a\n", "x.exe", "application/x-msdownload", "not permitted"),
        (b"x" * 1001, "big.txt", "text/plain", "exceeds"),
        (b"\xff\xfe\x00bad", "bin.txt", "text/plain", "does not match"),
        (b"", "e.txt", "text/plain", "empty"),
    ],
)
def test_attachment_rejections(store, data, name, mime, err):
    with pytest.raises(AttachmentError, match=err):
        store.save(data, name, mime, "api")


def test_attachment_path_traversal(store):
    att = store.save(b"x", "../../../etc/passwd", "text/plain", "api")
    assert att.filename == "passwd"
    assert att.storage_path.is_relative_to(store.root.resolve())
    with pytest.raises(AttachmentError):
        store.save(b"x", "a.txt", "text/plain", "api", task_id="../escape")


def test_attachment_task_isolation(store):
    att = store.save(b"x", "a.txt", "text/plain", "api", task_id="tsk_a")
    with pytest.raises(AttachmentError, match="another task"):
        store.get(att.attachment_id, task_id="tsk_b")
    inbox = store.save(b"y", "b.txt", "text/plain", "api")
    store.bind(inbox.attachment_id, "tsk_b")
    with pytest.raises(AttachmentError):
        store.bind(inbox.attachment_id, "tsk_c")


def test_attachment_symlink_refused(store, tmp_path):
    att = store.save(b"x", "a.txt", "text/plain", "api")
    secret = tmp_path / "secret.txt"
    secret.write_text("secret")
    os.remove(att.storage_path)
    os.symlink(secret, att.storage_path)
    with pytest.raises(AttachmentError):
        store.read(att)


def test_a_changed_input_or_artifact_is_refused_by_its_checksum(store, tmp_path):
    """Evidence integrity: an input or artifact edited on disk after it was stored is not read."""
    from app.storage import ArtifactStore

    att = store.save(b"speed = 6", "a.txt", "text/plain", "api")
    assert store.read(att) == b"speed = 6"
    att.storage_path.write_bytes(b"speed = 9")
    with pytest.raises(AttachmentError, match="content changed on disk"):
        store.read(att)
    arts = ArtifactStore(tmp_path / "artifacts")
    art = arts.add("tsk_a", "manifest", "m.json", "{}")
    art.path.write_text('{"files": []}')
    with pytest.raises(AttachmentError, match="content changed on disk"):
        arts.read(art)


def test_cleanup_skips_in_use(store):
    att = store.save(b"x", "a.txt", "text/plain", "api", task_id="tsk_a")
    assert store.cleanup_task("tsk_a", in_use={"tsk_a"}) is False
    assert att.storage_path.exists()
    assert store.cleanup_task("tsk_a", in_use=set()) is True
    assert not att.storage_path.exists()


# ---------- quick actions ----------


def test_action_single_use_expiry_state():
    clock = [0.0]
    reg = ActionRegistry(ttl_s=10, clock=lambda: clock[0])
    a = reg.create("approve", "t1", "Approve", {TaskState.waiting_for_approval}, group="g")
    b = reg.create("reject", "t1", "Reject", {TaskState.waiting_for_approval}, group="g")
    assert len(f"a:{a.token}".encode()) <= 64
    with pytest.raises(ActionError, match="not applicable"):
        reg.consume(a.token, 1, TaskState.running)
    reg.consume(a.token, 1, TaskState.waiting_for_approval)
    with pytest.raises(ActionError, match="already used"):
        reg.consume(a.token, 1, TaskState.waiting_for_approval)
    with pytest.raises(ActionError, match="already used"):  # sibling in same group
        reg.consume(b.token, 1, TaskState.waiting_for_approval)
    c = reg.create("retry", "t1", "Retry", {TaskState.failed})
    clock[0] = 11
    with pytest.raises(ActionError, match="expired"):
        reg.consume(c.token, 1, TaskState.failed)
    with pytest.raises(ActionError, match="unknown"):
        reg.consume("forged", 1, TaskState.failed)


# ---------- formatting ----------


def test_formatting_escapes_and_fits():
    assert (
        fmt.code("<b>&", "python")
        == '<pre><code class="language-python">&lt;b&gt;&amp;</code></pre>'
    )
    assert '"a": 1' in fmt.json_block({"a": 1})
    assert fmt.log_block("x" * 5000, 100).startswith("<pre><code>…")
    msg, overflow = fmt.fit("<b>x</b>" * 1000)
    assert len(msg) <= fmt.MESSAGE_LIMIT and overflow
    msg, overflow = fmt.fit("short")
    assert overflow is None
    assert fmt.plain("<b>a &amp; b</b>") == "a & b"
    f = ReviewFinding(severity="high", description="<bad>", location="a.py:1", evidence="e")
    assert "&lt;bad&gt;" in fmt.findings_report([f])


# ---------- router ----------


def test_router_capabilities_and_no_silent_downgrade():
    text_only = FakeConnector("deepseek", {Cap.text})
    vision = FakeConnector("openai", MODEL)
    writer = FakeConnector("claude_code", WRITER)
    r = Router([text_only, vision, writer])
    assert r.route({Cap.text}, ["deepseek"]).name == "deepseek"
    assert r.route({Cap.text, Cap.image_input}, ["deepseek"]).name == "openai"
    with pytest.raises(UnsupportedCapability, match="lacks: image_input"):
        r.route({Cap.image_input}, explicit="deepseek")
    vision.available = False
    with pytest.raises(UnsupportedCapability):
        r.route({Cap.image_input, Cap.text})
    assert r.route({Cap.text}, ["claude_code"], exclude={"claude_code"}).name == "deepseek"


# ---------- assets ----------


def test_png_inspection_and_validation():
    info = inspect_image(make_png(64, 32, alpha=True))
    assert (info.format, info.width, info.height, info.has_alpha) == ("png", 64, 32, True)
    opaque = inspect_image(make_png(30, 30, alpha=False))
    assert opaque.has_alpha is False
    rules = AssetRules(require_alpha=True, power_of_two=True, expected_width=64, expected_height=64)
    e = manifest_entry("a1", "t", "hero", make_png(30, 30, False), "/x", rules, [])
    assert e.validation_status == "failed"
    assert any("alpha" in i for i in e.validation_issues)
    assert any("power-of-two" in i for i in e.validation_issues)
    assert any("expected" in i for i in e.validation_issues)


def test_manifest_duplicates_and_missing():
    rules = AssetRules(require_alpha=False)
    png = make_png(4, 4)
    a = manifest_entry("a1", "t", "coin", png, "/a", rules, [])
    b = manifest_entry("a2", "t", "gem", png, "/b", rules, [])
    problems = check_manifest([a, b], ["coin", "gem", "chest"])
    assert "gem duplicates coin" in problems
    assert "missing asset: chest" in problems


# ---------- QA runner ----------


async def test_qa_pass_fail_timeout(tmp_path):
    qa = QARunner(
        {"ok": "true", "fail": "sh -c 'echo boom; exit 3'", "slow": "sleep 5"},
        timeout_s=0.5,
        max_output_bytes=100,
    )
    ok, fail, slow = await qa.run_all(tmp_path)
    assert ok.passed
    assert not fail.passed and fail.exit_code == 3 and "boom" in fail.output_tail
    assert slow.timed_out and not slow.passed
    with pytest.raises(KeyError):
        await qa.run("rm -rf /", tmp_path)


async def test_qa_output_capped(tmp_path):
    qa = QARunner({"spam": "sh -c 'yes | head -c 50000; echo END'"}, max_output_bytes=100)
    r = await qa.run("spam", tmp_path)
    assert len(r.output_tail) <= 100 and r.output_tail.endswith("END\n")


def test_qa_docker_argv(tmp_path):
    qa = QARunner({"t": "pytest -q"}, sandbox="docker", docker_image="python:3.12")
    argv = qa.argv("t", tmp_path)
    assert argv[:5] == ["docker", "run", "--rm", "--network", "none"]
    assert argv[-2:] == ["pytest", "-q"]
