"""Operator controls and delivery fixes: /off twins, replies reaching agents, answers pushed back,
idle sessions first, the work-only agent loop, deploy locks, stale approvals, review stop."""

import asyncio
import subprocess
import sys
from pathlib import Path

from mcp.client import Client

from app import deploys
from app.mcp_server import build_mcp_server
from tests.test_config_gateway import gateway, make

A = "2048 Shooter (K05)"


async def test_off_switches_the_window_s_twin_sessions(settings, cfg_dir):
    orch, _ = make(settings, cfg_dir, [])
    hub = orch.channel
    push = hub.connect("cc_a", A, "/w/a")
    pull, _, _ = hub.pull(A, None, cwd="/w/a")
    hub.set_enabled(push, False)
    assert not push.enabled and not pull.enabled
    _, target = hub.send(A, "work")
    assert target is None  # nothing in that window takes it


async def test_reply_to_an_agent_report_reaches_its_session(settings, cfg_dir):
    orch, _ = make(settings, cfg_dir, [])
    s, _, _ = orch.channel.pull(A, None)
    task = orch.register_external("Added pause menu", "mcp", A)  # reported without session_id
    rep = await orch.collab.submit_report(
        task_id=task.task_id, text="done", source="mcp", agent_status="completed"
    )
    fb = await orch.collab.submit_feedback(text="also a sound toggle", user_id="1", source="api",
                                           report_id=rep.report_id)  # fmt: skip
    assert fb.route_id == "channel" and fb.delivered
    _, msgs, _ = orch.channel.pull(A, s.session_id)
    assert "also a sound toggle" in msgs[0]["text"]


async def test_operator_answer_is_pushed_to_the_asker(settings, cfg_dir):
    orch, _ = make(settings, cfg_dir, [])
    gateway(orch)
    s, _, _ = orch.channel.pull(A, None)
    hi = await orch.collab.request_human_input("Which build?", options=["uat", "prod"], project=A,
                                               session_id=s.session_id)  # fmt: skip
    orch.collab.answer_human_input(hi.request_id, "prod, after the smoke test", "1", free=True)
    _, msgs, _ = orch.channel.pull(A, s.session_id)
    assert msgs[0]["kind"] == "answer" and "prod, after the smoke test" in msgs[0]["text"]


async def test_idle_session_is_preferred_and_fresh_runs_see_their_work(settings, cfg_dir):
    orch, _ = make(settings, cfg_dir, [])
    hub = orch.channel
    busy, _, _ = hub.pull(A, None, cwd="/w/a", agent="claude_code")
    hub.send(A, "first", session_id=busy.session_id)
    hub.pull(A, busy.session_id)  # now in progress
    idle, _, _ = hub.pull(A, None, cwd="/w/b", agent="claude_code")
    _, target = hub.send(A, "second")
    assert target.session_id == idle.session_id

    s, out, in_progress = hub.pull(
        A, None, "/w/a", 0, "claude_code"
    )  # the loop's peek: hands out nothing
    assert out == [] and in_progress and s.session_id == busy.session_id
    async with Client(build_mcp_server(orch)) as c:
        r = (
            await c.call_tool("get_messages", {"project": A, "session_id": busy.session_id})
        ).structured_content
    assert r["busy"] and r["in_progress_messages"][0]["text"].endswith("first")


def test_deploy_left_running_blocks_and_can_be_stopped(tmp_path, monkeypatch):
    repo = tmp_path / "game"
    repo.mkdir()
    deploys.worktree_dir(repo).parent.mkdir(parents=True)
    orphan = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    deploys.lock_file(repo).write_text(str(orphan.pid))
    assert deploys.busy(repo)  # e.g. after a gateway restart
    assert deploys.stop(repo)
    orphan.wait(5)
    assert not deploys.busy(repo) and not deploys.lock_file(repo).exists()


async def test_stale_approvals_expire_on_restart(settings, cfg_dir):
    orch, _ = make(settings, cfg_dir, [])
    orch.store.put("approval", "apr_0000000001", {"approval_id": "apr_0000000001", "task_id": None,
                                                   "state": "pending"}, state="pending")  # fmt: skip
    await orch.recover()
    assert orch.store.get("approval", "apr_0000000001")["state"] == "expired"


async def test_review_can_be_stopped(settings, cfg_dir, tmp_path):
    repo = tmp_path / "k05"
    script = repo / ".claude/skills/game-review/run.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/usr/bin/env bash\nsleep 30\n")
    script.chmod(0o755)
    orch, _ = make(settings, cfg_dir, [])
    gw = gateway(orch)
    orch.channel.pull(A, None, cwd=str(repo))
    r = orch.reviews.start(A, "playtest")
    for _ in range(50):
        if r["review_id"] in orch.reviews._procs:
            break
        await asyncio.sleep(0.02)
    assert orch.reviews.stop(r["review_id"])
    await asyncio.wait_for(orch.reviews._jobs[r["review_id"]], 10)
    done = orch.reviews.get(r["review_id"])
    assert done["state"] == "failed" and "stopped by the operator" in done["problems"]
    assert "Review failed" in gw.bot.sent[-1][1]["text"]
    assert Path(script).exists()
