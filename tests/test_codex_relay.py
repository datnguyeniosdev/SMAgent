"""Codex desktop relay: the hook starts/stops a relay per folder; the relay pushes new work only."""

import json
import os

from app import agents_setup as ag
from app import codex_relay as cr


def test_hook_starts_one_relay_per_folder_and_session_end_stops_it(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "RELAYS", tmp_path / "relays")
    monkeypatch.setattr("app.projects.project_for", lambda cwd: "Coinlets")
    started = []

    class Proc:
        pid = os.getpid()  # "alive", so a second SessionStart starts nothing new

    monkeypatch.setattr(cr.subprocess, "Popen", lambda *a, **k: started.append(a[0]) or Proc())
    out = cr.hook({"hook_event_name": "SessionStart", "session_id": "t1", "cwd": "/w/coin"})
    assert "Coinlets" in out["hookSpecificOutput"]["additionalContext"]
    cr.hook({"hook_event_name": "SessionStart", "session_id": "t2", "cwd": "/w/coin"})
    assert len(started) == 1 and started[0][1:] == ["codex-relay", "--cwd", "/w/coin"]
    state = json.loads(cr.state_file("/w/coin").read_text())
    assert state["thread"] == "t2"  # the newest thread gets the work
    monkeypatch.setattr(cr.os, "kill", lambda pid, sig: None)
    cr.hook({"hook_event_name": "SessionEnd", "session_id": "t2", "cwd": "/w/coin"})
    assert not cr.state_file("/w/coin").exists()


def test_relay_pushes_queued_work_into_the_thread(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "RELAYS", tmp_path)
    monkeypatch.setattr("app.projects.project_for", lambda cwd: "Coin & Co")
    monkeypatch.setattr(cr, "codex_cli", lambda: "/bin/codex")
    cr.state_file("/w/coin").write_text(json.dumps({"thread": "t9", "pid": 1}))

    class Resp:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(self.body).encode()

    calls = []
    monkeypatch.setattr(cr.urllib.request, "urlopen", lambda url, timeout: Resp({"queued": False}))
    monkeypatch.setattr(cr.subprocess, "run", lambda cmd, **k: calls.append(cmd))
    cr.relay("/w/coin", 8000, once=True)
    assert calls == []  # nothing new: nothing pushed

    class Done:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(cr.urllib.request, "urlopen", lambda url, timeout: Resp({"queued": True}))
    monkeypatch.setattr(cr.subprocess, "run", lambda cmd, **k: calls.append(cmd) or Done())
    cr.relay("/w/coin", 8000, once=True)
    assert calls[0][:4] == ["/bin/codex", "queue", "--thread", "t9"]
    assert "agent 'codex'" in calls[0][5] and "/w/coin" in calls[0][5]


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
