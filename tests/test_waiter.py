"""`agw wait`: wakes Claude only for new queued work, waits out a down gateway, newest waiter wins."""

import json
import os

from app import waiter as w


class Resp:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass

    def read(self):
        return json.dumps(self.body).encode()


def feed(monkeypatch, states):
    """Peek answers in order; an OSError entry is a gateway that is down. Hook POSTs (tell) are
    recorded in the returned list and take no answer."""
    it, posts = iter(states), []

    def urlopen(url, timeout):
        if not isinstance(url, str):  # a Request: tell() posting stalled / active
            posts.append(url.full_url)
            return Resp({})
        s = next(it)
        if isinstance(s, Exception):
            raise s
        return Resp(s)

    monkeypatch.setattr(w.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(w.time, "sleep", lambda s: None)
    return posts


def test_wakes_once_per_new_message(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(w, "WAITERS", tmp_path)
    idle, m1 = {"queued": False}, {"queued": True, "next": "chm_1"}
    feed(monkeypatch, [OSError("down"), idle, m1])  # down and idle: no wake-up, no output
    assert w.wait("Coin & Co", "/w/coin", "app", 8000) == 0
    out = capsys.readouterr().out
    assert "chm_1" in out and "session_id 'pull_coinco_claudecode_" in out and "'/w/coin'" in out
    assert "run_in_background" in out and "wait --project 'Coin & Co' --cwd /w/coin" in out

    # restarted while chm_1 is still queued (e.g. busy): no new wake-up until chm_2
    feed(monkeypatch, [m1, m1, {"queued": True, "next": "chm_2"}])
    assert w.wait("Coin & Co", "/w/coin", "app", 8000) == 0
    assert "chm_2" in capsys.readouterr().out


def test_closed_session_and_newer_waiter_stop_quietly(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(w, "WAITERS", tmp_path)
    feed(monkeypatch, [{"closing": True}])
    assert w.wait("P", "/w/p", "", 8000) == 0
    assert "do not restart" in capsys.readouterr().out

    def replaced(url, timeout):  # another waiter took over this session meanwhile
        (path,) = tmp_path.glob("*.json")
        path.write_text(json.dumps({"pid": os.getpid() + 1}))
        return Resp({"queued": False})

    monkeypatch.setattr(w.urllib.request, "urlopen", replaced)
    assert w.wait("P", "/w/p", "", 8000) == 0
    assert "newer waiter" in capsys.readouterr().out


async def test_long_poll_returns_on_new_work_and_waits_past_after(settings, cfg_dir):
    import asyncio

    from tests.test_config_gateway import make

    orch, _ = make(settings, cfg_dir, [])
    hub = orch.channel
    idle = await hub.wait("P", "claude_code", "/w/p", "app", hold=1.2)  # nothing: held, then empty
    assert idle["queued"] is False and idle["session_id"] in hub.sessions

    async def later():
        await asyncio.sleep(0.3)
        return hub.send("P", "do it", session_id=idle["session_id"])[0]

    sent = asyncio.create_task(later())
    got = await hub.wait("P", "claude_code", "/w/p", "app", hold=5)
    msg = await sent
    assert got["next"] == msg["message_id"]  # woke on arrival, before the hold ran out
    held = await hub.wait("P", "claude_code", "/w/p", "app", after=msg["message_id"], hold=1.2)
    assert (
        held["next"] == msg["message_id"]
    )  # already woken for: held to the end, not returned early


def test_prompt_hook_restarts_a_dead_waiter_unless_closed(tmp_path, monkeypatch, capsys):
    """After an app restart the waiter is gone: the next prompt tells Claude to start it again."""
    from app import projects
    from app.cli import main

    monkeypatch.setattr(w, "WAITERS", tmp_path / "waiters")
    (tmp_path / "ball-shot").mkdir()
    (tmp_path / "ball-shot/AGENTS.md").write_text("x")
    (tmp_path / "ball-shot/README.md").write_text("# Ball Shot (K10)\n")
    monkeypatch.setattr(projects, "projects_root", lambda: tmp_path)
    monkeypatch.chdir(tmp_path / "ball-shot")
    cwd = str(tmp_path / "ball-shot")
    assert main(["hook-prompt"]) == 0
    out = capsys.readouterr().out
    assert "not running" in out and "wait --project 'Ball Shot (K10)'" in out

    from app.channel import ChannelHub

    state = w.WAITERS / f"{ChannelHub.pull_id('Ball Shot (K10)', 'claude_code', cwd)}.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"pid": os.getpid()}))  # alive
    assert main(["hook-prompt"]) == 0 and capsys.readouterr().out == ""
    state.write_text(json.dumps({"pid": 999999}))  # its process is gone
    assert w.missing("Ball Shot (K10)", cwd)
    state.write_text(json.dumps({"closed": True}))  # /close: not brought back
    assert main(["hook-prompt"]) == 0 and capsys.readouterr().out == ""
    monkeypatch.chdir(tmp_path)  # outside a project: silent
    assert main(["hook-prompt"]) == 0 and capsys.readouterr().out == ""


async def test_watchdog_prune_reports_a_dropped_session(settings, cfg_dir, monkeypatch):
    from datetime import timedelta

    from app import channel as ch
    from tests.test_config_gateway import make

    orch, _ = make(settings, cfg_dir, [])
    hub, notes = orch.channel, []
    monkeypatch.setattr(hub, "_ops", notes.append)
    s, _, _ = hub.pull("Ball Shot (K10)", None, "/w/ball", 0, "claude_code", "app")
    hub._prune()
    assert s.session_id in hub.sessions and not any("left" in n for n in notes)  # seen recently
    s.last_seen -= ch.PULL_TTL + timedelta(seconds=1)
    hub._prune()
    assert s.session_id not in hub.sessions
    assert (
        "left" in notes[-1] and "app restarted" in notes[-1] and "restarts its waiter" in notes[-1]
    )


async def test_reply_message_tells_an_app_session_to_restart_its_waiter(settings, cfg_dir):
    """The waiter exited to wake Claude; right after answering is when it must start again."""
    from mcp.client import Client

    from app.mcp_server import build_mcp_server
    from tests.test_config_gateway import make

    orch, _ = make(settings, cfg_dir, [])
    async with Client(build_mcp_server(orch)) as c:
        kw = {"project": "Mario Clone (K10)", "agent": "claude_code", "cwd": "/w/mario"}
        for client, restart in (("app", True), ("cli", False)):
            sid = (await c.call_tool("get_messages", {**kw, "client": client})).structured_content[
                "session_id"
            ]
            msg, _ = orch.channel.send("Mario Clone (K10)", "plan the characters", session_id=sid)
            await c.call_tool("get_messages", {**kw, "client": client, "session_id": sid})
            rep = (await c.call_tool("reply_message", {"session_id": sid, "text": "Dạ, xong ạ",
                                                        "message_id": msg["message_id"]})).structured_content  # fmt: skip
            assert (
                "wait --project 'Mario Clone (K10)' --cwd /w/mario" in rep.get("next", "")
            ) is restart


def test_antigravity_waiter_wakes_its_conversation_and_keeps_running(tmp_path, monkeypatch):
    monkeypatch.setattr(w, "WAITERS", tmp_path)
    m1, m2 = {"queued": True, "next": "chm_1"}, {"queued": True, "next": "chm_2"}
    feed(monkeypatch, [m1, m1, m2, {"closing": True}])
    woke = []
    assert w.wait("Glazed Gems (GMA)", "/w/ok03", "app", 8000, agent="antigravity",
                  notify=lambda t: woke.append(t) or True) == 0  # fmt: skip
    assert len(woke) == 2 and "chm_1" in woke[0] and "chm_2" in woke[1]  # once each, never exits
    assert "agent 'antigravity'" in woke[0] and "pull_glazedgemsgma_antigravity_" in woke[0]
    assert "keeps running by itself" in woke[0] and "--agent antigravity" in woke[0]


def test_antigravity_notify_needs_its_terminal(monkeypatch):
    calls = []
    monkeypatch.delenv("ANTIGRAVITY_CONVERSATION_ID", raising=False)
    assert w.antigravity_notify() is None  # outside Antigravity: nothing to wake
    monkeypatch.setenv("ANTIGRAVITY_CONVERSATION_ID", "conv-1")
    monkeypatch.setenv("ANTIGRAVITY_LS_ADDRESS", "127.0.0.1:1")
    monkeypatch.setenv("ANTIGRAVITY_AGENTAPI_EXE", "/x/agentapi")

    class Done:
        returncode, stdout = 0, "{}"

    monkeypatch.setattr(w.subprocess, "run", lambda cmd, **k: calls.append(cmd) or Done())
    assert w.antigravity_notify()("work") and calls == [
        ["/x/agentapi", "send-message", "--title=Telegram work", "conv-1", "work"]
    ]

    calls.clear()
    monkeypatch.setenv("ANTIGRAVITY_AGENTAPI_EXE", "/bin/language_server")
    assert w.antigravity_notify()("work2") and calls == [
        [
            "/bin/language_server",
            "agentapi",
            "send-message",
            "--title=Telegram work",
            "conv-1",
            "work2",
        ]
    ]


async def test_reply_message_tells_antigravity_to_restart_its_waiter(settings, cfg_dir):
    from mcp.client import Client

    from app.mcp_server import build_mcp_server
    from tests.test_config_gateway import make

    orch, _ = make(settings, cfg_dir, [])
    kw = {"project": "Agent Gateway", "agent": "antigravity", "cwd": "/w/gw", "client": "app"}
    async with Client(build_mcp_server(orch)) as c:
        sid = (await c.call_tool("get_messages", kw)).structured_content["session_id"]
        msg, _ = orch.channel.send("Agent Gateway", "xin chào", session_id=sid)
        await c.call_tool("get_messages", {**kw, "session_id": sid})
        rep = (await c.call_tool("reply_message", {"session_id": sid, "text": "Dạ, chào anh ạ",
                                                    "message_id": msg["message_id"]})).structured_content  # fmt: skip
    assert (
        "wait --project 'Agent Gateway' --cwd /w/gw --client app --agent antigravity" in rep["next"]
    )


def test_antigravity_waiter_detaches_and_stops_when_its_conversation_is_gone(
    tmp_path, monkeypatch, capsys
):
    from app import cli, projects

    monkeypatch.setattr(w, "WAITERS", tmp_path / "waiters")
    (tmp_path / "mario").mkdir()
    (tmp_path / "mario/AGENTS.md").write_text("x")
    (tmp_path / "mario/README.md").write_text("# Mario Clone (K10)\n")
    monkeypatch.setattr(projects, "projects_root", lambda: tmp_path)
    monkeypatch.setenv("ANTIGRAVITY_CONVERSATION_ID", "conv-1")
    monkeypatch.setenv("ANTIGRAVITY_LS_ADDRESS", "127.0.0.1:1")
    monkeypatch.delenv("AGW_WAIT_DETACHED", raising=False)
    spawned = []

    class Proc:
        pid = 4242

    monkeypatch.setattr(
        cli.subprocess, "Popen", lambda cmd, **k: spawned.append((cmd, k)) or Proc()
    )
    assert cli.main(["wait", "--agent", "antigravity", "--cwd", str(tmp_path / "mario")]) == 0
    cmd, k = spawned[0]
    assert cmd[1:4] == ["wait", "--agent", "antigravity"] and "Mario Clone (K10)" in cmd
    assert k["start_new_session"] and k["env"]["AGW_WAIT_DETACHED"] == "1"
    assert "pid 4242" in capsys.readouterr().out  # returns at once: Antigravity cannot stop it

    m1 = {"queued": True, "next": "chm_1"}
    feed(monkeypatch, [m1, m1, m1])
    assert w.wait("P", "/w/p", "app", 8000, agent="antigravity", notify=lambda t: False) == 1


async def test_every_gateway_tool_says_when_a_desktop_sessions_waiter_is_not_running(
    settings, cfg_dir, tmp_path, monkeypatch
):
    """Seen in prod: a main woke, worked on (still_working, its own subagent) and ended its turn
    without restarting its waiter; only reply_message reminded it. Now every tool does."""
    from mcp.client import Client

    from app.channel import ChannelHub
    from app.mcp_server import build_mcp_server
    from tests.test_config_gateway import make

    monkeypatch.setattr(w, "WAITERS", tmp_path / "waiters")
    orch, _ = make(settings, cfg_dir, [])
    kw = {"project": "UIUXAgent", "agent": "claude_code", "cwd": "/w/ui", "client": "app"}
    async with Client(build_mcp_server(orch)) as c:
        r = (await c.call_tool("get_messages", kw)).structured_content
        sid = r["session_id"]
        assert (
            "waiter is not running" in r["next"]
            and "wait --project UIUXAgent --cwd /w/ui" in r["next"]
        )
        orch.channel.send("UIUXAgent", "review", session_id=sid)
        await c.call_tool("get_messages", {**kw, "session_id": sid})
        r = (
            await c.call_tool("still_working", {"session_id": sid, "note": "half way"})
        ).structured_content
        assert "waiter is not running" in r["next"]

        state = w.WAITERS / f"{ChannelHub.pull_id('UIUXAgent', 'claude_code', '/w/ui')}.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"pid": os.getpid()}))  # its waiter runs: no reminder
        r = (await c.call_tool("still_working", {"session_id": sid})).structured_content
        assert "next" not in r
        r = (
            await c.call_tool("get_messages", {**kw, "session_id": sid, "limit": 0})
        ).structured_content
        assert "next" not in r
