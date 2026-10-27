"""Cursor: sessionStart gives each conversation the gateway rules; stop waits for Telegram work and
returns it as a followup_message, which Cursor submits so the conversation picks it up by itself."""

import io
import json

from app import cursor_hooks as ch
from app import projects
from app import waiter as w
from tests.test_waiter import feed


def folder(tmp_path, monkeypatch):
    (tmp_path / "mario").mkdir()
    (tmp_path / "mario/AGENTS.md").write_text("x")
    (tmp_path / "mario/README.md").write_text("# Mario Clone (K10)\n")
    monkeypatch.setattr(projects, "projects_root", lambda: tmp_path)
    monkeypatch.setattr(w, "WAITERS", tmp_path / "waiters")
    return {"workspace_roots": [str(tmp_path / "mario")], "conversation_id": "c1"}


def test_session_start_names_the_project_and_rules(tmp_path, monkeypatch):
    ev = folder(tmp_path, monkeypatch)
    out = ch.hook({**ev, "hook_event_name": "sessionStart"}, 8000)
    ctx = out["additional_context"]
    assert (
        "'Mario Clone (K10)'" in ctx
        and "agent 'cursor'" in ctx
        and "pull_marioclonek10_cursor_" in ctx
    )
    assert "submit_report" in ctx  # the gateway rules, no pasting into Cursor's UI
    assert (
        ch.hook({"hook_event_name": "sessionStart", "workspace_roots": ["/elsewhere"]}, 8000) == {}
    )


def test_stop_returns_new_work_as_a_followup_once(tmp_path, monkeypatch):
    ev = {**folder(tmp_path, monkeypatch), "hook_event_name": "stop", "status": "completed"}
    feed(monkeypatch, [{"queued": False}, {"queued": True, "next": "chm_1"}])
    out = ch.hook(ev, 8000)
    assert "chm_1" in out["followup_message"] and "agent 'cursor'" in out["followup_message"]
    assert "wakes this conversation again" in out["followup_message"]
    # the same message, just woken for: held, not returned again (many rounds within the hold)
    feed(monkeypatch, [{"queued": True, "next": "chm_1"}] * 100000)
    assert ch.stop(ev, 8000, hold=0.01) == {}
    state = json.loads(next((tmp_path / "waiters").glob("*.json")).read_text())
    assert state["woke"] == "chm_1"


def test_stop_after_the_user_aborted_does_not_continue(tmp_path, monkeypatch):
    ev = folder(tmp_path, monkeypatch)
    assert ch.hook({**ev, "hook_event_name": "stop", "status": "aborted"}, 8000) == {}


def test_cli_reads_the_event_from_stdin(tmp_path, monkeypatch, capsys):
    from app.cli import main

    ev = {**folder(tmp_path, monkeypatch), "hook_event_name": "sessionStart"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(ev)))
    assert main(["cursor-hook"]) == 0
    assert "Mario Clone (K10)" in json.loads(capsys.readouterr().out)["additional_context"]


def test_stop_with_an_error_hands_the_work_to_the_operator(tmp_path, monkeypatch):
    ev = {**folder(tmp_path, monkeypatch), "hook_event_name": "stop", "status": "error"}
    posts = feed(monkeypatch, [])
    assert ch.hook(ev, 8000) == {}  # no waiting, no followup
    assert len(posts) == 1 and "/api/v1/channel/stalled?" in posts[0] and "agent=cursor" in posts[0]
    posts = feed(monkeypatch, [{"queued": True, "next": "chm_9"}])
    ch.hook({**ev, "status": "completed"}, 8000)  # a completed turn: the model works again
    assert "/api/v1/channel/active?" in posts[0]
