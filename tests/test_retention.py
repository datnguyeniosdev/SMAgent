"""Housekeeping: the daily retention pass deletes old finished work only; /list stays short (idle
projects on one line, closed lanes hidden); /forget drops an old project name."""

from datetime import timedelta

from app.models import Task, TaskState, now
from tests.test_config_gateway import gateway, make


def backdate(orch, kind, rid, days=40):
    orch.store._db.execute(
        "UPDATE records SET updated_at=? WHERE kind=? AND id=?",
        ((now() - timedelta(days=days)).isoformat(), kind, rid),
    )


def task(orch, state, days=40, parent=None):
    t = Task(title="t", description="d", state=state, parent_id=parent)
    t.updated_at = now() - timedelta(days=days)
    orch.tasks[t.task_id] = t
    orch.store.put("task", t.task_id, t, task_id=t.task_id, state=state.value)
    orch.store.put("step", f"{t.task_id}:s1", {"x": 1}, task_id=t.task_id, state="succeeded")
    orch.store.event("task.note", task_id=t.task_id)
    return t


def test_retention_deletes_old_finished_work_and_keeps_open_or_recent_work(settings, cfg_dir):
    orch, _ = make(settings, cfg_dir, [])
    old_done = task(orch, TaskState.succeeded)
    recent_done = task(orch, TaskState.succeeded, days=2)
    running_parent = task(orch, TaskState.running)
    old_child_of_running = task(orch, TaskState.succeeded, parent=running_parent.task_id)
    hub = orch.channel
    live, _, _ = hub.pull("Glazed Gems (GMA)", None, "/w/gems")
    for rid, state in [("m_done", "done"), ("m_pending", "pending"), ("m_recent", "done")]:
        orch.store.put("channel_msg", rid, {"message_id": rid, "state": state}, state=state)
    orch.store.put("channel_session", "pull_gone", {"project": "Old"}, state="open")
    for kind, rid in [("channel_msg", "m_done"), ("channel_msg", "m_pending"),
                      ("channel_session", "pull_gone"), ("channel_session", live.session_id)]:  # fmt: skip
        backdate(orch, kind, rid)
    orch.store._db.execute("UPDATE events SET ts=? WHERE task_id=?",
                           ((now() - timedelta(days=40)).isoformat(), old_done.task_id))  # fmt: skip

    counts = orch.purge_old(30)

    assert counts["task"] == 1 and old_done.task_id not in orch.tasks
    assert orch.store.get("task", old_done.task_id) is None
    assert orch.store.list("step", task_id=old_done.task_id) == []
    assert orch.store.events(old_done.task_id) == []
    for kept in (recent_done, running_parent, old_child_of_running):
        assert kept.task_id in orch.tasks and orch.store.get("task", kept.task_id)
    assert orch.store.get("channel_msg", "m_done") is None  # old and finished
    assert orch.store.get("channel_msg", "m_pending") and orch.store.get("channel_msg", "m_recent")
    assert orch.store.get("channel_session", "pull_gone") is None
    assert orch.store.get("channel_session", live.session_id)  # a live session keeps its record
    assert any(e["type"] == "retention.purge" for e in orch.store.events())


def test_list_shows_idle_projects_on_one_line_and_forget_drops_an_old_name(settings, cfg_dir):
    orch, _ = make(settings, cfg_dir, [])
    gw = gateway(orch)
    hub = orch.channel
    hub.pull("Zen Word (EG07)", None, "/w/eg07")
    old, _, _ = hub.pull("eg07-zen-word", None, "/w/eg07-old")
    hub.disconnect(old)  # renamed: nothing uses the old name any more

    text = gw._list_text()
    assert "<b>p1</b> · Zen Word (EG07) · 1 open" in text
    assert "💤 no session, no work: p2 eg07-zen-word" in text and "<b>p2</b>" not in text
    assert "<b>p2</b> · eg07-zen-word" in gw._list_text(full=True)

    assert hub.forget_project("Zen Word (EG07)") == "it still has an open session (/close it first)"
    assert hub.forget_project("eg07-zen-word") is None
    assert hub.projects() == [(1, "Zen Word (EG07)")]
    assert hub.resolve_project("eg07-zen-word") is None
    assert "eg07-zen-word" not in gw._list_text(full=True)


def test_forget_refuses_a_project_with_open_requests(settings, cfg_dir):
    orch, _ = make(settings, cfg_dir, [])
    hub = orch.channel
    s, _, _ = hub.pull("Old Name", None, "/w/old")
    hub.disconnect(s)
    hub.send("Old Name", "still waiting")
    assert "open requests" in hub.forget_project("Old Name")


def test_old_backups_go_after_three_days_by_the_time_in_their_name(settings, cfg_dir):
    from datetime import datetime

    orch, _ = make(settings, cfg_dir, [])
    d = orch.s.data_dir
    old = (datetime.now() - timedelta(days=4)).strftime("%Y%m%d-%H%M%S")
    new = datetime.now().strftime("%Y%m%d-%H%M%S")
    (d / f"state.db.bak-{old}").write_text("x")
    (d / f"artifacts.bak-{old}").mkdir()
    (d / f"artifacts.bak-{old}/a.png").write_text("x")
    (d / f"state.db.bak-{new}").write_text("x")
    (d / "notes.bak-later").write_text("x")  # not a timestamp: not ours
    assert orch.purge_backups() == 2
    assert sorted(p.name for p in d.glob("*.bak-*")) == ["notes.bak-later", f"state.db.bak-{new}"]
