"""What the operator sees on Telegram, and the routing fixes behind it: every agent ask and answer is
copied to the group, sessions are announced, failed sends are reported, pull sessions stay apart."""

import asyncio
from types import SimpleNamespace

import pytest

from app.asks import AskError
from app.channel import ChannelHub
from app.collaboration import CollaborationError
from tests.test_config_gateway import CONTROL, USER, gateway, make, reply_msg

A, B = "2048 Shooter (K05)", "PocketSort (ttg-002-coin_sort)"


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


def texts(gw):
    return [kw.get("text", "") for _, kw, _ in gw.bot.sent]


async def test_asks_and_answers_are_copied_and_sessions_announced(settings, cfg_dir):
    orch, _ = make(settings, cfg_dir, [])
    gw = gateway(orch)
    hub = orch.channel
    a, _, _ = hub.pull(A, None, agent="claude_code")
    b, _, _ = hub.pull(B, None, agent="opencode")
    await settle()
    assert sum("connected" in t for t in texts(gw)) == 2 and any(
        "OpenCode(CLI)-" in t for t in texts(gw)
    )

    ask = orch.asks.create("agent:opencode", "Coin size?", from_session=a.session_id)
    assert ask["delivered_to"] == b.session_id  # agent:<tool> names the project; its main has it
    await settle()
    kind, kw, mid = gw.bot.sent[-1]
    assert kw["chat_id"] == CONTROL and "AGENT NHỜ" in kw["text"] and "Coin size?" in kw["text"]
    assert "/asks" not in kw["text"] and ask["ask_id"] in gw._asks_text()

    orch.asks.answer(ask["ask_id"], "128 px", by="s2 (PocketSort)")
    await settle()
    assert "AGENT TRẢ LỜI" in texts(gw)[-1] and "128 px" in texts(gw)[-1]
    assert gw._asks_text() == "No open agent requests."

    with pytest.raises(AskError):  # the asker is never its own target
        orch.asks.create("agent:claude_code", "x", from_session=a.session_id)


async def test_failed_sends_are_not_silent(settings, cfg_dir):
    orch, _ = make(settings, cfg_dir, [])
    gw = gateway(orch)
    a, _, _ = orch.channel.pull(A, None)
    orch.channel.disconnect(orch.channel.connect("cc_b", B, "/w/k09"))  # B known, no session open

    async def refuse(**kw):
        raise RuntimeError("Bad Gateway")

    ask = orch.asks.create(B, "ping", from_session=a.session_id)
    gw.bot.send_message = refuse
    await orch.asks.escalate(orch.asks.get(ask["ask_id"]), "test")
    left = orch.asks.get(ask["ask_id"])
    assert left["state"] != "escalated" and left.get("escalation_failed_at")  # retried later

    with pytest.raises(CollaborationError) as e:
        await orch.collab.request_human_input("Ship it?", project=A)
    assert e.value.code == "delivery_failed"


async def test_question_buttons_and_reply_answer(settings, cfg_dir):
    orch, _ = make(settings, cfg_dir, [])
    gw = gateway(orch)
    hi = await orch.collab.request_human_input("Which build?", options=["uat", "prod"], project=A)
    _, kw, mid = gw.bot.sent[-1]
    assert [b.callback_data for row in kw["reply_markup"].inline_keyboard for b in row] == [
        f"h:{hi.request_id}:0",
        f"h:{hi.request_id}:1",
    ]
    alerts = []

    async def note(text=None, show_alert=False, **_):
        alerts.append(text)

    q = SimpleNamespace(data=f"h:{hi.request_id}:1", from_user=SimpleNamespace(id=USER), answer=note,
                        message=SimpleNamespace(chat=SimpleNamespace(id=CONTROL), answer=note))  # fmt: skip
    await gw.on_question_button(q)
    assert orch.collab.get_human_input(hi.request_id).response == "prod"

    gw.bot.id = 42  # replies count only on the bot's own messages
    hi2 = await orch.collab.request_human_input("Release notes?", project=A)
    _, kw, mid = gw.bot.sent[-1]
    target = SimpleNamespace(chat=SimpleNamespace(id=CONTROL), message_id=mid, forward_origin=None,
                             from_user=SimpleNamespace(id=gw.bot.id), text=kw["text"], caption=None)  # fmt: skip
    orch.s.telegram_discussion_chat_id = CONTROL
    m, mkw = reply_msg("Only the bug fixes", target, chat=CONTROL)
    await gw.on_discussion(m, **mkw)
    assert orch.collab.get_human_input(hi2.request_id).response == "Only the bug fixes"


def test_pull_sessions_are_kept_apart_and_answer_only_their_own():
    assert ChannelHub.pull_id(A, "claude_code", "/w/a") != ChannelHub.pull_id(
        A, "claude_code", "/w/b"
    )
    assert ChannelHub.pull_id(A, "claude_code", "/w/a") != ChannelHub.pull_id(A, "opencode", "/w/a")
    assert ChannelHub.pull_id(A, "claude_code", "/w/a") == ChannelHub.pull_id(
        A, "claude_code", "/w/a"
    )


async def test_reply_ownership(settings, cfg_dir):
    orch, _ = make(settings, cfg_dir, [])
    hub = orch.channel
    one, _, _ = hub.pull(A, None, agent="claude_code", cwd="/w/a")
    two, _, _ = hub.pull(A, None, agent="opencode", cwd="/w/b")
    hub.send(A, "do it", session_id=one.session_id)
    _, msgs, _ = hub.pull(A, one.session_id)
    with pytest.raises(PermissionError):
        await hub.reply(two.session_id, "done", msgs[0]["message_id"])


def test_deploy_env_drops_gateway_secrets(monkeypatch):
    from app.deploys import deploy_env

    env = deploy_env(
        {"PATH": "/bin", "TELEGRAM_BOT_TOKEN": "x", "AGW_SECRET_KEY": "y", "CLAUDECODE": "1"}
    )
    assert env == {"PATH": "/bin"}


def test_child_processes_get_no_gateway_secret(monkeypatch):
    from app import keys
    from app.config import child_env, secret_env_names

    monkeypatch.setattr(keys, "RUNTIME", {"SET_WITH_KEYS"})  # a key saved with /keys
    assert {"OPERATOR_TOKEN", "TELEGRAM_CONFIRM_PASSCODE", "OPENCODE_PASSWORD", "AGW_SECRET_KEY",
            "SET_WITH_KEYS"} <= secret_env_names()  # fmt: skip
    env = {n: "x" for n in secret_env_names() | {"PATH", "ANTHROPIC_API_KEY", "AGW_API_TOKEN"}}
    assert child_env(env) == {"PATH": "x", "ANTHROPIC_API_KEY": "x", "AGW_API_TOKEN": "x"}


async def test_agents_cannot_cancel_the_operators_tasks(settings, cfg_dir):
    from mcp.client import Client

    from app.mcp_server import build_mcp_server

    orch, _ = make(settings, cfg_dir, [])
    task = orch.register_external("from Telegram", str(USER), A)
    async with Client(build_mcp_server(orch)) as c:
        r = await c.call_tool("cancel_task", {"task_id": task.task_id})
    assert r.is_error and "only they can cancel" in r.content[0].text
