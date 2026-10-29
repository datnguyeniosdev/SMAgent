"""🌙 Auto-accept: switched on with the passcode (private chat), off at once; while on every question
is answered (secure ones approved, else the first option) so agents do not wait."""

from types import SimpleNamespace

from pydantic import SecretStr

from app.collaboration import APPROVED, AUTO_ANSWER
from tests.test_config_gateway import gateway, make

A = "2048 Shooter (K05)"


async def test_auto_accept_needs_the_passcode_and_answers_every_question_while_on(
    settings, cfg_dir
):
    orch, _ = make(settings, cfg_dir, [])
    gw = gateway(orch)
    orch.s.telegram_confirm_passcode = SecretStr("Pa55-kode!")
    user = next(iter(orch.s.telegram_allowed_user_ids))
    s, _, _ = orch.channel.pull(A, None)
    collab = orch.collab
    private, group = [], []

    async def noop(*a, **k):
        return None

    async def group_answer(text, **k):
        group.append(text)

    def press():
        return SimpleNamespace(data="m:auto", from_user=SimpleNamespace(id=user), answer=noop,
                               message=SimpleNamespace(chat=SimpleNamespace(id=orch.s.telegram_control_chat_id),
                                                       answer=group_answer))  # fmt: skip

    def dm(text):
        async def answer(t, **k):
            private.append(t)

        return SimpleNamespace(text=text, from_user=SimpleNamespace(id=user), answer=answer,
                               delete=noop, chat=SimpleNamespace(id=user, type="private"))  # fmt: skip

    waiting = await collab.request_human_input("Màu nút?", options=["Xanh", "Đỏ"], project=A,
                                               session_id=s.session_id)  # fmt: skip
    assert collab.get_human_input(waiting.request_id).status == "pending"  # off: it waits

    await gw.on_menu_button(press())  # On asks the passcode in private
    assert not collab.auto_accept() and any(kw.get("chat_id") == user for _, kw, _ in gw.bot.sent)
    await gw.on_passcode_value(dm("0000"))
    assert "còn 2 lần" in private[-1] and not collab.auto_accept()
    await gw.on_passcode_value(dm("Pa55-kode!"))
    assert collab.auto_accept() and "bật" in private[-1]
    assert collab.get_human_input(waiting.request_id).response == "Xanh"  # the waiting one too

    secure = await collab.request_human_input(
        "Release?", project=A, session_id=s.session_id, secure=True
    )
    free = await collab.request_human_input("Tên level?", project=A, session_id=s.session_id)
    assert secure.status == "answered" and secure.response == APPROVED
    assert free.response == AUTO_ANSWER and "Auto-accept đang bật" in gw.bot.sent[-1][1]["text"]

    await gw.on_menu_button(press())  # Off: at once, no passcode
    assert not collab.auto_accept()
    later = await collab.request_human_input("Ok?", options=["Có", "Không"], project=A)
    assert later.status == "pending"
    rows = orch.store._db.execute(
        "SELECT body FROM records UNION ALL SELECT body FROM events"
    ).fetchall()
    assert not any("Pa55-kode!" in r[0] for r in rows)


async def test_an_answered_question_loses_its_telegram_buttons(settings, cfg_dir):
    import asyncio

    orch, _ = make(settings, cfg_dir, [])
    gw = gateway(orch)
    s, _, _ = orch.channel.pull(A, None)
    collab = orch.collab

    def dropped():
        return [kw["message_id"] for kind, kw, _ in gw.bot.sent if kind == "markup"]

    on_tg = await collab.request_human_input(
        "Màu?", options=["Xanh", "Đỏ"], project=A, session_id=s.session_id
    )
    mid = gw.bot.sent[-1][2]
    assert collab.get_human_input(on_tg.request_id).tg_messages == [mid]
    collab.answer_human_input(on_tg.request_id, "Xanh", "1")  # a button on Telegram
    await asyncio.sleep(0)
    assert dropped() == [mid]

    in_app = await collab.request_human_input(
        "Commit?", project=A, session_id=s.session_id, secure=True
    )
    mid2 = gw.bot.sent[-1][2]
    await collab.close_human_input(in_app.request_id, "yes, in the session")  # answered in the app
    await asyncio.sleep(0)
    assert dropped() == [mid, mid2]


async def test_a_question_answered_in_the_session_is_not_resent_once_the_session_moves_on(
    settings, cfg_dir
):
    """Seen in prod: approvals answered in the app, no close_question, so the Telegram copy was
    reminded 30 minutes later and looked like a duplicate."""
    import asyncio

    from mcp.client import Client

    from app.mcp_server import build_mcp_server

    orch, _ = make(settings, cfg_dir, [])
    gw = gateway(orch)
    s, _, _ = orch.channel.pull(A, None, "/w/a")
    collab = orch.collab
    first = await collab.request_human_input(
        "Commit theme A?", project=A, session_id=s.session_id, secure=True
    )
    second = await collab.request_human_input(
        "Commit theme B?", project=A, session_id=s.session_id, secure=True
    )
    await asyncio.sleep(0)
    old = collab.get_human_input(first.request_id)
    assert old.status == "answered" and old.responder == "session"  # settled before the new one
    assert collab.get_human_input(second.request_id).status == "pending"
    assert any(kind == "markup" for kind, _, _ in gw.bot.sent)  # its buttons are gone

    async with Client(build_mcp_server(orch)) as c:
        await c.call_tool("submit_report", {"text": "Dạ, đang làm", "project": A,
                                            "session_id": s.session_id, "agent_status": "in_progress"})  # fmt: skip
        assert (
            collab.get_human_input(second.request_id).status == "pending"
        )  # progress: still waiting
        await c.call_tool("submit_report", {"text": "Dạ, đã commit theme B", "project": A,
                                            "session_id": s.session_id})  # fmt: skip
    assert collab.get_human_input(second.request_id).status == "answered"
    assert (
        await collab.remind_questions(after=__import__("datetime").timedelta(0)) == 0
    )  # nothing to resend
