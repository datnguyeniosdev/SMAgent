"""Codex desktop relay: the hook starts/stops a relay per folder; the relay pushes new work only."""

import json

from app import agents_setup as ag
from app import codex_relay as cr


def test_hook_records_the_newest_thread_and_it_outlives_session_end(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "RELAYS", tmp_path / "relays")
    monkeypatch.setattr("app.projects.project_for", lambda cwd: "Coinlets")
    out = cr.hook({"hook_event_name": "SessionStart", "session_id": "t1", "cwd": "/w/coin"})
    assert "Coinlets" in out["hookSpecificOutput"]["additionalContext"]
    cr.state_file("/w/coin").write_text(json.dumps({"thread": "t1", "cwd": "/w/coin", "pid": 7}))
    cr.hook({"hook_event_name": "SessionStart", "session_id": "t2", "cwd": "/w/coin"})
    state = json.loads(cr.state_file("/w/coin").read_text())
    assert state == {"thread": "t2", "cwd": "/w/coin"}  # newest thread; an old relay's pid is gone
    cr.hook({"hook_event_name": "SessionEnd", "session_id": "t2", "cwd": "/w/coin"})
    assert cr.state_file("/w/coin").exists()  # work waits in the thread until it is reopened


class Proc:
    def __init__(self, code, out=b""):
        self.returncode, self.out = code, out

    async def communicate(self):
        return self.out, b""


async def test_gateway_pushes_new_codex_work_once_and_drops_deleted_threads(
    settings, cfg_dir, tmp_path, monkeypatch
):
    from tests.test_config_gateway import make

    orch, _ = make(settings, cfg_dir, [])
    hub = orch.channel
    monkeypatch.setattr("app.projects.project_for", lambda cwd: "Coin & Co")
    monkeypatch.setattr(cr, "codex_cli", lambda: "/bin/codex")
    state = tmp_path / "x.json"
    state.write_text(json.dumps({"thread": "t9", "cwd": "/w/coin"}))
    calls = []

    async def run(*cmd, **k):
        calls.append(cmd)
        return Proc(0)

    monkeypatch.setattr(cr.asyncio, "create_subprocess_exec", run)
    await cr.push_round(hub, tmp_path, refresh=True)
    sid = hub.pull_id("Coin & Co", "codex", "/w/coin")
    assert sid in hub.sessions and calls == []  # listed, nothing to push
    msg, _ = hub.send("Coin & Co", "test", session_id=sid)
    await cr.push_round(hub, tmp_path, refresh=False)
    await cr.push_round(hub, tmp_path, refresh=False)  # pushed once
    assert len(calls) == 1 and calls[0][:4] == ("/bin/codex", "queue", "--thread", "t9")
    assert "agent 'codex'" in calls[0][5] and "/w/coin" in calls[0][5]
    assert f"session_id '{sid}'" in calls[0][5]  # the exact folder session
    assert json.loads(state.read_text())["pushed"] == msg["message_id"]

    hub.pull("Coin & Co", sid, "/w/coin", 1, "codex", "app")  # the thread took the first one
    hub.send("Coin & Co", "again", session_id=sid)

    async def gone(*cmd, **k):
        return Proc(1, b"no rollout found for thread id t9")

    monkeypatch.setattr(cr.asyncio, "create_subprocess_exec", gone)
    await cr.push_round(hub, tmp_path, refresh=False)
    assert not state.exists()  # the thread was deleted: its record goes, a new thread records again


def test_setup_adds_and_removes_only_its_codex_hooks(tmp_path):
    path = tmp_path / "hooks.json"
    mine = {"hooks": [{"type": "command", "command": "my-hook"}]}
    path.write_text(json.dumps({"hooks": {"SessionStart": [mine]}}))
    ag.codex_hooks(path, install=True)
    ag.codex_hooks(path, install=True)  # idempotent
    hooks = json.loads(path.read_text())["hooks"]
    assert len(hooks["SessionStart"]) == 2 and hooks["SessionStart"][0] == mine
    assert "codex-hook" in hooks["SessionEnd"][0]["hooks"][0]["command"]
    ag.codex_hooks(path, install=False)
    assert json.loads(path.read_text())["hooks"] == {"SessionStart": [mine]}


async def test_get_messages_without_cwd_finds_the_folder_session(settings, cfg_dir):
    """Codex desktop calls get_messages without cwd: it still gets the relay's (folder) session."""
    from tests.test_config_gateway import make

    orch, _ = make(settings, cfg_dir, [])
    hub, p = orch.channel, "Coinlets"
    relay_s, _, _ = hub.pull(p, None, "/w/coin", 0, "codex", "app")  # the relay's peek
    msg, _ = hub.send(p, "report progress", session_id=relay_s.session_id)
    s, got, _ = hub.pull(p, None, "", 1, "codex", "app")  # the desktop thread, no cwd
    assert s.session_id == relay_s.session_id and [m["message_id"] for m in got] == [
        msg["message_id"]
    ]


def test_loop_prompt_lets_a_fresh_run_finish_unanswered_work():
    """A busy reply must not stop a new thread from finishing what a dead session left behind."""
    from app.repo_setup import loop_prompt

    p = loop_prompt("Coinlets", "app", "codex")
    assert "If it reports busy, follow its note" in p and "in_progress_messages" in p
    assert "busy, switched off or no messages, do nothing" not in p


async def test_stale_session_id_without_cwd_finds_the_folder_session(settings, cfg_dir):
    """Codex desktop reuses an old session_id (from a cwd-less call) after that session expired."""
    from tests.test_config_gateway import make

    orch, _ = make(settings, cfg_dir, [])
    hub, p = orch.channel, "Coinlets"
    relay_s, _, _ = hub.pull(p, None, "/w/coin", 0, "codex", "app")
    msg, _ = hub.send(p, "test", session_id=relay_s.session_id)
    stale = hub.pull_id(p, "codex", "")  # what the thread remembered
    s, got, _ = hub.pull(p, stale, "", 1, "codex", "app")
    assert s.session_id == relay_s.session_id and [m["message_id"] for m in got] == [
        msg["message_id"]
    ]


async def test_codex_reports_and_replies_name_codex(settings, cfg_dir):
    """A Codex session's submit_report / reply_message must not read as claude_code or mcp-caller."""
    from mcp.client import Client

    from app.mcp_server import build_mcp_server
    from tests.test_config_gateway import make

    orch, _ = make(settings, cfg_dir, [])
    hub, p = orch.channel, "Coinlets"
    s, _, _ = hub.pull(p, None, "/w/coin", 0, "codex", "app")
    msg, _ = hub.send(p, "progress?", session_id=s.session_id)
    hub.pull(p, s.session_id, "/w/coin", 1, "codex", "app")
    async with Client(build_mcp_server(orch)) as c:
        args = {"project": p, "text": "Dạ, xong ạ", "session_id": s.session_id}
        r = (await c.call_tool("submit_report", args)).structured_content
        rr = (await c.call_tool("reply_message", {"session_id": s.session_id, "text": "Dạ, ok ạ",
                                                   "message_id": msg["message_id"]})).structured_content  # fmt: skip
    assert orch.collab.get_report(r["report_id"]).source == "codex (pull session)"
    assert orch.collab.get_report(rr["report_id"]).source == "codex (pull session)"
