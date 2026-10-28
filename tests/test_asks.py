"""Agent-to-agent asks: routing, answers, the watchdog and the operator's buttons."""

from datetime import timedelta
from types import SimpleNamespace

from mcp.client import Client

from app import asks as asks_mod
from app.mcp_server import build_mcp_server
from tests.test_config_gateway import CONTROL, DISCUSSION, USER, gateway, make, reply_msg

A, B = "2048 Shooter (K05)", "PocketSort (ttg-002-coin_sort)"


def call(c):
    async def tool(name, args):
        r = await c.call_tool(name, args)
        assert not r.is_error, r.content[0].text
        return r.structured_content

    return tool


async def test_agent_asks_agent_and_gets_the_answer(settings, cfg_dir):
    orch, _ = make(settings, cfg_dir, [])
    gateway(orch)
    async with Client(build_mcp_server(orch)) as c:
        t = call(c)
        a_sid = (await t("get_messages", {"project": A, "agent": "claude_code"}))["session_id"]
        b_sid = (await t("get_messages", {"project": B, "agent": "opencode"}))["session_id"]
        agents = (await t("list_agents", {}))["agents"]
        assert {x["project"]: x["agent"] for x in agents} == {A: "claude_code", B: "opencode"}
        b_id = next(x["id"] for x in agents if x["project"] == B)

        ask = await t(
            "ask_agent",
            {"to": b_id, "request": "Which coin sprite size do you use?", "session_id": a_sid},
        )
        assert ask["state"] == "delivered" and ask["delivered_to"] == b_sid

        got = (await t("get_messages", {"project": B, "session_id": b_sid}))["messages"]
        assert got[0]["meta"]["kind"] == "request" and "Which coin sprite size" in got[0]["text"]
        assert "s1 (2048 Shooter (K05))" in got[0]["text"]  # who is asking
        res = await t(
            "reply_message",
            {"session_id": b_sid, "message_id": got[0]["message_id"], "text": "128 px"},
        )
        assert res["answered"] and res["ask_id"] == ask["ask_id"]
        assert (
            orch.collab.reports_for(orch.channel.sessions[b_sid].task_id) == []
        )  # no Telegram post

        back = await t("get_messages", {"project": A, "session_id": a_sid})
        msg = back["messages"][0]
        assert (
            msg["meta"]["kind"] == "answer"
            and "128 px" in msg["text"]
            and "needs no reply" in msg["text"]
        )
        again = await t("get_messages", {"project": A, "session_id": a_sid})
        assert "busy" not in again  # answers don't block
        assert (await t("get_ask", {"ask_id": ask["ask_id"]}))["answer"] == "128 px"
        bad = await c.call_tool("ask_agent", {"to": "nobody-here", "request": "x"})
        assert bad.is_error and "unknown agent or project" in bad.content[0].text


async def test_unreachable_agent_is_escalated_with_buttons_and_the_operator_answers(
    settings, cfg_dir, monkeypatch
):
    monkeypatch.setattr(asks_mod, "UNREACHABLE", timedelta(0))
    orch, _ = make(settings, cfg_dir, [])
    orch.asks.started -= timedelta(hours=1)  # past the restart grace
    gw = gateway(orch)
    hub = orch.channel
    a, _, _ = hub.pull(A, None)
    hub.store_b = hub.connect("cc_b", B, "/w/k09")  # B was seen once, then closed
    hub.disconnect(hub.store_b)

    ask = orch.asks.create(B, "Export the coin atlas", from_session=a.session_id)
    assert ask["state"] == "queued"
    assert await orch.asks.check() == [ask["ask_id"]]
    kind, kw, mid = gw.bot.sent[-1]
    assert kw["chat_id"] == CONTROL and kw["text"].startswith("🆘 <b>AGENT ĐANG CHỜ</b>")
    assert "no open session" in kw["text"] and "Export the coin atlas" in kw["text"]
    labels = [b.text for row in kw["reply_markup"].inline_keyboard for b in row]
    assert labels == ["🔁 Resend", "➡️ Other session", "🚀 Open it", "✍️ Answer myself", "❌ Cancel"]
    assert await orch.asks.check() == []  # escalated once, then it waits for the operator

    # the operator replies to the escalation with a description: that is the answer for A
    target = SimpleNamespace(chat=SimpleNamespace(id=CONTROL), message_id=mid, forward_origin=None,
                             from_user=SimpleNamespace(id=USER), text=kw["text"], caption=None)  # fmt: skip
    orch.s.telegram_discussion_chat_id = CONTROL  # single-group mode
    m, mkw = reply_msg("Use the 2048 atlas, 128 px tiles", target, chat=CONTROL)
    await gw.on_discussion(m, **mkw)
    assert "Sent to s1" in m.replies[-1]
    done = orch.asks.get(ask["ask_id"])
    assert done["state"] == "answered" and done["answered_by"] == "the operator"
    _, msgs, _ = hub.pull(A, a.session_id)
    assert "Use the 2048 atlas" in msgs[0]["text"] and msgs[0]["kind"] == "answer"


async def test_buttons_resend_retarget_cancel_and_late_session(settings, cfg_dir, monkeypatch):
    monkeypatch.setattr(asks_mod, "UNREACHABLE", timedelta(0))
    orch, _ = make(settings, cfg_dir, [])
    gw = gateway(orch)
    hub = orch.channel
    a, _, _ = hub.pull(A, None)
    hub.disconnect(hub.connect("cc_b", B, "/w/k09"))
    alerts = []

    async def q_answer(text=None, show_alert=False):
        alerts.append(text)

    async def msg_answer(text, reply_markup=None):
        alerts.append((text, reply_markup))
        return SimpleNamespace(message_id=999)

    def press(data):
        return SimpleNamespace(data=data, from_user=SimpleNamespace(id=USER), answer=q_answer,
                               message=SimpleNamespace(chat=SimpleNamespace(id=CONTROL), answer=msg_answer,
                                                       edit_reply_markup=lambda reply_markup=None: _noop()))  # fmt: skip

    async def _noop():
        return None

    # a session for B opens before the operator acts: the watchdog delivers there by itself
    ask = orch.asks.create(B, "ping", from_session=a.session_id)
    b2 = hub.connect("cc_b2", B, "/w/k09")
    assert await orch.asks.check() == [] and orch.asks.get(ask["ask_id"])["delivered_to"] == "cc_b2"

    # other session: the operator picks one from a list, the request moves there
    other = hub.connect("cc_c", "Other game", "/w/other")
    orch.asks.get(ask["ask_id"])
    await orch.asks.escalate(orch.asks.get(ask["ask_id"]), "test")
    await gw.on_ask_button(press(f"k:other:{ask['ask_id']}"))
    text, kb = alerts[-1]
    nums = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"k:to:{ask['ask_id']}:{other.num}" in nums
    await gw.on_ask_button(press(f"k:to:{ask['ask_id']}:{other.num}"))
    assert orch.asks.get(ask["ask_id"])["delivered_to"] == "cc_c" and "Resent to s" in alerts[-1]

    # cancel: A is told, the request leaves the other session's queue
    await gw.on_ask_button(press(f"k:cancel:{ask['ask_id']}"))
    assert orch.asks.get(ask["ask_id"])["state"] == "cancelled" and other.queue.empty()
    _, msgs, _ = hub.pull(A, a.session_id)
    assert "was cancelled" in msgs[-1]["text"]
    await gw.on_ask_button(press(f"k:resend:{ask['ask_id']}"))
    assert alerts[-1] == "Already cancelled."
    assert b2.num and DISCUSSION  # sessions stay listed


async def test_no_escalation_right_after_a_restart(settings, cfg_dir, monkeypatch):
    monkeypatch.setattr(asks_mod, "UNREACHABLE", timedelta(0))
    orch, _ = make(settings, cfg_dir, [])
    gateway(orch)
    hub = orch.channel
    a, _, _ = hub.pull(A, None)
    hub.disconnect(hub.connect("cc_b", B, "/w/k09"))
    orch.asks.create(B, "ping", from_session=a.session_id)
    assert await orch.asks.check() == []  # sessions have not polled again yet
    orch.asks.started -= timedelta(hours=1)
    assert len(await orch.asks.check()) == 1


async def test_a_holder_working_through_a_restart_keeps_its_ask(settings, cfg_dir):
    """Seen in prod: a child had the ask, the gateway restarted, the child kept working without polling;
    past the deadline the ask was resent to the main, cancelling the child's delivery."""
    orch, _ = make(settings, cfg_dir, [])
    orch.asks.started -= timedelta(hours=1)
    gateway(orch)
    hub = orch.channel
    a, _, _ = hub.pull(A, None, "/w/a", 0, "claude_code")
    hub.pull(B, None, "/w/b", 0, "claude_code")
    hub.pull(B, None, "/w/b/.agw-children/qa-1", 0, "claude_code")
    ask = orch.asks.create(B, "re-test", from_session=a.session_id)
    holder = ask["delivered_to"]
    hub.pull(B, holder)
    hub.sessions.pop(holder)  # restart: the holder has not polled again, it is working
    rec = orch.asks.get(ask["ask_id"])
    rec["deadline"] = (asks_mod.now() - timedelta(seconds=1)).isoformat()
    orch.asks._save(rec)
    assert await orch.asks.check() == []
    assert orch.asks.get(ask["ask_id"])["message_id"] == ask["message_id"]  # not resent to the main
    rec = orch.asks.get(ask["ask_id"])
    rec["sent_at"] = (asks_mod.now() - asks_mod.HARD_CAP - timedelta(seconds=1)).isoformat()
    orch.asks._save(rec)
    await orch.asks.check()
    assert orch.asks.get(ask["ask_id"])["message_id"] != ask["message_id"]  # past the cap: moved on


async def test_two_busy_agents_asking_each_other_reach_the_operator(settings, cfg_dir):
    """A waits for B and B waits for A, both busy with their own job: no deadlock. Live sessions are
    given until the hard cap (a long QA pass is not stuck), then each ask goes to the operator."""
    orch, _ = make(settings, cfg_dir, [])
    orch.asks.started -= timedelta(hours=1)  # past the restart grace
    gateway(orch)
    hub = orch.channel
    a, _, _ = hub.pull(A, None, "/w/a", 0, "claude_code")
    b, _, _ = hub.pull(B, None, "/w/b", 0, "claude_code")
    for s in (a, b):  # each is busy with a job of its own
        hub.send(s.project, "own job", session_id=s.session_id)
        hub.pull(s.project, s.session_id)
    ab = orch.asks.create(B, "need your atlas", from_session=a.session_id, wait_s=60)
    ba = orch.asks.create(A, "need your palette", from_session=b.session_id, wait_s=60)
    assert await orch.asks.check() == []  # before the deadline: waiting is fine
    for ask in (ab, ba):  # past the deadline, but both sessions are live: still working on it
        rec = orch.asks.get(ask["ask_id"])
        rec["deadline"] = (asks_mod.now() - timedelta(seconds=1)).isoformat()
        orch.asks._save(rec)
    assert await orch.asks.check() == []
    for ask in (ab, ba):  # the hard cap since delivery: the operator decides
        rec = orch.asks.get(ask["ask_id"])
        rec["sent_at"] = (asks_mod.now() - asks_mod.HARD_CAP - timedelta(seconds=1)).isoformat()
        orch.asks._save(rec)
    assert sorted(await orch.asks.check()) == sorted([ab["ask_id"], ba["ask_id"]])
    assert {orch.asks.get(x["ask_id"])["state"] for x in (ab, ba)} == {"escalated"}


async def test_an_ask_answered_another_way_frees_the_session_that_holds_it(settings, cfg_dir):
    orch, _ = make(settings, cfg_dir, [])
    hub = orch.channel
    a, _, _ = hub.pull(A, None, "/w/a")
    b, _, _ = hub.pull(B, None, "/w/b")
    ask = orch.asks.create(B, "Progress?", from_session=a.session_id)
    _, got, _ = hub.pull(B, b.session_id)
    assert got[0]["ask_id"] == ask["ask_id"] and hub.in_progress(b.session_id)  # B holds it: busy

    orch.asks.answer(ask["ask_id"], "done", by="the operator")  # not through B's reply_message
    assert orch.store.get("channel_msg", ask["message_id"])["state"] == "done"
    assert hub.in_progress(b.session_id) == []  # free for its next job


async def test_asks_go_through_the_main_and_a_conversation_keeps_its_partner(settings, cfg_dir):
    orch, _ = make(settings, cfg_dir, [])
    hub = orch.channel
    a, _, _ = hub.pull(A, None, "/w/a")
    main, _, _ = hub.pull(B, None, "/w/b")  # B's main: it routes
    kid, _, _ = hub.pull(B, None, "/w/b-kid", 0, "codex")  # a session the main hands work to
    assert hub.main_for(B) is main

    first = orch.asks.create(B, "What coin size?", from_session=a.session_id)
    assert first["delivered_to"] == main.session_id  # through the main
    msg = orch.store.get("channel_msg", first["message_id"])
    assert msg.get("route")  # with the routing note: the main dispatches it, never does it
    hub.delivered(msg, kid.session_id)  # (what dispatch does: the kid gets it)
    await hub.reply(kid.session_id, "128 px", first["message_id"])

    again = orch.asks.create(B, "And the font?", from_session=a.session_id)
    assert (
        again["to_session"] == again["delivered_to"] == kid.session_id
    )  # same partner, its context

    fresh = orch.asks.create(
        B, "New topic: sounds", from_session=a.session_id, new_conversation=True
    )
    assert fresh["delivered_to"] == main.session_id  # a new conversation: routed by the main again

    hub.disconnect(kid)  # the partner is gone: the main routes the next one
    later = orch.asks.create(B, "Still there?", from_session=a.session_id)
    assert later["delivered_to"] == main.session_id


async def test_a_request_to_a_project_with_core_skills_tells_its_agent_to_load_them(
    settings, cfg_dir
):
    orch, _ = make(settings, cfg_dir, [])
    orch.s.core_skills = {B: ["uiux-art-director", "game-ui-quality-gate"]}
    hub = orch.channel
    a, _, _ = hub.pull(A, None, "/w/a")
    b, _, _ = hub.pull(B, None, "/w/b")
    ask = orch.asks.create(B, "Review the shop screen", from_session=a.session_id)
    text = orch.store.get("channel_msg", ask["message_id"])["text"]
    assert (
        "load `uiux-art-director`, `game-ui-quality-gate`" in text
        and "follow `uiux-art-director`" in text
    )
    other, _ = hub.send(A, "plain work")  # a project without core skills: unchanged
    assert "Core skills" not in other["text"]
    orch.asks.answer(ask["ask_id"], "done", by="B")  # an answer is not work: no note
    answer = [m for m in orch.store.list("channel_msg") if m.get("kind") == "answer"][-1]
    assert "Core skills" not in answer["text"]
