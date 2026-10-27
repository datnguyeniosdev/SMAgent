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
        assert first["params"]["content"].startswith("do the early thing")
        assert first["params"]["meta"]["message_id"] == early["message_id"]
        session = hub.session_for(PROJECT)
        assert session is not None

        msg, target = hub.send(PROJECT, "fix the pause menu", sender="Darren")  # what /send does
        assert target is session
        pushed = await shim.read()
        assert pushed["params"]["content"].startswith("fix the pause menu")
        assert pushed["params"]["meta"] == {"source": "telegram", "sender": "Darren", "project": PROJECT,
                                            "message_id": msg["message_id"],
                                            "session_id": session.session_id}  # fmt: skip

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
            first["messages"] == []
            and first.get("enabled", True)
            and hub.session_for(PROJECT).mode == "pull"
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
        assert busy["busy"] and [m["message_id"] for m in busy["in_progress_messages"]] == [
            back["messages"][0]["message_id"]
        ]
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
    hub.set_enabled(push, True)  # the main (pull) is offline: the operator chooses, nothing moves
    assert push.queue.empty()
    hub.set_main(push)  # /main s#
    assert push.queue.get_nowait()["message_id"] == msg["message_id"]


async def test_list_ids_and_send_by_id(settings, cfg_dir):
    from types import SimpleNamespace

    from tests.test_config_gateway import CONTROL, USER, gateway, make

    orch, _ = make(settings, cfg_dir, [])
    gw = gateway(orch)
    hub = orch.channel
    a = hub.connect("cc_a", PROJECT, "/w/k05-toss-mallow-p0")
    b, _, _ = hub.pull("Coinlets: Stack & Merge (TCN)", None, "/w/k09-coin-sort-fun")
    assert (a.num, b.num) == (1, 2) and hub.projects() == [
        (1, PROJECT),
        (2, "Coinlets: Stack & Merge (TCN)"),
    ]
    a = hub.connect(
        "cc_a", PROJECT, "/w/k05-toss-mallow-p0"
    )  # a reconnect (new stream) keeps its id
    assert a.num == 1
    assert hub.find("s2") is b and hub.resolve_project("p2") == "Coinlets: Stack & Merge (TCN)"
    assert hub.find("s9") is None and hub.resolve_project("p9") is None

    text = gw._list_text()
    assert (
        "<b>s1</b> · ⌨️ <b>ClaudeCode(CLI)-2048Shooter</b> · 👑 main · 🟢 on · ⚡ push · k05-toss-mallow-p0"
        in text
    )
    assert (
        "<b>s2</b> · ⌨️ <b>Agent(CLI)-Coinlets</b> · 👑 main · 🟢 on · 🔄 pull" in text
        and "<b>p2</b> · Coinlets" in text
    )

    answers = []

    def msg(args):
        async def answer(t):
            answers.append(t)

        m = SimpleNamespace(chat=SimpleNamespace(id=CONTROL), from_user=SimpleNamespace(id=USER, full_name="D"),
                            answer=answer)  # fmt: skip
        return m, SimpleNamespace(args=args, command="send")

    await gw.on_send(*msg("s2 run the tests"))  # exact session
    text = b.queue.get_nowait()["text"]  # s2 is the main: even addressed to it, it routes
    assert "run the tests" in text and "dispatch(" in text and "session s2" in answers[-1]
    await gw.on_send(
        *msg("p1 fix the menu")
    )  # project -> its main session; no git repo: no children,
    text = a.queue.get_nowait()["text"]  # so it does the work itself
    assert (
        "fix the menu" in text and "dispatch(" in text
    )  # a folder without git: children get a copy
    await gw.on_send(*msg("x9 hello"))
    assert "Unknown session or project" in answers[-1] and "SESSIONS" in answers[-1]

    hub.disconnect(b)  # s2's waiter stopped: /send s2 waits for it instead of "unknown session"
    await gw.on_send(*msg("s2 plan the characters"))
    assert "queued" in answers[-1] and "Unknown" not in answers[-1]
    b2, got, _ = hub.pull(
        "Coinlets: Stack & Merge (TCN)", None, "/w/k09-coin-sort-fun", 1
    )  # it reconnects
    assert b2.num == 2 and "plan the characters" in got[0]["text"]


async def test_one_job_at_a_time_queue_and_cancel(settings, cfg_dir):
    from types import SimpleNamespace

    from tests.test_config_gateway import CONTROL, USER, gateway, make

    orch, _ = make(settings, cfg_dir, [])
    gw = gateway(orch)
    hub = orch.channel
    hub.connect(
        "cc_main", PROJECT, "/w/main", mode="push", notify=False
    )  # the main: it only routes
    s, _, _ = hub.pull(PROJECT, None)  # a worker session
    m1, _ = hub.send(PROJECT, "first", session_id=s.session_id)
    m2, _ = hub.send(PROJECT, "second", session_id=s.session_id)
    m3, _ = hub.send(PROJECT, "third", session_id=s.session_id)
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
        "<b>m1</b> · ▶️ in progress · s2" in answers[-1] and "<b>m2</b> · 📌 assigned" in answers[-1]
    )

    await gw.on_cancel(*msg("m2", "cancel"))  # waiting work: never delivered
    assert "m2</b> cancelled" in answers[-1] and "will not be delivered" in answers[-1]
    await hub.reply(s.session_id, "done first", m1["message_id"])  # frees the session
    _, got, _ = hub.pull(PROJECT, s.session_id)
    assert [m["message_id"] for m in got] == [m3["message_id"]]  # m2 skipped

    await gw.on_cancel(*msg("m3", "cancel"))  # in progress: only marked
    assert "told to stop" in answers[-1]
    rep = await hub.reply(s.session_id, "done third", m3["message_id"])
    assert "which was cancelled on Telegram" in rep.text
    [stop] = hub.open_messages()  # the session was told to stop; a note, no reply needed
    assert stop["kind"] == "answer" and "cancelled m3" in stop["text"]
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
    from app.repo_setup import session_start_hint

    hint = session_start_hint(PROJECT, "/w/r")
    assert (
        "CronDelete" in hint
        and "run_in_background" in hint
        and "wait --project '2048 Shooter (K05)' --cwd /w/r" in hint
        and "CronCreate" not in hint
    )
    assert main(["hook-session-start", "--project", PROJECT]) == 0


def test_repo_hook_follows_a_renamed_project(tmp_path, monkeypatch, capsys):
    """An older repo hook pins the old name (Gem Mosaic); the README now says Glazed Gems."""
    from app import global_setup, projects
    from app.cli import main

    (tmp_path / "colorever").mkdir()
    (tmp_path / "colorever/AGENTS.md").write_text("x")
    (tmp_path / "colorever/README.md").write_text("# Glazed Gems (GMA)\n")
    monkeypatch.setattr(projects, "projects_root", lambda: tmp_path)
    monkeypatch.chdir(tmp_path / "colorever")
    monkeypatch.setattr(global_setup, "installed", lambda: False)
    assert main(["hook-session-start", "--project", "Gem Mosaic (GMA)"]) == 0
    out = capsys.readouterr().out
    assert "Glazed Gems (GMA)" in out and "Gem Mosaic" not in out
    monkeypatch.setattr(global_setup, "installed", lambda: True)
    assert main(["hook-session-start", "--project", "Gem Mosaic (GMA)"]) == 0
    assert capsys.readouterr().out == ""  # the global hook says it once


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
        assert not again.get("closed") and [x["message_id"] for x in again["messages"]] == [
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
            assert r.status_code == 200 and '"session_id"' in r.text
    finally:
        get_settings.cache_clear()


async def test_restart_requeues_and_lease_expiry_stalls_for_the_operator(settings, cfg_dir):
    from datetime import timedelta

    from app.channel import LEASE, ChannelHub
    from app.models import now
    from tests.test_config_gateway import make

    orch, _ = make(settings, cfg_dir, [])
    hub = orch.channel
    hub.connect(
        "cc_main", PROJECT, "/w/main", mode="push", notify=False
    )  # the main: it only routes
    s, _, _ = hub.pull(PROJECT, None)
    m1, _ = hub.send(PROJECT, "first", session_id=s.session_id)
    m2, _ = hub.send(PROJECT, "second", session_id=s.session_id)
    _, got, _ = hub.pull(PROJECT, s.session_id)
    assert [m["message_id"] for m in got] == [m1["message_id"]]

    # gateway restart: the in-memory queue holding m2 is gone; m2 must not stay "queued" forever
    hub = orch.channel = ChannelHub(orch)
    assert orch.store.get("channel_msg", m2["message_id"])["state"] == "pending"

    def age(mid):  # pretend the lease ran out
        msg = orch.store.get("channel_msg", mid)
        msg["delivered_at"] = (now() - LEASE - timedelta(minutes=1)).isoformat()
        orch.store.put("channel_msg", mid, msg, state=msg["state"])

    age(m1["message_id"])  # m1 never answered: held for the operator, not handed out silently
    _, got, _ = hub.pull(PROJECT, s.session_id, limit=0)
    assert orch.store.get("channel_msg", m1["message_id"])["state"] == "stalled"
    _, got, _ = hub.pull(PROJECT, s.session_id)  # the session is free for m2
    assert [m["message_id"] for m in got] == [m2["message_id"]]
    await hub.reply(s.session_id, "m2 done", m2["message_id"])
    msg, target = hub.resume(f"m{m1['num']}")  # ↪️ on Telegram: out again
    assert (
        target.session_id == s.session_id and msg["state"] == "queued" and "stall_reason" not in msg
    )


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


async def test_old_delivered_message_without_timestamp_expires_not_redelivered(settings, cfg_dir):
    """Messages delivered before leases existed have no delivered_at: pulling must not crash, and a
    day-old request must not be handed to an agent again."""
    from tests.test_config_gateway import make

    orch, _ = make(settings, cfg_dir, [])
    hub = orch.channel
    s, _, _ = hub.pull(PROJECT, None)
    old, _ = hub.send(PROJECT, "yesterday's request")
    _, got, _ = hub.pull(PROJECT, s.session_id)
    rec = orch.store.get("channel_msg", old["message_id"])
    rec.pop("delivered_at")  # as written by the older code
    orch.store.put("channel_msg", old["message_id"], rec, state="delivered")
    s2, got2, busy = hub.pull(PROJECT, "pull_other")  # used to raise KeyError
    assert got2 == [] and busy == []
    assert orch.store.get("channel_msg", old["message_id"])["state"] == "expired"
    assert hub.in_progress(s.session_id) == []


async def test_stale_pull_id_and_answers_stay_with_their_tool(settings, cfg_dir):
    """ok03: the Claude agent kept its pre-rename id; its waiter uses the folder's. And an answer for
    the Claude session never lands in the project's Codex thread."""
    from tests.test_config_gateway import make

    orch, _ = make(settings, cfg_dir, [])
    hub, p, ok03 = orch.channel, "Glazed Gems (GMA)", "/w/ok03-colorever"
    stale = hub.pull_id(p, "claude_code", "/w/colorever")  # remembered from the old folder
    s, _, _ = hub.pull(p, stale, ok03, 0, "claude_code", "app")
    assert s.session_id == hub.pull_id(p, "claude_code", ok03)  # the folder's session
    codex, _, _ = hub.pull(p, None, ok03, 0, "codex", "app")
    hub.disconnect(s)  # the Claude session is offline when the operator answers
    msg, target = hub.send(p, "💬 answer", session_id=s.session_id, extra={"kind": "answer"})
    assert target is None and codex.queue.empty()  # waits for Claude, not Codex
    s2, got, _ = hub.pull(p, stale, ok03, 5, "claude_code", "app")  # Claude is back
    assert [m["message_id"] for m in got] == [msg["message_id"]]


async def test_main_session_routes_lanes_to_children_in_worktrees(
    settings, cfg_dir, tmp_path, monkeypatch
):
    """docs/DISPATCH_DESIGN.md: project work -> the main session, which only routes; one child per
    lane (same lane in order), a new lane opens a new child in its own git worktree."""
    import subprocess
    from datetime import timedelta

    from app import projects
    from app.channel import CHILD_IDLE
    from tests.test_config_gateway import make

    repo = tmp_path / "game"
    repo.mkdir()
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(repo)]
    subprocess.run([*git, "init", "-q"], check=True)
    (repo / "README.md").write_text(f"# {PROJECT}\n")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "init"], check=True)
    monkeypatch.setattr(projects, "projects_root", lambda: tmp_path)
    from app import channel as ch

    claude_json = tmp_path / "claude.json"  # the operator trusts the project, not its children yet
    claude_json.write_text(json.dumps({"projects": {str(repo): {"hasTrustDialogAccepted": True,
                                                                "allowedTools": ["x"]}}}))  # fmt: skip
    monkeypatch.setattr(ch, "CLAUDE_JSON", claude_json)

    orch, _ = make(settings, cfg_dir, [])
    hub, opened, notes = orch.channel, [], []
    hub.launch = lambda path, agent, model="": (
        opened.append((path, agent)) or 100 + len(opened)
    )  # window id
    from app import launcher

    closed_windows = []
    monkeypatch.setattr(launcher, "close_terminal_window", closed_windows.append)
    monkeypatch.setattr(hub, "_ops", notes.append)
    main, _, _ = hub.pull(PROJECT, None, str(repo), 0, "claude_code", "app")
    other, _, _ = hub.pull(PROJECT, None, "/w/other", 0, "codex", "cli")
    assert hub.main_for(PROJECT) is main  # the first session (app before CLI) becomes the main

    job, _ = hub.send(
        PROJECT, "own job", session_id=main.session_id
    )  # addressed to the main: routed
    assert "do not do it yourself" in orch.store.get("channel_msg", job["message_id"])["text"]
    _, got, _ = hub.pull(PROJECT, main.session_id)
    await hub.dispatch(main.session_id, job["message_id"], "misc")
    opened.clear()  # (its child: not part of what follows)
    m1, t1 = hub.send(PROJECT, "fix the shop button")
    assert t1 is main
    _, got, busy = hub.pull(PROJECT, main.session_id)
    assert [m["message_id"] for m in got] == [m1["message_id"]] and not busy  # never busy
    assert "do not do it yourself" in got[0]["text"]

    with pytest.raises(PermissionError):  # only the main routes
        await hub.dispatch(other.session_id, m1["message_id"], "ui")
    r = await hub.dispatch(main.session_id, f"m{m1['num']}", "UI", brief="fix it, report a shot")
    assert r["new_session"] and r["branch"].startswith("agw/ui-") and len(opened) == 1
    path, r_branch = Path(r["path"]), r["branch"]
    trusted = json.loads(claude_json.read_text())["projects"][str(path)]
    assert trusted == {  # trusted like the project (its own imports too), nothing else copied
        "hasTrustDialogAccepted": True,
        "hasClaudeMdExternalIncludesApproved": True,
        "hasClaudeMdExternalIncludesWarningShown": True,
    }
    # a copy-on-write clone inside the project (APFS), else a git worktree next to it
    assert path.parent in (repo / ".agw-children", tmp_path / ".agw-worktrees" / "game")
    assert (path / ".mcp.json").exists() and (path / "README.md").exists()
    assert (
        "?? "
        not in subprocess.run(
            [*git, "status", "--porcelain"], capture_output=True, text=True
        ).stdout
    )
    assert hub.in_progress(main.session_id) == []  # routing done: the main is never left busy

    child, _, _ = hub.pull(PROJECT, None, str(path), 0, "claude_code", "cli")  # the child connects
    assert hub.child(child)["lane"] == "ui" and hub.main_for(PROJECT) is main
    _, got, _ = hub.pull(PROJECT, child.session_id)
    assert got[0]["message_id"] == m1["message_id"]
    assert got[0]["text"].endswith("fix it, report a shot") and "do not do it" not in got[0]["text"]

    m2, _ = hub.send(PROJECT, "and the coin icon")
    hub.pull(PROJECT, main.session_id)
    r = await hub.dispatch(
        main.session_id, m2["message_id"], "ui"
    )  # same lane: that child, in order
    assert r == {"to": f"s{child.num}", "lane": "ui", "new_session": False} and len(opened) == 1
    m3, _ = hub.send(PROJECT, "new level music")
    hub.pull(PROJECT, main.session_id)
    r = await hub.dispatch(main.session_id, m3["message_id"], "audio")  # other lane: in parallel
    assert r["new_session"] and len(opened) == 2

    subprocess.run(["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                    "--allow-empty", "-m", "child work"], check=True)  # fmt: skip
    await hub.reply(child.session_id, "Dạ, xong ạ", m1["message_id"])  # straight to the sender
    assert (
        r_branch in subprocess.run([*git, "branch"], capture_output=True, text=True).stdout
    )  # fetched
    _, got, _ = hub.pull(PROJECT, child.session_id)
    assert got[0]["message_id"] == m2["message_id"]
    await hub.reply(child.session_id, "Dạ, xong ạ", m2["message_id"])
    child.last_work -= CHILD_IDLE + timedelta(minutes=1)
    hub._prune()  # idle child: closed, branch kept
    assert child.session_id not in hub.sessions and "kept for review" in notes[-1]
    await asyncio.sleep(0.2)
    assert closed_windows == [101]  # its Terminal window goes with it
    assert [c["status"] for c in hub.children(PROJECT) if c["lane"] == "ui"] == ["closed"]

    m4, _ = hub.send(PROJECT, "one more ui fix")  # its lane's child is reopened, no new copy
    assert "ui (closed, m" in m4["text"]  # the router sees the lanes and their last task
    hub.pull(PROJECT, main.session_id)
    r = await hub.dispatch(main.session_id, m4["message_id"], "ui")
    assert r["to"] == "reopened" and r["path"] == str(path) and opened[-1][0] == path

    hub.stall(main.session_id, "rate limit")  # the main is stuck: a temporary router is opened
    m5, t5 = hub.send(PROJECT, "fix the score")
    router_dir = repo / ".agw-children" / "router"
    for _ in range(100):  # opened in the background (git, a Terminal window)
        if opened[-1][0] == router_dir:
            break
        await asyncio.sleep(0.05)
    assert t5 is None and opened[-1][0] == router_dir and (router_dir / ".mcp.json").exists()
    router, _, _ = hub.pull(PROJECT, None, str(router_dir), 0, "claude_code", "cli")
    _, got, _ = hub.pull(PROJECT, router.session_id)  # it gets the waiting work, with the lanes
    assert got[0]["message_id"] == m5["message_id"] and "temporary router" in got[0]["text"]
    assert "ui (" in got[0]["text"]
    await hub.dispatch(router.session_id, m5["message_id"], "audio")  # reopens audio's copy
    hub.active(main.session_id)  # back: it routes again, hears what was routed; router closed
    assert hub.main_for(PROJECT) is main and router.session_id not in hub.sessions
    note = [m for m in main.queue._queue if m.get("kind") == "answer"][-1]
    assert f"m{m5['num']} → audio" in note["text"] and "audio (" in note["text"]
    kids = {c["lane"]: c for c in hub.children(PROJECT)}
    assert kids["audio"]["task"].startswith(f"m{m5['num']} fix the score")
    assert "router" not in kids


async def test_a_push_session_and_its_pull_loop_are_one_main(settings, cfg_dir):
    """The main recorded from a window's push session (no agent name) is still the main when its
    pull loop (claude_code) asks to dispatch."""
    from tests.test_config_gateway import make

    orch, _ = make(settings, cfg_dir, [])
    hub = orch.channel
    push = hub.connect("cc_1", PROJECT, "/w/a", mode="push", notify=False)
    assert hub.main_for(PROJECT) is push
    hub.disconnect(push)
    pull, _, _ = hub.pull(PROJECT, None, "/w/a", 0, "claude_code", "cli")
    assert hub.main_for(PROJECT) is pull


async def test_answers_skip_the_line_and_an_offline_main_is_put_to_the_operator(
    settings, cfg_dir, monkeypatch
):
    from tests.test_config_gateway import make

    orch, _ = make(settings, cfg_dir, [])
    hub, asked = orch.channel, []
    monkeypatch.setattr(hub, "_notify", lambda name, *a, **k: asked.append((name, a)))
    main0, _, _ = hub.pull(PROJECT, None, "/w/main", 0, "claude_code", "app")  # routes only
    a, _, _ = hub.pull(PROJECT, None, "/w/a", 0, "claude_code")
    job, _ = hub.send(PROJECT, "work", session_id=a.session_id)
    later, _ = hub.send(PROJECT, "later", session_id=a.session_id)
    hub.pull(PROJECT, a.session_id)
    ans, _ = hub.send(PROJECT, "✅ Đồng ý", session_id=a.session_id, extra={"kind": "answer"})
    _, got, busy = hub.pull(PROJECT, a.session_id)
    assert [m["message_id"] for m in got] == [ans["message_id"]] and busy  # skipped the line
    await hub.reply(a.session_id, "done", job["message_id"])
    _, got, _ = hub.pull(PROJECT, a.session_id)
    assert got[0]["message_id"] == later["message_id"]  # then its own work, in order

    hub.disconnect(a)
    hub.disconnect(main0)  # the main is gone and no other session is open
    m, target = hub.send(PROJECT, "project work")
    m2, _ = hub.send(PROJECT, "more")
    assert target is None and [n for n, _ in asked].count("send_main_offline") == 1  # asked once
    hub.want_new_main(PROJECT)  # 🆕 on Telegram: the next session to connect becomes the main
    c, _, _ = hub.pull(PROJECT, None, "/w/c", 0, "claude_code")
    assert hub.main_for(PROJECT) is c
    assert [x["message_id"] for x in c.queue._queue] == [m["message_id"], m2["message_id"]]


def test_child_folders_are_trusted_only_under_a_trusted_project(tmp_path, monkeypatch):
    from app import channel as ch

    cfg = tmp_path / "claude.json"
    cfg.write_text(
        json.dumps({"projects": {"/w/ok": {"hasTrustDialogAccepted": True}}, "other": 1})
    )
    monkeypatch.setattr(ch, "CLAUDE_JSON", cfg)
    assert not ch._trust(Path("/w/no/.agw-children/x"), Path("/w/no"))  # never trusted: left alone
    assert ch._trust(Path("/w/ok/.agw-children/x"), Path("/w/ok"))
    data = json.loads(cfg.read_text())
    assert data["other"] == 1 and "/w/no/.agw-children/x" not in data["projects"]
    assert data["projects"]["/w/ok/.agw-children/x"]["hasTrustDialogAccepted"] is True


async def _lanes(settings, cfg_dir, tmp_path, monkeypatch):
    """A tmp git repo (A.txt, B.txt), its main session and hub; dispatch(lane) opens a child."""
    import subprocess

    from app import channel as ch
    from app import projects
    from tests.test_config_gateway import make

    repo = tmp_path / "game"
    repo.mkdir()
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(repo)]
    subprocess.run([*git, "init", "-q"], check=True)
    (repo / "README.md").write_text(f"# {PROJECT}\n")
    (repo / "A.txt").write_text("a\n")
    (repo / "B.txt").write_text("b\n")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "init"], check=True)
    monkeypatch.setattr(projects, "projects_root", lambda: tmp_path)
    monkeypatch.setattr(ch, "CLAUDE_JSON", tmp_path / "claude.json")  # never the real one
    orch, _ = make(settings, cfg_dir, [])
    hub = orch.channel
    hub.launch = lambda path, agent, model="": None
    monkeypatch.setattr(hub, "_ops", lambda html: None)
    main, _, _ = hub.pull(PROJECT, None, str(repo), 0, "claude_code", "app")

    async def dispatch(lane: str, text: str = "work") -> dict:
        m, _ = hub.send(PROJECT, text)
        hub.pull(PROJECT, main.session_id)
        r = await hub.dispatch(main.session_id, m["message_id"], lane)
        return {**r, "msg": m}

    def sh(cwd, *args) -> str:
        cmd = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(cwd), *args]
        return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()

    return orch, hub, repo, dispatch, sh


async def test_child_records_its_base_and_target_and_how_it_was_made(
    settings, cfg_dir, tmp_path, monkeypatch
):
    from tests.test_config_gateway import gateway

    orch, hub, repo, dispatch, sh = await _lanes(settings, cfg_dir, tmp_path, monkeypatch)
    head, branch = sh(repo, "rev-parse", "HEAD"), sh(repo, "symbolic-ref", "--short", "HEAD")
    r = await dispatch("ui")
    rec = orch.store.get("channel_child", r["path"])
    assert rec["base"] == head and rec["target"] == branch
    assert rec["provider"] in ("clone", "worktree") and sh(r["path"], "rev-parse", "HEAD") == head
    ev = [e["data"] for e in orch.store.events() if e["type"] == "channel.child_created"]
    assert len(ev) == 1 and ev[0]["provider"] == rec["provider"] and ev[0]["seconds"] >= 0
    assert ev[0]["free_before"] > 0 and ev[0]["free_after"] > 0

    (repo / "A.txt").write_text("moved on\n")  # the repo advances: the child's base does not
    sh(repo, "commit", "-qam", "later")
    r2 = await dispatch("ui")  # the lane's child is closed (never connected): reopened
    assert r2["to"] == "reopened" and r2["path"] == r["path"]
    kid = next(c for c in hub.children(PROJECT) if c["lane"] == "ui")
    assert kid["base"] == head and kid["target"] == branch
    assert f"from {branch}@{head[:7]}" in gateway(orch)._list_text()


async def test_forced_fallback_records_a_worktree_child(settings, cfg_dir, tmp_path, monkeypatch):
    import subprocess

    from app import channel as ch

    orch, hub, repo, dispatch, sh = await _lanes(settings, cfg_dir, tmp_path, monkeypatch)
    real = subprocess.run

    def run(cmd, *a, **k):  # copy-on-write cloning fails (not APFS)
        return subprocess.CompletedProcess(cmd, 1) if cmd[0] == "cp" else real(cmd, *a, **k)

    monkeypatch.setattr(ch.subprocess, "run", run)
    r = await dispatch("build")
    rec = orch.store.get("channel_child", r["path"])
    assert rec["provider"] == "worktree" and rec["base"] == sh(repo, "rev-parse", "HEAD")
    ev = [e["data"] for e in orch.store.events() if e["type"] == "channel.child_created"]
    assert ev[-1]["provider"] == "worktree"
    assert {"seconds", "free_before", "free_after"} <= set(ev[-1])
    assert (Path(r["path"]) / ".git").is_file()  # a real git worktree


async def test_fetch_back_reports_conflicts_and_other_lanes_read_only(
    settings, cfg_dir, tmp_path, monkeypatch
):
    orch, hub, repo, dispatch, sh = await _lanes(settings, cfg_dir, tmp_path, monkeypatch)
    bodies = []

    async def submit_report(task_id, text, *a, **k):
        bodies.append(text)

    monkeypatch.setattr(orch.collab, "submit_report", submit_report)
    target = sh(repo, "symbolic-ref", "--short", "HEAD")
    ui, audio = await dispatch("ui", "fix A"), await dispatch("audio", "fix B")
    (repo / "A.txt").write_text("target edit\n")  # the target moves on the same line
    sh(repo, "commit", "-qam", "target edit")
    tip, status = sh(repo, "rev-parse", "HEAD"), sh(repo, "status", "--porcelain")

    async def work(r, files: dict) -> str:
        for name, text in files.items():
            (Path(r["path"]) / name).write_text(text)
        sh(r["path"], "commit", "-qm", "child work", "--", *files)
        s, _, _ = hub.pull(PROJECT, None, r["path"], 0, "claude_code", "cli")
        hub.pull(PROJECT, s.session_id)
        await hub.reply(s.session_id, "Dạ, xong ạ", r["msg"]["message_id"])
        return bodies[-1]

    body = await work(audio, {"B.txt": "audio\n"})  # clean: no report line
    assert "conflicts" not in body and "also changed" not in body
    body = await work(ui, {"A.txt": "ui edit\n", "B.txt": "ui\n"})
    assert f"conflicts with {target}: A.txt" in body
    assert "also changed by lane audio: B.txt" in body
    # read-only: target ref, HEAD and the project's working tree are untouched
    assert sh(repo, "rev-parse", target) == tip and sh(repo, "status", "--porcelain") == status
    assert sh(repo, "symbolic-ref", "--short", "HEAD") == target
    assert (repo / "A.txt").read_text() == "target edit\n"


async def test_prune_child_only_when_nothing_is_lost(settings, cfg_dir, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app import channel as ch
    from tests.test_config_gateway import CONTROL, USER, gateway

    orch, hub, repo, dispatch, sh = await _lanes(settings, cfg_dir, tmp_path, monkeypatch)
    r = await dispatch("ui")
    path, branch = r["path"], r["branch"]
    rec = orch.store.get("channel_child", path)

    victim = tmp_path / "victim"  # outside the children folder, directly and through a symlink
    victim.mkdir()
    (repo / ".agw-children" / "evil").symlink_to(victim)
    for bad in (str(victim), str(repo / ".agw-children" / "evil")):
        orch.store.put("channel_child", bad, {**rec, "path": bad, "lane": "evil"})
        with pytest.raises(ValueError, match="not a folder inside"):
            hub.prune_child(bad)
    assert victim.is_dir()

    if rec["provider"] == "clone":  # a worktree's branch is in the repo from the start
        with pytest.raises(ValueError, match="not fetched back"):
            hub.prune_child(path)
        assert ch._fetch_back(rec)
    s, _, _ = hub.pull(PROJECT, None, path, 0, "claude_code", "cli")
    with pytest.raises(ValueError, match="session is still open"):
        hub.prune_child(path)
    hub.disconnect(s)
    (Path(path) / "A.txt").write_text("unsaved\n")
    with pytest.raises(ValueError, match="uncommitted"):
        hub.prune_child(path)
    sh(path, "checkout", "-q", "--", "A.txt")
    (Path(path) / "new_asset.png").write_bytes(b"x")  # made by the child, never committed
    with pytest.raises(ValueError, match="new_asset.png"):
        hub.prune_child(path)
    (Path(path) / "new_asset.png").unlink()

    gw, said = gateway(orch), []

    async def answer(text=None, reply_markup=None, show_alert=False):
        said.append((text, reply_markup))

    msg = SimpleNamespace(answer=answer)
    monkeypatch.setattr(gw, "_authorized_msg", lambda m: True)
    await gw.on_children(msg, SimpleNamespace(args="prune ui"))
    data = [b.callback_data for row in said[-1][1].inline_keyboard for b in row]
    assert data[0] == f"cp:yes:{Path(path).name}" and Path(path).is_dir()  # asked, nothing gone
    press = SimpleNamespace(data=data[0], from_user=SimpleNamespace(id=USER), answer=answer,
                            message=SimpleNamespace(chat=SimpleNamespace(id=CONTROL), answer=answer))  # fmt: skip
    await gw.on_prune_button(press)
    assert "kept in the project repo" in said[-1][0] and not Path(path).exists()
    assert sh(repo, "rev-parse", "--verify", f"refs/heads/{branch}")  # the branch stays
    assert [c["status"] for c in hub.children(PROJECT) if c["path"] == path] == ["deleted"]
    assert hub.prune_child(path) == "already deleted"  # idempotent
    await gw.on_prune_button(press)
    assert "already deleted" in said[-1][0]


async def test_nothing_waits_forever_for_a_child_or_router_that_never_starts(
    settings, cfg_dir, tmp_path, monkeypatch
):
    """A child that never connects: its work goes to the operator as stuck. A temporary router that
    never connects: given up, the operator is asked, and no second router is opened."""
    from datetime import timedelta

    from app import channel as ch
    from app.models import now

    orch, hub, repo, dispatch, sh = await _lanes(settings, cfg_dir, tmp_path, monkeypatch)
    old = (now() - ch.START_WAIT - timedelta(minutes=1)).isoformat()
    r = await dispatch("ui", "fix the shop")  # its Terminal never opens: launch does nothing
    m = orch.store.get("channel_msg", r["msg"]["message_id"])
    assert m["state"] == "pending" and m["wait_cwd"] == r["path"]
    hub._expire()
    assert orch.store.get("channel_msg", m["message_id"])["state"] == "pending"  # still in time
    m["waiting_since"] = old
    orch.store.put("channel_msg", m["message_id"], m, state="pending")
    hub._expire()
    stuck = orch.store.get("channel_msg", m["message_id"])
    assert stuck["state"] == "stalled" and "did not start" in stuck["stall_reason"]

    main = hub.main_for(PROJECT)
    asked, opened = [], []
    monkeypatch.setattr(hub, "_notify", lambda name, *a, **k: asked.append(name))
    hub.launch = lambda path, agent, model="": opened.append(path)
    hub.stall(main.session_id, "rate limit")  # stuck main: a router is opened in the background
    m2, t2 = hub.send(PROJECT, "fix the score")
    assert t2 is None
    rec = orch.store.get("channel_main", ch._norm(PROJECT))
    assert rec["router_cwd"]
    rec["router_since"] = old  # it never connected
    orch.store.put("channel_main", ch._norm(PROJECT), rec)
    hub._expire()
    rec = orch.store.get("channel_main", ch._norm(PROJECT))
    assert rec.get("router_failed") and not rec.get("router_cwd") and "send_main_offline" in asked
    hub.send(PROJECT, "more work")
    assert orch.store.get("channel_main", ch._norm(PROJECT)).get("router_cwd") is None  # no reopen
    hub.active(main.session_id)  # the main works again: it takes everything that waited
    assert hub.main_for(PROJECT) is main
    assert not orch.store.get("channel_main", ch._norm(PROJECT)).get("router_failed")
    assert m2["message_id"] in [x["message_id"] for x in main.queue._queue]


async def test_a_terminal_main_is_not_confused_with_a_desktop_session_in_its_folder(
    settings, cfg_dir
):
    """Seen in prod (UIUXAgent): the main was a Terminal window (push, no agent name) and work went to
    a desktop session in the same folder. Only the window's own pull loop is its twin."""
    from tests.test_config_gateway import make

    orch, _ = make(settings, cfg_dir, [])
    hub = orch.channel
    push = hub.connect("cc_1", PROJECT, "/w/ui", mode="push", notify=False)
    assert hub.main_for(PROJECT) is push
    desktop, _, _ = hub.pull(PROJECT, None, "/w/ui", 0, "claude_code", "app")
    loop, _, _ = hub.pull(PROJECT, None, "/w/ui", 0, "claude_code", "cli")
    push.blocked = True  # its channel events are dropped: the pull loop takes its work
    assert hub.main_for(PROJECT) is loop
    hub.disconnect(loop)
    assert hub.main_for(PROJECT) is None  # the desktop session is not the main


async def test_still_working_keeps_long_work_from_being_called_stuck_or_closed(
    settings, cfg_dir, tmp_path, monkeypatch
):
    """Long tasks were killed: an early answer or a task past the lease made a working child look
    idle or stuck. still_working renews the lease and keeps the session busy; a child with work
    waiting for the operator is never closed."""
    from datetime import timedelta

    from app import channel as ch
    from app.models import now

    orch, hub, repo, dispatch, sh = await _lanes(settings, cfg_dir, tmp_path, monkeypatch)
    r = await dispatch("qa", "long playtest")
    child, _, _ = hub.pull(PROJECT, None, r["path"], 0, "claude_code", "cli")
    _, got, _ = hub.pull(PROJECT, child.session_id)
    m = orch.store.get("channel_msg", got[0]["message_id"])
    m["delivered_at"] = (now() - ch.LEASE + timedelta(minutes=1)).isoformat()  # 59 min in
    orch.store.put("channel_msg", m["message_id"], m, state="delivered")
    out = hub.still_working(child.session_id, "level 12 of 30")
    assert out["renewed"] == [m["message_id"]]
    hub._expire()  # an hour later it would have been stuck; the lease is fresh now
    assert orch.store.get("channel_msg", m["message_id"])["state"] == "delivered"

    await hub.reply(child.session_id, "Dạ, đang làm ạ", m["message_id"])  # answered early
    child.last_work -= ch.CHILD_IDLE + timedelta(minutes=1)
    hub._prune()
    assert child.session_id in hub.sessions  # still busy (still_working): not closed
    child.busy_until = now() - timedelta(seconds=1)
    m2, _ = hub.send(PROJECT, "stuck one", session_id=child.session_id)
    hub._stall([orch.store.get("channel_msg", m2["message_id"])], child.session_id, "rate limit")
    child.queue._queue.clear()
    hub._prune()
    assert child.session_id in hub.sessions  # work waits for the operator: not closed
    hub.cancel(m2["message_id"])
    hub._prune()
    assert child.session_id not in hub.sessions  # now really idle: closed

    with pytest.raises(KeyError):
        hub.still_working("pull_nobody")


async def test_a_router_finishes_its_routing_before_the_main_takes_over(
    settings, cfg_dir, tmp_path, monkeypatch
):
    from app import channel as ch

    orch, hub, repo, dispatch, sh = await _lanes(settings, cfg_dir, tmp_path, monkeypatch)
    main = hub.main_for(PROJECT)
    hub.stall(main.session_id, "rate limit")
    m, _ = hub.send(PROJECT, "fix the score")
    router_dir = repo / ".agw-children" / "router"
    for _ in range(100):
        if (orch.store.get("channel_main", ch._norm(PROJECT)) or {}).get("router_cwd"):
            break
        await asyncio.sleep(0.05)
    router, _, _ = hub.pull(PROJECT, None, str(router_dir), 0, "claude_code", "cli")
    _, got, _ = hub.pull(PROJECT, router.session_id)
    assert got[0]["message_id"] == m["message_id"]
    hub.active(main.session_id)  # the main is back while the router is mid-way
    assert router.session_id in hub.sessions and hub.main_for(PROJECT) is main
    await hub.dispatch(router.session_id, m["message_id"], "audio")
    hub.main_for(PROJECT)  # routed: now the router is closed and the main hears what it did
    assert router.session_id not in hub.sessions
    note = [x for x in main.queue._queue if x.get("kind") == "answer"][-1]
    assert f"m{m['num']} → audio" in note["text"]


async def test_a_project_without_git_still_gets_children_so_its_main_only_routes(
    settings, cfg_dir, tmp_path, monkeypatch
):
    """POAgent is not a git repo: its main did all the work itself and could get stuck. Now its
    children work in a plain copy of the folder (no branch)."""
    from app import channel as ch
    from app import projects
    from tests.test_config_gateway import make

    folder = tmp_path / "poagent"
    folder.mkdir()
    (folder / "README.md").write_text(f"# {PROJECT}\n")
    (folder / "plan.md").write_text("v1\n")
    monkeypatch.setattr(projects, "projects_root", lambda: tmp_path)
    monkeypatch.setattr(ch, "CLAUDE_JSON", tmp_path / "claude.json")
    orch, _ = make(settings, cfg_dir, [])
    hub, opened = orch.channel, []
    hub.launch = lambda path, agent, model="": opened.append(path)
    main, _, _ = hub.pull(PROJECT, None, str(folder), 0, "claude_code", "app")
    m, _ = hub.send(
        PROJECT, "research the market", session_id=main.session_id
    )  # even addressed to it
    _, got, _ = hub.pull(PROJECT, main.session_id)
    assert "do not do it yourself" in got[0]["text"]
    r = await hub.dispatch(main.session_id, m["message_id"], "research")
    path = Path(r["path"])
    assert r["new_session"] and r["branch"] == "" and opened == [path]
    assert (path / "plan.md").read_text() == "v1\n" and path.parent == folder / ".agw-children"
    assert orch.store.get("channel_child", str(path))["provider"] == "copy"
    child, _, _ = hub.pull(PROJECT, None, str(path), 0, "claude_code", "cli")
    _, got, _ = hub.pull(PROJECT, child.session_id)
    assert got[0]["message_id"] == m["message_id"]
    rep = await hub.reply(child.session_id, "Dạ, xong; sửa plan.md ạ", m["message_id"])
    assert rep is not None and hub.in_progress(main.session_id) == []


async def test_a_request_unanswered_for_two_leases_goes_to_the_operator_not_dropped(
    settings, cfg_dir
):
    """Seen in prod: a QA child still auditing hit its second lease; the request would have been
    dropped as "never answered". Now it goes to the operator as stuck."""
    from datetime import timedelta

    from app.channel import LEASE
    from app.models import now
    from tests.test_config_gateway import make

    orch, _ = make(settings, cfg_dir, [])
    hub = orch.channel
    hub.connect("cc_main", PROJECT, "/w/main", mode="push", notify=False)
    qa, _, _ = hub.pull(PROJECT, None, "/w/qa", 0, "claude_code")
    ask = orch.asks.create(PROJECT, "audit the game", from_session="cc_main")
    m, _ = hub.send(
        PROJECT,
        "audit",
        session_id=qa.session_id,
        extra={"ask_id": ask["ask_id"], "kind": "request"},
    )
    for lease in (1, 2):
        _, got, _ = hub.pull(PROJECT, qa.session_id)
        rec = orch.store.get("channel_msg", m["message_id"])
        rec["delivered_at"] = (now() - LEASE - timedelta(minutes=1)).isoformat()
        orch.store.put("channel_msg", m["message_id"], rec, state="delivered")
        hub._expire()
    got = orch.store.get("channel_msg", m["message_id"])
    assert got["state"] == "stalled" and "2 leases" in got["stall_reason"]


async def test_children_work_on_opus_and_the_router_on_a_cheaper_model(
    settings, cfg_dir, tmp_path, monkeypatch
):
    from app.launcher import terminal_command

    orch, hub, repo, dispatch, sh = await _lanes(settings, cfg_dir, tmp_path, monkeypatch)
    launched = []
    hub.launch = lambda path, agent, model="": launched.append(
        (Path(path).name.split("-")[0], model)
    )
    await dispatch("ui")  # the work: Opus by default
    m, _ = hub.send(PROJECT, "light job")
    main = hub.main_for(PROJECT)
    hub.pull(PROJECT, main.session_id)
    await hub.dispatch(main.session_id, m["message_id"], "docs", model="sonnet")  # per task
    hub.set_model(PROJECT, "child", "haiku")  # per project
    await dispatch("audio")
    with pytest.raises(ValueError):
        hub.set_model(PROJECT, "child", "gpt-9")
    assert launched == [("ui", "opus"), ("docs", "sonnet"), ("audio", "haiku")]
    assert hub.models(PROJECT) == {"child": "haiku", "router": "sonnet"}
    hub.set_model(PROJECT, "child", "default")
    assert hub.models(PROJECT)["child"] == "opus"

    hub.stall(main.session_id, "rate limit")  # the temporary router: the cheaper router model
    hub.send(PROJECT, "route me")
    for _ in range(100):
        if launched[-1][0] == "router":
            break
        await asyncio.sleep(0.05)
    assert launched[-1] == ("router", "sonnet")
    assert terminal_command(repo, "", "opus").endswith("session --model opus")
    assert "--model" not in terminal_command(repo, "codex", "opus")  # other tools keep their own


async def test_untaken_work_counts_from_when_the_session_is_free_and_a_busy_main_gets_a_router(
    settings, cfg_dir, tmp_path, monkeypatch
):
    """Seen in prod: work queued 19 min behind a job was marked stuck 2 s after the job ended; and
    routing work waiting behind a busy main never brought in the temporary router."""
    from datetime import timedelta

    from app import channel as ch
    from app.models import now

    orch, hub, repo, dispatch, sh = await _lanes(settings, cfg_dir, tmp_path, monkeypatch)
    worker, _, _ = hub.pull(PROJECT, None, "/w/worker", 0, "claude_code", "cli")
    job, _ = hub.send(PROJECT, "long job", session_id=worker.session_id)
    hub.pull(PROJECT, worker.session_id)
    waiting, _ = hub.send(PROJECT, "next job", session_id=worker.session_id)
    worker.queue._queue[0]["queued_at"] = (now() - ch.QUEUE_WAIT - timedelta(minutes=9)).isoformat()
    hub._expire()  # busy: it waits its turn
    assert orch.store.get("channel_msg", waiting["message_id"])["state"] == "queued"
    await hub.reply(worker.session_id, "done", job["message_id"])
    hub._expire()  # just free: not stuck yet
    assert orch.store.get("channel_msg", waiting["message_id"])["state"] == "queued"
    worker.last_work -= ch.QUEUE_WAIT + timedelta(minutes=1)  # free for 10+ min and still untaken
    hub._expire()
    assert orch.store.get("channel_msg", waiting["message_id"])["state"] == "stalled"

    main = hub.main_for(PROJECT)  # a main busy on work of its own: routing work waits for it
    own, _ = hub.send(PROJECT, "legacy job", session_id=main.session_id)
    m = orch.store.get("channel_msg", own["message_id"])
    m.pop("route", None)  # (sent before the main only routed)
    orch.store.put("channel_msg", own["message_id"], m, state="queued")
    main.queue._queue[0].pop("route", None)
    hub.pull(PROJECT, main.session_id)
    r, _ = hub.send(PROJECT, "route me")
    main.queue._queue[0]["queued_at"] = (now() - ch.QUEUE_WAIT - timedelta(minutes=1)).isoformat()
    hub._expire()
    assert main.stalled and (orch.store.get("channel_main", ch._norm(PROJECT)) or {}).get(
        "router_cwd"
    )


async def test_stuck_work_resumed_to_the_main_is_routed_not_done_there(settings, cfg_dir):
    from tests.test_config_gateway import make

    orch, _ = make(settings, cfg_dir, [])
    hub = orch.channel
    main, _, _ = hub.pull(PROJECT, None, "/w/main", 0, "claude_code", "app")
    worker, _, _ = hub.pull(PROJECT, None, "/w/worker", 0, "claude_code", "cli")
    m, _ = hub.send(PROJECT, "fix the hud", session_id=worker.session_id)
    hub.pull(PROJECT, worker.session_id)
    hub.stall(worker.session_id, "rate limit")
    _, target = hub.resume(f"m{m['num']}", main.session_id)  # ↪️ the main on Telegram
    assert target is main and "do not do it yourself" in main.queue._queue[0]["text"]
