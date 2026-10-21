"""Tool registry/executor/policy, built-in tools, MCP client (real local stdio server), tool loop."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from app.builtin_tools import register_builtin_tools
from app.connectors.base import Cap, UnsupportedCapability
from app.mcp_client import McpServerConfig, discover, register_mcp_tools
from app.qa import QARunner
from app.storage import ArtifactStore
from app.tool_loop import LoopLimits, run_tool_loop, wire_name
from app.tools import (
    ToolContext,
    ToolDefinition,
    ToolError,
    ToolExecutor,
    ToolOutput,
    ToolRegistry,
    ToolStatus,
)
from tests.conftest import make_png

ALL = frozenset({"workspace:read", "workspace:write", "process:execute", "mcp:execute"})
FIXTURE = str(Path(__file__).parent / "fixtures" / "mcp_echo_server.py")


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "a.py").write_text("print(1)\n")
    (root / "sub").mkdir()
    (root / "sub" / "b.txt").write_text("hello")
    return root


@pytest.fixture
def ex(tmp_path):
    reg = ToolRegistry()
    register_builtin_tools(
        reg, QARunner({"ok": "true", "missing": "definitely-not-installed-xyz"}, timeout_s=5)
    )
    return ToolExecutor(reg)


def ctx(ws, perms=ALL, approved=frozenset(), artifacts=None):
    return ToolContext("tsk_1", ws, perms, approved=approved, artifacts=artifacts)


async def test_registry_lists_real_tools_and_validates_schema():
    reg = ToolRegistry()
    register_builtin_tools(reg, QARunner({}))
    names = [t.name for t in reg.list()]
    assert (
        "workspace.read_file" in names and "qa.run" not in names
    )  # no QA configured -> not registered
    with pytest.raises(Exception):
        reg.register(ToolDefinition("bad", "x", {"type": "nonsense"}, None, frozenset()))


async def test_unknown_tool_and_invalid_input_structured(ex, ws):
    r = await ex.execute("rm_rf", {}, ctx(ws))
    assert (
        r.status == ToolStatus.failed
        and r.error_category == "unknown_tool"
        and "workspace.read_file" in r.error
    )
    r = await ex.execute("workspace.read_file", {"path": 5}, ctx(ws))
    assert r.error_category == "invalid_input"
    r = await ex.execute("workspace.read_file", {"path": "a.py", "extra": 1}, ctx(ws))
    assert r.error_category == "invalid_input"


async def test_policy_permissions_and_workspace(ex, ws):
    r = await ex.execute(
        "workspace.create_file",
        {"path": "n.txt", "content": "x"},
        ctx(ws, frozenset({"workspace:read"})),
    )
    assert (
        r.status == ToolStatus.failed
        and r.error_category == "forbidden"
        and "workspace:write" in r.error
    )
    r = await ex.execute("workspace.read_file", {"path": "a.py"}, ctx(None))
    assert r.error_category == "forbidden"


async def test_path_traversal_symlink_and_git_dir(ex, ws, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("s")
    (ws / "link").symlink_to(secret)
    for p in ["../secret.txt", "/etc/passwd", "link", "sub/../../secret.txt"]:
        r = await ex.execute("workspace.read_file", {"path": p}, ctx(ws))
        assert r.status == ToolStatus.failed and r.error_category == "forbidden", p
    (ws / ".git").mkdir()
    (ws / ".git" / "config").write_text("x")
    r = await ex.execute("workspace.read_file", {"path": ".git/config"}, ctx(ws))
    assert r.error_category == "forbidden"


async def test_builtin_tools_real_results(ex, ws):
    r = await ex.execute("workspace.list_files", {"pattern": "*.py"}, ctx(ws))
    assert r.status == ToolStatus.succeeded and r.data["files"] == ["a.py"]
    r = await ex.execute(
        "workspace.create_file", {"path": "new/x.py", "content": "y = 2\n"}, ctx(ws)
    )
    assert r.status == ToolStatus.succeeded and (ws / "new/x.py").read_text() == "y = 2\n"
    r = await ex.execute("workspace.create_file", {"path": "a.py", "content": "evil"}, ctx(ws))
    assert (
        r.error_category == "conflict" and (ws / "a.py").read_text() == "print(1)\n"
    )  # never overwrites
    r = await ex.execute("artifact.hash", {"path": "sub/b.txt"}, ctx(ws))
    assert r.data["sha256"] == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    (ws / "i.png").write_bytes(make_png(8, 4))
    r = await ex.execute(
        "image.validate", {"path": "i.png", "width": 8, "height": 4, "require_alpha": True}, ctx(ws)
    )
    assert r.data["issues"] == [] and r.data["width"] == 8


async def test_qa_tool_records_and_skips_missing_runtime(ex, ws, tmp_path):
    arts = ArtifactStore(tmp_path / "arts")
    r = await ex.execute("qa.run", {"check": "ok"}, ctx(ws, artifacts=arts))
    assert (
        r.status == ToolStatus.succeeded
        and r.data["status"] == "passed"
        and r.data["exit_code"] == 0
    )
    assert r.data["run_id"].startswith("qa_") and r.data["task_id"] == "tsk_1" and r.artifact_ids
    r = await ex.execute("qa.run", {"check": "missing"}, ctx(ws, artifacts=arts))
    assert (
        r.status == ToolStatus.failed and r.error_category == "runtime_unavailable"
    )  # skipped is never passed
    r = await ex.execute("qa.run", {"check": "rm -rf /"}, ctx(ws))
    assert r.error_category == "invalid_input"


async def test_approval_timeout_idempotency_and_output_cap(ws):
    reg = ToolRegistry()
    calls = []

    async def slow(c, a):
        await asyncio.sleep(1)

    async def big(c, a):
        calls.append(1)
        return ToolOutput("x" * 50_000)

    reg.register(
        ToolDefinition(
            "danger",
            "d",
            {"type": "object"},
            big,
            frozenset(),
            requires_approval=True,
            side_effects=True,
        )
    )
    reg.register(
        ToolDefinition("slow_ro", "d", {"type": "object"}, slow, frozenset(), timeout_s=0.05)
    )
    reg.register(
        ToolDefinition(
            "slow_w", "d", {"type": "object"}, slow, frozenset(), timeout_s=0.05, side_effects=True
        )
    )
    events = []

    class Pub:
        async def tool_event(self, rec, event):
            events.append((rec.tool, event, rec.status))

    ex = ToolExecutor(reg, publisher=Pub())
    r = await ex.execute("danger", {}, ctx(ws))
    assert r.status == ToolStatus.approval_required and not calls
    r2 = await ex.resume_approved(r.record_id, {}, ctx(ws))
    assert (
        r2.status == ToolStatus.succeeded and len(r2.output) < 21_000 and "truncated" in r2.output
    )
    a = await ex.execute("danger", {}, ctx(ws, approved=frozenset({"danger"})), call_id="c1")
    b = await ex.execute("danger", {}, ctx(ws, approved=frozenset({"danger"})), call_id="c1")
    assert a is b and len(calls) == 2  # same call id never executes twice
    assert (await ex.execute("slow_ro", {}, ctx(ws))).status == ToolStatus.timed_out
    assert (await ex.execute("slow_w", {}, ctx(ws))).status == ToolStatus.unknown
    assert ("danger", "approval_required", ToolStatus.approval_required) in events


async def test_publisher_failure_does_not_change_outcome(ws):
    reg = ToolRegistry()

    async def ok(c, a):
        return ToolOutput("fine")

    reg.register(ToolDefinition("ok", "d", {"type": "object"}, ok, frozenset()))

    class Broken:
        async def tool_event(self, rec, event):
            raise RuntimeError("telegram down")

    r = await ToolExecutor(reg, publisher=Broken()).execute("ok", {}, ctx(ws))
    assert r.status == ToolStatus.succeeded


# ---------- MCP (real stdio protocol against a local fixture server) ----------


async def test_mcp_discovery_allowlist_and_invocation(ws):
    cfg = McpServerConfig(
        name="fx",
        command=sys.executable,
        args=[FIXTURE],
        allowed_tools=["echo", "read_len", "explode", "not_there"],
        read_only_tools=["echo", "read_len"],
    )
    found = await discover(cfg)
    assert (
        found.ok
        and found.server_name == "echo-fixture"
        and {t["name"] for t in found.tools} == {"echo", "read_len", "flaky", "explode"}
    )
    reg = ToolRegistry()
    names = register_mcp_tools(reg, cfg, found)
    assert names == ["mcp.fx.echo", "mcp.fx.read_len", "mcp.fx.explode"]
    assert any("not_there" in n for n in reg.discovery_notes) and any(
        "flaky: discovered, not allowlisted" in n for n in reg.discovery_notes
    )
    assert (
        reg.get("mcp.fx.explode").requires_approval and not reg.get("mcp.fx.echo").requires_approval
    )
    ex = ToolExecutor(reg)
    r = await ex.execute("mcp.fx.echo", {"text": "hi"}, ctx(ws))
    assert r.status == ToolStatus.succeeded and r.output == "echo: hi"
    r = await ex.execute("mcp.fx.read_len", {"path": "sub/b.txt"}, ctx(ws))
    assert r.output == "5"  # relative path resolved inside the workspace
    r = await ex.execute("mcp.fx.read_len", {"path": "../../etc/passwd"}, ctx(ws))
    assert r.error_category == "forbidden"
    r = await ex.execute("mcp.fx.explode", {}, ctx(ws, approved=frozenset({"mcp.fx.explode"})))
    assert (
        r.status == ToolStatus.failed
        and r.error_category == "mcp_tool_error"
        and "explode" in r.error
    )  # server hides details
    r = await ex.execute("mcp.fx.echo", {"wrong": 1}, ctx(ws))
    assert r.error_category == "invalid_input"


async def test_mcp_unreachable_server_reports_failure():
    found = await discover(
        McpServerConfig(name="dead", command="/nonexistent/mcp-binary", timeout_s=5)
    )
    assert not found.ok and found.error
    found = await discover(
        McpServerConfig(name="dead_http", url="http://127.0.0.1:9/mcp", timeout_s=3)
    )
    assert not found.ok


# ---------- model tool-calling loop (mocked provider, real tools) ----------


class ScriptedProvider:
    name = "scripted"
    caps = frozenset({Cap.text, Cap.tool_calling})

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []

    async def chat(self, payload):
        self.seen.append(json.loads(json.dumps(payload)))
        msg = self.replies.pop(0)
        return msg, {"model": "m", "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


def call(cid, name, args):
    return {
        "id": cid,
        "type": "function",
        "function": {"name": wire_name(name), "arguments": json.dumps(args)},
    }


async def test_tool_loop_executes_real_tools_and_returns_results(ex, ws):
    prov = ScriptedProvider(
        [
            {
                "content": None,
                "tool_calls": [
                    call("c1", "workspace.read_file", {"path": "a.py"}),
                    call("c2", "bogus.tool", {}),
                ],
            },
            {"content": "The file prints 1."},
        ]
    )
    res = await run_tool_loop(prov, ex, ctx(ws), "what does a.py do?", ["workspace.read_file"])
    assert res.status == "succeeded" and res.text == "The file prints 1." and res.iterations == 2
    assert res.records[0].status == ToolStatus.succeeded and res.records[0].output == "print(1)\n"
    assert res.records[1].error_category == "unknown_tool"  # invented tool rejected, not executed
    tool_msgs = [m for m in prov.seen[1]["messages"] if m["role"] == "tool"]
    assert json.loads(tool_msgs[0]["content"])["output"] == "print(1)\n"
    assert json.loads(tool_msgs[1]["content"])["status"] == "failed"
    assert prov.seen[0]["tools"][0]["function"]["name"] == "workspace__read_file"


async def test_tool_loop_budget_and_unsupported(ex, ws):
    loop_forever = [
        {"content": None, "tool_calls": [call(f"c{i}", "workspace.list_files", {})]}
        for i in range(10)
    ]
    res = await run_tool_loop(
        ScriptedProvider(loop_forever),
        ex,
        ctx(ws),
        "x",
        ["workspace.list_files"],
        limits=LoopLimits(max_iterations=3),
    )
    assert res.status == "budget_exhausted" and res.iterations == 3 and len(res.records) == 3

    class NoTools(ScriptedProvider):
        caps = frozenset({Cap.text})

    with pytest.raises(UnsupportedCapability):
        await run_tool_loop(NoTools([]), ex, ctx(ws), "x", ["workspace.list_files"])
    with pytest.raises(UnsupportedCapability):
        await run_tool_loop(ScriptedProvider([]), ex, ctx(ws), "x", ["not.registered"])


async def test_tool_loop_stops_at_approval(ws):
    reg = ToolRegistry()

    async def h(c, a):
        raise ToolError("should not run")

    reg.register(
        ToolDefinition(
            "deploy",
            "d",
            {"type": "object"},
            h,
            frozenset(),
            requires_approval=True,
            side_effects=True,
        )
    )
    prov = ScriptedProvider([{"content": None, "tool_calls": [call("c1", "deploy", {})]}])
    res = await run_tool_loop(prov, ToolExecutor(reg), ctx(ws), "deploy", ["deploy"])
    assert (
        res.status == "approval_required" and res.records[0].status == ToolStatus.approval_required
    )


def test_wire_names_are_safe():
    assert wire_name("mcp.unity.editor_status") == "mcp__unity__editor_status"
    assert len(wire_name("x" * 100)) == 64


async def test_symlink_in_workspace_root_is_contained(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f").write_text("secret")
    ws = tmp_path / "w"
    ws.mkdir()
    os.symlink(outside, ws / "escape")
    reg = ToolRegistry()
    register_builtin_tools(reg, QARunner({}))
    r = await ToolExecutor(reg).execute("workspace.list_files", {"path": "escape"}, ctx(ws))
    assert r.error_category == "forbidden"
