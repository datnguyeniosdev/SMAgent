"""Channel push into a live Claude Code session: real gateway (uvicorn) + real shim process (stdio)."""

import asyncio
import json
import os
import socket
import sys
from pathlib import Path

import pytest
import uvicorn

from tests.conftest import make_png

PROJECT = "2048 Shooter (K05)"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ShimProc:
    """Drives the shim exactly like Claude Code: newline-delimited JSON-RPC on stdin/stdout."""

    def __init__(self, proc):
        self.proc, self.n = proc, 0

    async def send(self, obj):
        self.proc.stdin.write((json.dumps(obj) + "\n").encode())
        await self.proc.stdin.drain()

    async def read(self, timeout=10):
        return json.loads(await asyncio.wait_for(self.proc.stdout.readline(), timeout))

    async def request(self, method, params=None):
        self.n += 1
        await self.send({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}})
        while True:  # notifications may interleave with the response
            msg = await self.read()
            if msg.get("id") == self.n:
                return msg


async def test_push_into_live_session_and_reply_round_trip(tmp_path, monkeypatch, cfg_dir):
    from app.config import get_settings

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("CONFIG_DIR", str(cfg_dir))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    from app.main import app

    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    hub = app.state.orch.channel
    proc = None
    try:
        # A session for the project was seen before (so /send can resolve it); none is open now.
        hub.svc.store.put(
            "channel_session", "cc_old", {"project": PROJECT, "cwd": "/x", "task_id": "t"}
        )
        early, target = hub.send(hub.resolve_project("2048"), "do the early thing")
        assert target is None  # waits in the inbox

        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", f"from app.channel_shim import main; main({PROJECT!r})",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            env={**os.environ, "AGW_URL": f"http://127.0.0.1:{port}", "AGW_CHANNEL": "1"},
        )  # fmt: skip
        shim = ShimProc(proc)
        init = await shim.request(
            "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}
        )
        assert init["result"]["capabilities"]["experimental"] == {"claude/channel": {}}
        tools = await shim.request("tools/list")
        assert [t["name"] for t in tools["result"]["tools"]] == ["reply"]
        await shim.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        first = await shim.read()  # the waiting message arrives as soon as the session connects
        assert first["method"] == "notifications/claude/channel"
        assert first["params"]["content"] == "do the early thing"
        assert first["params"]["meta"]["message_id"] == early["message_id"]
        session = hub.session_for(PROJECT)
        assert session is not None

        msg, target = hub.send(PROJECT, "fix the pause menu", sender="Darren")  # what /send does
        assert target is session
        pushed = await shim.read()
        assert pushed["params"]["content"] == "fix the pause menu"
        assert pushed["params"]["meta"] == {"source": "telegram", "sender": "Darren", "project": PROJECT,
                                            "message_id": msg["message_id"]}  # fmt: skip

        res = await shim.request("tools/call", {"name": "reply", "arguments": {
            "text": "Pause menu fixed\nButtons realigned; EditMode tests pass.", "message_id": msg["message_id"]}})  # fmt: skip
        assert not res["result"]["isError"], res
        reports = hub.svc.collab.reports_for(session.task_id)
        assert reports and reports[-1].text.startswith("Pause menu fixed")
        assert reports[-1].source == "claude_code (push session)"

        # Operator replies on Telegram (with a screenshot) -> pushed back into the same session.
        fb = await hub.svc.collab.submit_feedback(
            text="also move the title up", user_id="11", source="telegram",
            report_id=reports[-1].report_id, images=[("shot.png", make_png(20, 10))],
        )  # fmt: skip
        assert fb.route_id == "channel" and fb.delivered and fb.follow_up_task_id is None
        back = await shim.read()
        assert "also move the title up" in back["params"]["content"]
        assert (
            "Screenshot: " in back["params"]["content"] and back["params"]["meta"]["sender"] == "11"
        )
        assert hub.svc.store.get("channel_msg", msg["message_id"]) is not None
    finally:
        if proc is not None:
            proc.kill()
            await proc.wait()
        server.should_exit = True
        await serving
        get_settings.cache_clear()
    assert hub.live() == []  # the stream closed with the shim


def test_shim_stays_idle_outside_agw_session():
    """In an ordinary session (no AGW_CHANNEL) the shim neither declares the channel nor listens."""
    import io

    from app.channel_shim import Shim

    out = io.StringIO()
    shim = Shim(PROJECT, "http://127.0.0.1:1", enabled=False, out=out)
    shim.serve(io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": "reply", "arguments": {"text": "x"}}}) + "\n"
    ))  # fmt: skip
    init, call = (json.loads(line) for line in out.getvalue().splitlines())
    assert "experimental" not in init["result"]["capabilities"] and not shim.streaming
    assert "Not a live channel session" in call["result"]["content"][0]["text"]


def test_shim_rejects_non_http_gateway():

    from app.channel_shim import Shim

    with pytest.raises(SystemExit):
        Shim(PROJECT, "file:///etc/passwd", enabled=True)


def test_channel_blocked_reads_claude_code_mcp_log(tmp_path, monkeypatch):
    import time

    from app.channel_shim import channel_blocked

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cwd = "/Users/me/Working/My project"
    logs = (
        tmp_path
        / "Library/Caches/claude-cli-nodejs/-Users-me-Working-My-project/mcp-logs-agent-gateway-channel"
    )
    logs.mkdir(parents=True)
    started = time.time()
    (logs / "a.jsonl").write_text('{"debug":"Successfully connected"}\n')
    assert channel_blocked(cwd, started, wait_s=0.3) is False
    (logs / "a.jsonl").write_text(
        '{"debug":"Channel notifications skipped: channels not enabled by org policy"}\n'
    )
    assert channel_blocked(cwd, started, wait_s=0.3) is True


async def test_blocked_session_never_receives(tmp_path):
    from types import SimpleNamespace

    from app.channel import ChannelHub
    from app.store import Store

    store = Store(tmp_path / "s.db")
    tasks = {}

    def register_external(title, user, project, sid):
        t = SimpleNamespace(
            task_id=f"tsk_{len(tasks)}",
            request={"data": {}},
            state=SimpleNamespace(value="succeeded"),
        )
        tasks[t.task_id] = t
        return t

    svc = SimpleNamespace(store=store, tasks=tasks, register_external=register_external)
    hub = ChannelHub(svc)
    blocked = hub.connect("cc_b", PROJECT, "/x", blocked=True)
    msg, target = hub.send(PROJECT, "hello")
    assert target is None and hub.blocked_for(PROJECT) and blocked.queue.empty()
    live = hub.connect("cc_ok", PROJECT, "/y")
    assert live.queue.get_nowait()["message_id"] == msg["message_id"]  # waited, then delivered
    _, target = hub.send(PROJECT, "again", session_id="cc_b")  # even when addressed to it
    assert target is live


async def test_pull_mode_round_trip_and_switch(settings, cfg_dir):
    """Desktop-style session: /loop -> get_messages, answer with reply_message, Telegram reply comes back."""
    from mcp.client import Client

    from app.mcp_server import build_mcp_server
    from tests.test_config_gateway import make

    orch, _ = make(settings, cfg_dir, [])
    hub = orch.channel
    async with Client(build_mcp_server(orch)) as c:

        async def get(**kw):
            return (
                await c.call_tool("get_messages", {"project": PROJECT, **kw})
            ).structured_content

        first = await get(cwd="/repo")  # registers the pull session
        sid = first["session_id"]
        assert (
            first["messages"] == [] and first["enabled"] and hub.session_for(PROJECT).mode == "pull"
        )

        msg, target = hub.send(PROJECT, "rename the score label", sender="Darren")
        assert target.session_id == sid
        got = await get(session_id=sid)
        assert [m["message_id"] for m in got["messages"]] == [msg["message_id"]]
        assert (await get(session_id=sid))["messages"] == []  # handed over once

        rep = (await c.call_tool("reply_message", {"session_id": sid, "message_id": msg["message_id"],
                                                    "text": "Renamed\nScoreLabel -> PointsLabel"})).structured_content  # fmt: skip
        report = orch.collab.get_report(rep["report_id"])
        assert report.source == "claude_code (pull session)" and report.text.startswith("Renamed")

        fb = await orch.collab.submit_feedback(text="use title case", user_id="11", source="telegram",
                                               report_id=report.report_id)  # fmt: skip
        assert fb.route_id == "channel" and fb.delivered
        back = await get(session_id=sid)
        assert "use title case" in back["messages"][0]["text"]
        busy = await get(session_id=sid)  # one at a time: nothing new until it is answered
        assert busy["busy"] and busy["in_progress"] == [back["messages"][0]["message_id"]]
        await c.call_tool("reply_message", {"session_id": sid, "text": "Done",
                                            "message_id": back["messages"][0]["message_id"]})  # fmt: skip

        # operator switch: off -> nothing is handed over and new work waits; on -> it arrives
        hub.set_enabled(hub.sessions[sid], False)
        held, target = hub.send(PROJECT, "later")
        assert target is None and "turned off" in hub.undeliverable_reason(PROJECT)
        off = await get(session_id=sid)
        assert off["enabled"] is False and off["messages"] == [] and "switched off" in off["note"]
        hub.set_enabled(hub.sessions[sid], True)
        assert [m["message_id"] for m in (await get(session_id=sid))["messages"]] == [
            held["message_id"]
        ]
        assert orch.store.get("channel_session", sid)["enabled"] is True  # persisted

        bad = await c.call_tool("reply_message", {"session_id": "nope", "text": "x"})
        assert bad.is_error


async def test_push_preferred_and_stale_pull_released(settings, cfg_dir):
    from datetime import timedelta

    from app.channel import PULL_TTL
    from tests.test_config_gateway import make

    orch, _ = make(settings, cfg_dir, [])
    hub = orch.channel
    pull, _, _ = hub.pull(PROJECT, None)
    push = hub.connect("cc_push", PROJECT, "/repo")
    assert hub.session_for(PROJECT) is push  # instant delivery wins
    hub.set_enabled(push, False)
    msg, target = hub.send(PROJECT, "x")
    assert target is pull
    pull.last_seen -= PULL_TTL + timedelta(seconds=1)  # stopped polling
    assert pull.session_id not in [s.session_id for s in hub.live()]
    assert orch.store.get("channel_msg", msg["message_id"]) is not None
    assert [m["message_id"] for m in orch.store.list("channel_msg", states=["pending"])] == [
        msg["message_id"]
    ]
    hub.set_enabled(push, True)  # released work goes to the next session that can take it
    assert push.queue.get_nowait()["message_id"] == msg["message_id"]


async def test_list_ids_and_send_by_id(settings, cfg_dir):
    from types import SimpleNamespace

    from tests.test_config_gateway import CONTROL, USER, gateway, make

    orch, _ = make(settings, cfg_dir, [])
    gw = gateway(orch)
    hub = orch.channel
    a = hub.connect("cc_a", PROJECT, "/w/MiniGame2048")
    b, _, _ = hub.pull("Coinlets: Stack & Merge (TCN)", None, "/w/tini-coin")
    assert (a.num, b.num) == (1, 2) and hub.projects() == [
        (1, PROJECT),
        (2, "Coinlets: Stack & Merge (TCN)"),
    ]
    a = hub.connect("cc_a", PROJECT, "/w/MiniGame2048")  # a reconnect (new stream) keeps its id
    assert a.num == 1
    assert hub.find("s2") is b and hub.resolve_project("p2") == "Coinlets: Stack & Merge (TCN)"
    assert hub.find("s9") is None and hub.resolve_project("p9") is None

    text = gw._list_text()
    assert "<b>s1</b> · 🟢 on · ⚡ push · 2048 Shooter (K05) · MiniGame2048" in text
    assert "<b>s2</b> · 🟢 on · 🔄 pull" in text and "<b>p2</b> · Coinlets" in text

    answers = []

    def msg(args):
        async def answer(t):
            answers.append(t)

        m = SimpleNamespace(chat=SimpleNamespace(id=CONTROL), from_user=SimpleNamespace(id=USER, full_name="D"),
                            answer=answer)  # fmt: skip
        return m, SimpleNamespace(args=args, command="send")

    await gw.on_send(*msg("s2 run the tests"))  # exact session
    assert b.queue.get_nowait()["text"].endswith("run the tests") and "session s2" in answers[-1]
    await gw.on_send(*msg("p1 fix the menu"))  # project -> its best session
    assert a.queue.get_nowait()["text"].endswith("fix the menu")
    await gw.on_send(*msg("x9 hello"))
    assert "Unknown session or project" in answers[-1] and "SESSIONS" in answers[-1]


async def test_one_job_at_a_time_queue_and_cancel(settings, cfg_dir):
    from types import SimpleNamespace

    from tests.test_config_gateway import CONTROL, USER, gateway, make

    orch, _ = make(settings, cfg_dir, [])
    gw = gateway(orch)
    hub = orch.channel
    s, _, _ = hub.pull(PROJECT, None)
    m1, _ = hub.send(PROJECT, "first")
    m2, _ = hub.send(PROJECT, "second")
    m3, _ = hub.send(PROJECT, "third")
    assert (m1["num"], m2["num"], m3["num"]) == (1, 2, 3)

    _, got, busy = hub.pull(PROJECT, s.session_id)
    assert [m["message_id"] for m in got] == [m1["message_id"]] and busy == []
    _, got, busy = hub.pull(PROJECT, s.session_id)  # the next loop round while m1 is unanswered
    assert got == [] and [m["message_id"] for m in busy] == [m1["message_id"]]

    answers = []

    def msg(args, cmd):
        async def answer(t):
            answers.append(t)

        m = SimpleNamespace(chat=SimpleNamespace(id=CONTROL), from_user=SimpleNamespace(id=USER, full_name="D"),
                            answer=answer)  # fmt: skip
        return m, SimpleNamespace(args=args, command=cmd)

    await gw.on_queue(msg("", "queue")[0])
    assert (
        "<b>m1</b> · ▶️ in progress · s1" in answers[-1] and "<b>m2</b> · 📌 assigned" in answers[-1]
    )

    await gw.on_cancel(*msg("m2", "cancel"))  # waiting work: never delivered
    assert "m2</b> cancelled" in answers[-1] and "will not be delivered" in answers[-1]
    await hub.reply(s.session_id, "done first", m1["message_id"])  # frees the session
    _, got, _ = hub.pull(PROJECT, s.session_id)
    assert [m["message_id"] for m in got] == [m3["message_id"]]  # m2 skipped

    await gw.on_cancel(*msg("m3", "cancel"))  # in progress: only marked
    assert "not interrupted" in answers[-1]
    rep = await hub.reply(s.session_id, "done third", m3["message_id"])
    assert "which was cancelled on Telegram" in rep.text
    assert hub.open_messages() == []
    await gw.on_cancel(*msg("m9", "cancel"))
    assert "Usage: /cancel" in answers[-1]


def test_session_start_hook_and_loop_prompt(tmp_path):
    import json as _json

    from app.cli import main
    from app.repo_setup import loop_prompt, setup_repo

    repo = tmp_path / "r"
    repo.mkdir()
    for _ in range(2):  # idempotent
        setup_repo(repo, "http://127.0.0.1:8000/mcp", PROJECT, token=False)
    hooks = _json.loads((repo / ".claude/settings.local.json").read_text())["hooks"]["SessionStart"]
    assert (
        len(hooks) == 1
        and "hook-session-start --project '2048 Shooter (K05)'" in hooks[0]["hooks"][0]["command"]
    )
    assert "get_messages" in loop_prompt(PROJECT) and "busy" in loop_prompt(PROJECT)
    from app.repo_setup import health_prompt, session_start_hint

    hint = session_start_hint(PROJECT)
    assert "*/10 * * * *" in hint and health_prompt() in hint and "/agw dev >>" in health_prompt()
    assert main(["hook-session-start", "--project", PROJECT]) == 0


def test_stop_claude_only_kills_claude_processes():
    import subprocess as sp
    import sys as _sys

    from app.channel import stop_claude

    other = sp.Popen(["sleep", "30"])
    fake_claude = sp.Popen([_sys.executable, "-c", "import time; time.sleep(30)", "claude"])
    try:
        assert stop_claude(other.pid) is False and other.poll() is None  # not claude: untouched
        assert stop_claude(fake_claude.pid) is True
        assert fake_claude.wait(5) != 0
        assert stop_claude(0) is False and stop_claude(fake_claude.pid) is False  # gone
    finally:
        other.kill()
        fake_claude.kill()


async def test_close_push_and_pull_sessions(settings, cfg_dir):
    import subprocess as sp
    import sys as _sys
    from types import SimpleNamespace

    from mcp.client import Client

    from app.mcp_server import build_mcp_server
    from tests.test_config_gateway import CONTROL, USER, gateway, make

    orch, _ = make(settings, cfg_dir, [])
    gw = gateway(orch)
    hub = orch.channel
    answers = []

    async def answer(t):
        answers.append(t)

    m = SimpleNamespace(
        chat=SimpleNamespace(id=CONTROL), from_user=SimpleNamespace(id=USER), answer=answer
    )

    fake_claude = sp.Popen([_sys.executable, "-c", "import time; time.sleep(30)", "claude"])
    try:
        push = hub.connect("cc_p", PROJECT, "/w/MiniGame2048", pid=fake_claude.pid)
        await gw.on_close(m, SimpleNamespace(args=f"s{push.num}"))
        assert "stopped its Claude Code process" in answers[-1] and fake_claude.wait(5) != 0
        assert "cc_p" not in hub.sessions
    finally:
        fake_claude.kill()

    async with Client(build_mcp_server(orch)) as c:
        first = (await c.call_tool("get_messages", {"project": PROJECT})).structured_content
        sid = first["session_id"]
        msg, _ = hub.send(PROJECT, "later")  # assigned to the pull session, not yet taken
        await gw.on_close(m, SimpleNamespace(args=f"s{hub.sessions[sid].num}"))
        assert "told to stop" in answers[-1]
        stop = (
            await c.call_tool("get_messages", {"project": PROJECT, "session_id": sid})
        ).structured_content
        assert stop["closed"] and "CronDelete" in stop["note"] and stop["messages"] == []
        assert sid not in hub.sessions  # gone from /list
        assert orch.store.get("channel_msg", msg["message_id"])["state"] == "pending"  # work waits
        again = (
            await c.call_tool("get_messages", {"project": PROJECT, "session_id": sid})
        ).structured_content
        assert not again["closed"] and [x["message_id"] for x in again["messages"]] == [
            msg["message_id"]
        ]
    await gw.on_close(m, SimpleNamespace(args="s99"))
    assert "Usage: /close" in answers[-1]


def test_mcp_is_stateless_so_restarts_break_no_client(tmp_path, monkeypatch, cfg_dir):
    """No MCP session id is issued and a bare call works: nothing in the gateway's memory that a
    restart could invalidate (a stateful server answers such a call with 'missing session ID')."""
    from fastapi.testclient import TestClient

    from app.config import get_settings

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("CONFIG_DIR", str(cfg_dir))
    monkeypatch.setenv("MCP_REQUIRE_TOKEN", "false")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    from app.main import app

    h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}  # fmt: skip
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "get_messages", "arguments": {"project": PROJECT}}}  # fmt: skip
    try:
        with TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000)) as c:
            r = c.post("/mcp", json=init, headers=h)
            assert r.status_code == 200 and "mcp-session-id" not in r.headers
        with TestClient(
            app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000)
        ) as c:  # "restarted"
            r = c.post("/mcp", json=call, headers=h)  # no session header, no new initialize
            assert r.status_code == 200 and '"enabled":true' in r.text.replace(" ", "")
    finally:
        get_settings.cache_clear()


async def test_restart_requeues_and_lease_expiry_redelivers_then_drops(settings, cfg_dir):
    from datetime import timedelta

    from app.channel import LEASE, ChannelHub
    from app.models import now
    from tests.test_config_gateway import make

    orch, _ = make(settings, cfg_dir, [])
    hub = orch.channel
    s, _, _ = hub.pull(PROJECT, None)
    m1, _ = hub.send(PROJECT, "first")
    m2, _ = hub.send(PROJECT, "second")
    _, got, _ = hub.pull(PROJECT, s.session_id)
    assert [m["message_id"] for m in got] == [m1["message_id"]]

    # gateway restart: the in-memory queue holding m2 is gone; m2 must not stay "queued" forever
    hub = orch.channel = ChannelHub(orch)
    assert orch.store.get("channel_msg", m2["message_id"])["state"] == "pending"

    def age(mid):  # pretend the lease ran out
        msg = orch.store.get("channel_msg", mid)
        msg["delivered_at"] = (now() - LEASE - timedelta(minutes=1)).isoformat()
        orch.store.put("channel_msg", mid, msg, state=msg["state"])

    age(m1["message_id"])  # m1 never answered: handed out again (m1 is older, so first)
    _, got, _ = hub.pull(PROJECT, s.session_id)
    assert [m["message_id"] for m in got] == [m1["message_id"]] and got[0]["deliveries"] == 2

    age(m1["message_id"])  # still unanswered after its second delivery: expired, m2 is next
    _, got, _ = hub.pull(PROJECT, s.session_id)
    assert orch.store.get("channel_msg", m1["message_id"])["state"] == "expired"
    assert [m["message_id"] for m in got] == [m2["message_id"]]


async def test_reply_and_report_attach_images_by_path(settings, cfg_dir, tmp_path):
    import pytest as _pytest

    from app.storage import AttachmentError, load_image_paths
    from tests.test_config_gateway import make

    shot = tmp_path / "shot.png"
    shot.write_bytes(make_png(20, 10))
    with _pytest.raises(AttachmentError, match="absolute"):
        load_image_paths(["shot.png"])
    with _pytest.raises(AttachmentError):
        load_image_paths([str(tmp_path / "missing.png")])

    orch, _ = make(settings, cfg_dir, [])
    hub = orch.channel
    s, _, _ = hub.pull(PROJECT, None)
    m, _ = hub.send(PROJECT, "show me")
    hub.pull(PROJECT, s.session_id)
    rep = await hub.reply(s.session_id, "done", m["message_id"], load_image_paths([str(shot)]))
    assert len(rep.artifact_ids) == 1
    assert orch.artifacts.get(rep.artifact_ids[0]).filename == "shot.png"
