"""Shared fixtures. Everything here is mocked: no network, no paid API calls."""

import asyncio
import shutil
import subprocess
import zlib
from pathlib import Path

import pytest

from app.actions import ActionRegistry
from app.config import Settings
from app.connectors.base import AgentResult, Cap
from app.orchestrator import Orchestrator
from app.qa import QARunner
from app.router import Router
from app.storage import ArtifactStore, AttachmentStore
from app.workspace import WorkspaceManager


def make_png(w: int, h: int, alpha: bool = True) -> bytes:
    import struct

    ctype = 6 if alpha else 2
    bpp = 4 if alpha else 3

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))

    raw = b"".join(b"\x00" + b"\x7f" * (w * bpp) for _ in range(h))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, ctype, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class FakeConnector:
    verification = "mocked"
    available = True

    def __init__(self, name, caps, handler=None, model="fake-model"):
        self.name = name
        self.caps = frozenset(caps)
        self.model = model
        self.handler = handler or (lambda req: AgentResult("ok", name, model))
        self.calls = []
        self.cancelled = []

    async def execute(self, req):
        self.calls.append(req)
        res = self.handler(req)
        if asyncio.iscoroutine(res):
            res = await res
        return res

    async def cancel(self, task_id):
        self.cancelled.append(task_id)
        return True


WRITER = {
    Cap.text,
    Cap.workspace_write,
    Cap.workspace_read,
    Cap.structured_output,
    Cap.session_resume,
}
MODEL = {Cap.text, Cap.image_input, Cap.file_input, Cap.structured_output}


class RecordingNotifier:
    def __init__(self, auto: str | None = "approve"):
        self.calls = []
        self.auto = auto
        self.orch = None

    async def report(self, task, html, *, actions=(), files=(), photos=(), control=False):
        self.calls.append(
            {"task": task.task_id, "html": html, "actions": list(actions), "control": control}
        )
        if self.auto:
            for a in actions:
                if a.kind == self.auto:
                    asyncio.get_running_loop().call_soon(
                        lambda t=a.token: asyncio.ensure_future(self.orch.apply_action(t, 1))
                    )

    def texts(self):
        return "\n".join(c["html"] for c in self.calls)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for k, v in {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }.items():
        monkeypatch.setenv(k, v)
    root = tmp_path / "project"
    root.mkdir()
    (root / "main.py").write_text("print('hi')\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
    return root


@pytest.fixture
def settings(tmp_path, repo):
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "var",
        workspace_root=repo,
        max_attachment_bytes=1024 * 1024,
        action_ttl_s=60,
        config_dir=None,  # legacy tests run without the YAML gateway configuration
    )


def build_orch(
    settings: Settings, connectors, qa_commands=None, image=None, auto="approve", gateway_cfg=None
):
    notifier = RecordingNotifier(auto)
    orch = Orchestrator(
        settings,
        Router(connectors),
        WorkspaceManager(settings.workspace_root, settings.data_dir / "worktrees"),
        AttachmentStore(settings.data_dir / "media", settings.max_attachment_bytes),
        ArtifactStore(settings.data_dir / "artifacts"),
        QARunner(qa_commands or {}, timeout_s=10),
        ActionRegistry(settings.action_ttl_s),
        image_provider=image,
        notifier=notifier,
        gateway_cfg=gateway_cfg,
    )
    notifier.orch = orch
    return orch, notifier


async def wait_done(orch, task, timeout=10):
    runner = orch._running.get(task.task_id)
    if runner:
        await asyncio.wait_for(asyncio.shield(runner), timeout)
    return task


def write_file(name: str, content: str):
    def handler(req):
        Path(req.workspace, name).write_text(content)
        return AgentResult(f"wrote {name}", "writer", "fake-model", session_id="sess-1")

    return handler


@pytest.fixture
def cfg_dir(tmp_path):
    """A writable copy of the repo's config/ directory."""
    d = tmp_path / "config"
    shutil.copytree(Path(__file__).resolve().parents[1] / "config", d)
    return d


@pytest.fixture(autouse=True)
def _outside_a_claude_conversation(monkeypatch):
    """Tests run the same inside or outside a Claude session: no conversation id from the env."""
    monkeypatch.delenv("CLAUDE_CODE_HOST_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
