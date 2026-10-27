"""Operator controls and delivery fixes: /off twins, replies reaching agents, answers pushed back,
idle sessions first, the work-only agent loop, deploy locks, stale approvals, review stop."""

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest
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


async def test_unanswered_question_is_sent_again_after_30_minutes(settings, cfg_dir):
    from datetime import timedelta

    from app.collaboration import HumanInput

    orch, _ = make(settings, cfg_dir, [])
    gw = gateway(orch)
    hi = await orch.collab.request_human_input(
        "Deploy lên UAT?", options=["Có", "Không"], project=A
    )
    assert await orch.collab.remind_questions() == 0  # just asked
    rec = HumanInput.model_validate(orch.store.get("human_input", hi.request_id))
    rec.created_at -= timedelta(minutes=31)
    orch.store.put("human_input", hi.request_id, rec, state="pending")
    before = len(gw.bot.sent)
    assert await orch.collab.remind_questions() == 1
    sent = gw.bot.sent[-1][1]
    assert (
        len(gw.bot.sent) == before + 1
        and "nhắc lại" in sent["text"]
        and "Deploy lên UAT?" in sent["text"]
    )
    assert sent["reply_markup"] is not None  # the option buttons come again
    assert await orch.collab.remind_questions() == 0  # next one 30 min later, not every round
    orch.collab.answer_human_input(hi.request_id, "Có", "1")
    rec = HumanInput.model_validate(orch.store.get("human_input", hi.request_id))
    rec.reminded_at -= timedelta(hours=1)
    orch.store.put("human_input", hi.request_id, rec, state=rec.status)
    assert await orch.collab.remind_questions() == 0  # answered: never again


async def test_idle_session_is_preferred_and_fresh_runs_see_their_work(settings, cfg_dir):
    orch, _ = make(settings, cfg_dir, [])
    hub = orch.channel
    hub.connect("cc_main", A, "/w/main", mode="push", notify=False)  # the main: it only routes
    busy, _, _ = hub.pull(A, None, cwd="/w/a", agent="claude_code")
    hub.send(A, "first", session_id=busy.session_id)
    hub.pull(A, busy.session_id)  # now in progress
    hub.pull(A, None, cwd="/w/b", agent="claude_code")
    route, target = hub.send(A, "second")  # project work: to the main, to route
    assert target.session_id == "cc_main"

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


async def test_html_escaped_project_name_reaches_the_same_session(settings, cfg_dir):
    """Codex (glm) sometimes sends "Stack &amp; Merge": it must still get that project's work."""
    orch, _ = make(settings, cfg_dir, [])
    p = "Coinlets: Stack & Merge (TCN)"
    async with Client(build_mcp_server(orch)) as c:
        first = await c.call_tool("get_messages", {"project": p, "agent": "codex", "cwd": "/w/c"})
        msg, _ = orch.channel.send(p, "report progress")
        args = {"project": "Coinlets: Stack &amp; Merge (TCN)", "agent": "codex", "cwd": "/w/c"}
        got = (await c.call_tool("get_messages", args)).structured_content
    assert got["session_id"] == first.structured_content["session_id"]
    assert [m["message_id"] for m in got["messages"]] == [msg["message_id"]]


async def test_one_line_answer_is_shown_in_full_in_a_quote_block(settings, cfg_dir):
    """Codex answers in one long line: it must not become a title cut at 120 characters."""
    orch, _ = make(settings, cfg_dir, [])
    gw = gateway(orch)
    task = orch.register_external("Telegram request", "mcp", A)
    text = "Dạ, " + "em đang chạy vòng theo dõi Telegram cho dự án Coinlets " * 4 + "ạ."
    await orch.collab.submit_report(task_id=task.task_id, text=text, source="codex (pull session)")
    sent = gw.bot.sent[-1][1]["text"]
    assert "<blockquote>" in sent and text.replace("&", "&amp;") in sent and "…" not in sent


async def test_quote_block_holds_only_the_agents_answer(settings, cfg_dir):
    """A short pull-session answer: the block shows the answer, the gateway note goes to the info line."""
    orch, _ = make(settings, cfg_dir, [])
    gw = gateway(orch)
    task = orch.register_external("Telegram request", "mcp", A)
    text = "Dạ, chào lại anh/chị ạ.\n\n(answer to chm_ff4acac414)"
    await orch.collab.submit_report(task_id=task.task_id, text=text, source="codex (pull session)")
    sent = gw.bot.sent[-1][1]["text"]
    block = sent.split("<blockquote>")[1].split("</blockquote>")[0]
    assert block == "Dạ, chào lại anh/chị ạ." and "trả lời chm_ff4acac414" in sent


async def test_unverified_reports_hide_the_gateway_label(settings, cfg_dir):
    """An agent's own report is never checked by the gateway: no 'chưa xác minh' noise."""
    orch, _ = make(settings, cfg_dir, [])
    gw = gateway(orch)
    task = orch.register_external("Telegram request", "mcp", A)
    await orch.collab.submit_report(
        task_id=task.task_id, text="Dạ, xong ạ", source="codex (pull session)"
    )
    sent = gw.bot.sent[-1][1]["text"]
    assert "từ codex (pull session)" in sent and "gateway:" not in sent


async def test_secure_confirmation_asks_the_passcode_in_private_and_cancels_after_3_tries(
    settings, cfg_dir
):
    from types import SimpleNamespace

    from aiogram.dispatcher.event.bases import SkipHandler
    from pydantic import SecretStr

    from app.collaboration import REFUSE, CollaborationError
    from app.telegram_bot import APPROVE_PASSCODE

    orch, _ = make(settings, cfg_dir, [])
    gw = gateway(orch)
    orch.s.telegram_confirm_passcode = SecretStr("Pa55-kode!")
    user = next(iter(orch.s.telegram_allowed_user_ids))
    s, _, _ = orch.channel.pull(A, None)
    ask = lambda: orch.collab.request_human_input(  # noqa: E731
        "Release lên prod?", options=["Release", "Huỷ"], project=A, session_id=s.session_id,
        secure=True)  # fmt: skip

    async def get_me():
        return SimpleNamespace(username="agw_bot")

    gw.bot.get_me = get_me
    group, private, deleted = [], [], []

    async def noop(*a, **k):
        return None

    async def group_answer(text, **k):
        group.append(text)

    def press(hi, idx):
        return SimpleNamespace(
            data=f"h:{hi.request_id}:{idx}", from_user=SimpleNamespace(id=user), answer=noop,
            message=SimpleNamespace(chat=SimpleNamespace(id=orch.s.telegram_control_chat_id),
                                    answer=group_answer),
        )  # fmt: skip

    def dm(text):
        async def answer(t, **k):
            private.append(t)

        async def delete():
            deleted.append(text)

        return SimpleNamespace(text=text, from_user=SimpleNamespace(id=user), answer=answer,
                               delete=delete, chat=SimpleNamespace(id=user, type="private"))  # fmt: skip

    hi = await ask()
    kb = gw.bot.sent[-1][1]["reply_markup"].inline_keyboard
    assert [b.text for row in kb for b in row] == [APPROVE_PASSCODE, REFUSE]
    assert "chat riêng" in gw.bot.sent[-1][1]["text"]
    try:  # a typed "Release" or the option itself does not approve it
        orch.collab.answer_human_input(hi.request_id, "Release", "1", free=True)
        raise AssertionError("approved without the passcode")
    except CollaborationError as e:
        assert e.code == "passcode_required"
    with pytest.raises(SkipHandler):  # no ✅ pressed: private text is not a passcode
        await gw.on_passcode_value(dm("Pa55-kode!"))

    await gw.on_question_button(press(hi, "p"))  # ✅: the passcode is asked for in private
    assert any(
        kw.get("chat_id") == user and hi.request_id in kw["text"] for _, kw, _ in gw.bot.sent
    )
    assert "chat riêng" in group[-1]
    await gw.on_passcode_value(dm("Pa55-kode!"))
    assert deleted == ["Pa55-kode!"] and "Đã duyệt" in private[-1]
    assert orch.collab.get_human_input(hi.request_id).status == "answered"
    _, msgs, _ = orch.channel.pull(A, s.session_id, limit=5)
    assert "đã xác minh passcode" in msgs[-1]["text"]

    hi = await ask()
    await gw.on_question_button(press(hi, "p"))
    for code, left in (("0000", "2"), ("1111", "1")):
        await gw.on_passcode_value(dm(code))
        assert f"còn {left} lần" in private[-1]
    await gw.on_passcode_value(dm("9999"))  # 3rd wrong try: cancelled, not approved
    assert "đã bị huỷ" in private[-1]
    assert orch.collab.get_human_input(hi.request_id).status == "cancelled"
    _, msgs, _ = orch.channel.pull(A, s.session_id, limit=5)
    assert "cancelled" in msgs[-1]["text"] and "NOT approved" in msgs[-1]["text"]
    with pytest.raises(SkipHandler):  # the wait ended with it
        await gw.on_passcode_value(dm("Pa55-kode!"))

    hi = await ask()
    orch.collab.answer_human_input(hi.request_id, REFUSE, "1")  # refusing needs no passcode
    rows = orch.store._db.execute(
        "SELECT body FROM records UNION ALL SELECT body FROM events"
    ).fetchall()
    assert not any("Pa55-kode!" in r[0] for r in rows)  # the passcode is never stored


async def test_question_answered_in_session_closes_the_telegram_copy(settings, cfg_dir):
    from datetime import timedelta

    from mcp.client import Client

    from app.collaboration import HumanInput
    from app.mcp_server import build_mcp_server

    orch, _ = make(settings, cfg_dir, [])
    gw = gateway(orch)
    s, _, _ = orch.channel.pull(A, None)
    hi = await orch.collab.request_human_input("Merge vào main?", options=["Có", "Không"], project=A,
                                               session_id=s.session_id)  # fmt: skip
    async with Client(build_mcp_server(orch)) as c:
        out = (await c.call_tool("close_question", {"request_id": hi.request_id,
                                                    "answer": "Có, merge đi"})).structured_content  # fmt: skip
    assert out["status"] == "answered" and "trả lời trong session" in gw.bot.sent[-1][1]["text"]
    rec = HumanInput.model_validate(orch.store.get("human_input", hi.request_id))
    rec.created_at -= timedelta(hours=1)
    orch.store.put("human_input", hi.request_id, rec, state=rec.status)
    assert await orch.collab.remind_questions() == 0  # no reminder for a closed question
    _, msgs, _ = orch.channel.pull(A, s.session_id, limit=5)
    assert msgs == []  # not pushed back to the agent that already has it


async def test_stalled_work_goes_to_the_operator_and_is_asked_again_when_back(settings, cfg_dir):
    """A rate-limited agent (StopFailure) leaves its work stalled, not stuck: Telegram shows it with
    move / wait / cancel; when the agent runs again the operator confirms before it goes on."""
    from types import SimpleNamespace

    from tests.test_config_gateway import CONTROL, USER

    orch, _ = make(settings, cfg_dir, [])
    gw, hub = gateway(orch), orch.channel
    hub.connect("cc_main", A, "/w/main", mode="push", notify=False)  # the main: it only routes
    claude, _, _ = hub.pull(A, None, "/w/k05", 0, "claude_code")
    codex, _, _ = hub.pull(A, None, "/w/k05", 0, "codex")
    m1, _ = hub.send(A, "fix the HUD", session_id=claude.session_id)
    m2, _ = hub.send(A, "then the menu", session_id=claude.session_id)
    hub.pull(A, claude.session_id, "/w/k05", 1, "claude_code")  # m1 in progress, m2 queued

    held = hub.stall(claude.session_id, "rate_limit · 429 Too Many Requests")
    assert {m["message_id"] for m in held} == {m1["message_id"], m2["message_id"]}
    assert not claude.deliverable and hub.session_for(A).session_id != claude.session_id
    await asyncio.sleep(0)
    notes = [kw for _, kw, _ in gw.bot.sent if "bị kẹt" in kw.get("text", "")]
    assert (
        len(notes) == 2 and "rate_limit" in notes[0]["text"] and "fix the HUD" in notes[0]["text"]
    )
    labels = [b.text for row in notes[0]["reply_markup"].inline_keyboard for b in row]
    assert any(t.startswith(f"↪️ s{codex.num}") for t in labels) and "❌ Huỷ" in labels

    answers = []

    async def q_answer(text=None, show_alert=False):
        answers.append(text)

    async def msg_answer(text, reply_markup=None):
        answers.append(text)

    def press(data):
        return SimpleNamespace(data=data, from_user=SimpleNamespace(id=USER), answer=q_answer,
                               message=SimpleNamespace(chat=SimpleNamespace(id=CONTROL), answer=msg_answer))  # fmt: skip

    await gw.on_stalled_button(press(f"st:to:{m2['message_id']}:{codex.num}"))  # m2 → Codex
    _, got, _ = hub.pull(A, codex.session_id, "/w/k05", 1, "codex")
    assert [m["message_id"] for m in got] == [m2["message_id"]]

    hub.pull(A, claude.session_id, "/w/k05", 1, "claude_code")  # Claude runs again: asked, once
    await asyncio.sleep(0)
    back = [kw for _, kw, _ in gw.bot.sent if "hoạt động lại" in kw.get("text", "")]
    assert len(back) == 1 and f"m{m1['num']}" in back[0]["text"]
    assert claude.deliverable and hub.active(claude.session_id) == []
    await gw.on_stalled_button(press(f"st:go:{m1['message_id']}"))  # ✅ go on there
    _, got, _ = hub.pull(A, claude.session_id, "/w/k05", 1, "claude_code")
    assert [m["message_id"] for m in got] == [m1["message_id"]]

    m3, _ = hub.send(A, "old task", session_id=claude.session_id)
    hub.stall(claude.session_id, "billing_error")
    await gw.on_stalled_button(press(f"st:cancel:{m3['message_id']}"))
    assert orch.store.get("channel_msg", m3["message_id"])["state"] == "cancelled"


def test_question_in_finds_the_question_an_agent_ends_on():
    from app.collaboration import question_in

    assert "commit luôn không?" in question_in("Đã sửa HUD.\n\nAnh muốn em commit luôn không?")
    assert question_in("Done: tests pass.\n\nNothing else to do.") is None
    assert "Should I deploy" in question_in(
        "Built.\n\nShould I deploy it to UAT now? It takes 5 minutes."
    )


async def test_a_question_asked_only_in_the_chat_reaches_telegram_and_its_answer_the_session(
    settings, cfg_dir
):
    from pydantic import SecretStr

    orch, _ = make(settings, cfg_dir, [])
    gw, hub = gateway(orch), orch.channel
    orch.s.telegram_confirm_passcode = SecretStr("Pa55-kode!")
    codex, _, _ = hub.pull(A, None, "/w/k05", 0, "codex")
    collab = orch.collab

    hi = await collab.turn_ended(
        A, codex.session_id, codex.title, "Xong.\n\nAnh có muốn em đổi màu nền không?"
    )
    assert hi.auto and not hi.secure and "đổi màu nền" in gw.bot.sent[-1][1]["text"]
    collab.answer_human_input(hi.request_id, "✅ Đồng ý", "11")
    _, msgs, _ = hub.pull(A, codex.session_id, "/w/k05", 5, "codex")  # the answer wakes Codex
    assert any("✅ Đồng ý" in m["text"] for m in msgs)

    risky = await collab.turn_ended(
        A, codex.session_id, codex.title, "Em commit và push lên main nhé?"
    )
    assert risky.secure  # commit/push: passcode on Telegram
    # the next turn ends without the Telegram answer: it was answered in the chat, the copy closes
    assert await collab.turn_ended(A, codex.session_id, codex.title, "Đã push xong.") is None
    assert collab.get_human_input(risky.request_id).status == "answered"

    own = await collab.request_human_input("Release?", project=A, session_id=codex.session_id)
    assert await collab.turn_ended(A, codex.session_id, codex.title, "Release luôn không?") is None
    assert collab.get_human_input(own.request_id).status == "pending"  # its own question stays
