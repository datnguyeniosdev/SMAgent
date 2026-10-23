"""Telegram gateway (fake bot, no network) and REST API."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import SendMessage
from fastapi.testclient import TestClient

from app import telegram_bot
from app.config import Settings, get_settings
from app.models import TaskState
from app.telegram_bot import TelegramGateway
from tests.conftest import MODEL, FakeConnector, build_orch, make_png

CONTROL, CHANNEL, USER, STRANGER = -100, -200, 11, 99


class FakeBot:
    def __init__(self, fail=()):
        self.sent = []
        self.fail = list(fail)
        self.files = {}

    async def _record(self, kind, **kw):
        if self.fail:
            raise self.fail.pop(0)
        self.sent.append((kind, kw))

    async def send_message(self, **kw):
        await self._record("message", **kw)

    async def send_photo(self, **kw):
        await self._record("photo", **kw)

    async def send_document(self, **kw):
        await self._record("document", **kw)

    async def download(self, file_id, destination):
        destination.write(self.files[file_id])
        return destination


@pytest.fixture
def tg(settings):
    s = settings.model_copy(
        update={
            "telegram_control_chat_id": CONTROL,
            "telegram_report_channel_id": CHANNEL,
            "telegram_allowed_user_ids": frozenset({USER}),
        }
    )
    orch, _ = build_orch(s, [FakeConnector("openai", MODEL)])
    bot = FakeBot()
    gw = TelegramGateway(s, orch, orch.attachments, orch.actions, bot=bot)
    orch.notifier = gw
    return gw, bot, orch


def msg(user=USER, chat=CONTROL, **kw):
    base = dict(
        from_user=SimpleNamespace(id=user),
        chat=SimpleNamespace(id=chat),
        message_id=1,
        photo=None,
        document=None,
        video=None,
        audio=None,
        voice=None,
        caption=None,
        media_group_id=None,
        answer=AsyncMock(),
    )
    base.update(kw)
    return SimpleNamespace(**base)


def cmd(args):
    return SimpleNamespace(args=args)


def test_authorization_rules(tg):
    gw, _, _ = tg
    assert gw.authorized(USER, CONTROL)
    assert not gw.authorized(STRANGER, CONTROL)
    assert not gw.authorized(USER, CHANNEL)  # channel/discussion is not a control surface
    assert not gw.authorized(None, CONTROL)


async def test_unauthorized_commands_ignored(tg):
    gw, _, orch = tg
    m = msg(user=STRANGER)
    await gw.on_status(m)
    await gw.on_task(m, cmd("do something"))
    m.answer.assert_not_called()
    assert not orch.tasks


async def test_command_validation(tg):
    gw, _, _ = tg
    m = msg()
    await gw.on_report(m, cmd(None))
    assert "Usage" in m.answer.call_args.args[0]
    m = msg()
    await gw.on_stop(m, cmd("tsk_missing"))
    assert "Unknown" in m.answer.call_args.args[0]
    m = msg()
    await gw.on_task(m, cmd(""))
    assert "Usage" in m.answer.call_args.args[0]


async def test_report_routing_control_vs_channel(tg):
    gw, bot, orch = tg
    task = SimpleNamespace(task_id="t")
    await gw.report(task, "hello")
    await gw.report(
        task, "approve?", control=True, photos=[("a.png", b"x")], files=[("r.txt", b"y")]
    )
    chats = [(kind, kw["chat_id"]) for kind, kw in bot.sent]
    assert chats == [
        ("message", CHANNEL),
        ("message", CONTROL),
        ("photo", CONTROL),
        ("document", CONTROL),
    ]


async def test_parse_error_falls_back_to_plain(tg):
    gw, bot, _ = tg
    bot.fail = [
        TelegramBadRequest(SendMessage(chat_id=1, text=""), "Bad Request: can't parse entities")
    ]
    await gw.report(SimpleNamespace(task_id="t"), "<b>a &amp; b</b>")
    kind, kw = bot.sent[0]
    assert kw["text"] == "a & b" and kw["parse_mode"] is None


async def test_rate_limit_backoff_and_delivery_failure(tg):
    gw, bot, orch = tg
    bot.fail = [TelegramRetryAfter(SendMessage(chat_id=1, text=""), "flood", 0)]
    await gw.report(SimpleNamespace(task_id="t"), "x")
    assert len(bot.sent) == 1
    # Non-retryable failures surface to the orchestrator, which records them.
    bot.fail = [RuntimeError("network down")] * 10
    task = await orch.submit("q")
    await asyncio.sleep(0.1)
    assert task.state == TaskState.succeeded and task.delivery_failures >= 1


async def test_task_draft_callback_flow(tg):
    gw, bot, orch = tg
    bot.files["f1"] = b"\xff\xd8\xff\xe0fakejpeg"  # Telegram photos are JPEG
    m = msg(photo=[SimpleNamespace(file_id="f1", file_size=100)])
    await gw.on_task(m, cmd("describe this"))
    markup = m.answer.call_args.kwargs["reply_markup"]
    token = markup.inline_keyboard[0][0].callback_data  # "Ask"
    assert len(token.encode()) <= 64

    def query(user, data=token):
        return SimpleNamespace(
            data=data,
            from_user=SimpleNamespace(id=user),
            message=SimpleNamespace(
                chat=SimpleNamespace(id=CONTROL), edit_reply_markup=AsyncMock()
            ),
            answer=AsyncMock(),
        )

    q = query(STRANGER)
    await gw.on_callback(q)
    assert q.answer.call_args.kwargs.get("show_alert") and not orch.tasks
    q = query(USER)
    await gw.on_callback(q)
    assert "created" in q.answer.call_args.args[0]
    (task,) = orch.tasks.values()
    assert len(task.attachment_ids) == 1
    q = query(USER)  # replay
    await gw.on_callback(q)
    assert "already used" in q.answer.call_args.args[0]
    assert len(orch.tasks) == 1


async def test_attachment_rejections_are_reported(tg):
    gw, bot, _ = tg
    m = msg(
        document=SimpleNamespace(
            file_id="big", file_size=10**9, file_name="x.bin", mime_type="application/octet-stream"
        )
    )
    await gw.on_task(m, cmd("x"))
    assert "rejected" in m.answer.call_args.args[0]
    bot.files["exe"] = b"MZ\x90\x00"
    m = msg(
        document=SimpleNamespace(
            file_id="exe", file_size=4, file_name="a.exe", mime_type="application/x-msdownload"
        )
    )
    await gw.on_task(m, cmd("x"))
    assert "not permitted" in m.answer.call_args.args[0]
    m = msg(voice=SimpleNamespace(file_id="v"))
    await gw.on_task(m, cmd("x"))
    assert "not supported" in m.answer.call_args.args[0]


async def test_stray_media_and_album_without_command(tg, monkeypatch):
    gw, _, _ = tg
    m = msg(photo=[SimpleNamespace(file_id="f", file_size=1)])
    await gw.on_stray_media(m)
    assert "not used" in m.answer.call_args.args[0]
    monkeypatch.setattr(telegram_bot, "ALBUM_WAIT_S", 0)
    parts = [
        msg(media_group_id="g", photo=[SimpleNamespace(file_id="f", file_size=1)]) for _ in range(2)
    ]
    for p in parts:
        await gw.on_album_part(p)
    await asyncio.sleep(0.05)
    assert "Album of 2 items not used" in parts[0].answer.call_args.args[0]


# ---------- REST API ----------


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "test-token-123")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "var"))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)  # avoid picking up a real .env
    get_settings.cache_clear()
    from app.main import app

    with TestClient(app) as client:
        yield client
    get_settings.cache_clear()


H = {"Authorization": "Bearer test-token-123"}


def test_api_auth(api):
    assert api.get("/health").json() == {"status": "ok"}
    assert api.get("/api/v1/agents").status_code == 401
    assert api.get("/api/v1/agents", headers={"Authorization": "Bearer wrong"}).status_code == 401
    agents = api.get("/api/v1/agents", headers=H).json()
    assert {c["name"] for c in agents["connectors"]} == {"claude_code", "openai", "deepseek"}
    assert not any(c["available"] for c in agents["connectors"])


def test_api_attachments_and_tasks(api):
    r = api.post(
        "/api/v1/attachments", headers=H, files={"file": ("a.png", make_png(2, 2), "image/png")}
    )
    assert r.status_code == 201 and "storage_path" not in r.json()
    att_id = r.json()["attachment_id"]
    r = api.post("/api/v1/attachments", headers=H, files={"file": ("a.png", b"fake", "image/png")})
    assert r.status_code == 400
    # No provider configured: accepted, then fails with an explicit unsupported error.
    r = api.post("/api/v1/tasks", headers=H, json={"description": "hi", "attachment_ids": [att_id]})
    assert r.status_code == 201
    task_id = r.json()["task_id"]
    body = api.get(f"/api/v1/tasks/{task_id}", headers=H).json()["task"]
    assert body["state"] == "failed" and body["error_category"] == "unsupported"
    arts = api.get(f"/api/v1/tasks/{task_id}/artifacts", headers=H).json()
    assert [a["kind"] for a in arts] == ["envelope"]  # result envelope only; nothing executed
    r = api.post("/api/v1/tasks", headers=H, json={"description": "x", "workflow": "code"})
    assert r.status_code == 422 and "WORKSPACE_ROOT" in r.json()["detail"]
    assert api.post("/api/v1/actions/forged/execute", headers=H).status_code == 403  # agents' token
    r = api.post("/api/v1/messages", headers=H, json={"content": "hello"})
    assert r.status_code == 201 and r.json()["task_id"] is None


def test_api_disabled_without_token(tmp_path, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "var"))
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    from app.main import app

    with TestClient(app) as client:
        assert client.get("/api/v1/agents", headers=H).status_code == 503
    get_settings.cache_clear()


def test_config_validation():
    with pytest.raises(ValueError, match="missing"):
        Settings(_env_file=None, telegram_bot_token="1:x")
    with pytest.raises(ValueError, match="ALLOWED"):
        Settings(
            _env_file=None,
            telegram_bot_token="1:x",
            telegram_control_chat_id=1,
            telegram_report_channel_id=2,
        )
    s = Settings(_env_file=None, telegram_allowed_user_ids="1, 2")
    assert s.telegram_allowed_user_ids == {1, 2}


def test_env_example_loads_with_blank_values(tmp_path):
    from pathlib import Path

    env = tmp_path / ".env"
    env.write_text(Path(".env.example").read_text())
    s = Settings(_env_file=env)
    # Blank WORKSPACE_ROOT must not silently become the current directory.
    assert s.workspace_root is None and s.claude_code_mcp_config is None
    assert s.telegram_bot_token is None and s.qa_commands == {}


async def test_generic_telegram_commands(tg):
    gw, bot, orch = tg
    m = msg()
    await gw.on_tool(m, cmd('workspace.list_files {"pattern": "*.py"}'))
    assert "Tool task" in m.answer.call_args.args[0]
    task = next(iter(orch.tasks.values()))
    await asyncio.sleep(0.2)
    assert task.state == TaskState.succeeded and task.request["tool"] == "workspace.list_files"
    m = msg()
    await gw.on_tool(m, cmd("workspace.list_files [1]"))
    assert "JSON object" in m.answer.call_args.args[0]
    m = msg()
    await gw.on_plugins(m)
    assert (
        "<b>data</b>" in m.answer.call_args.args[0] and "<b>unity</b>" in m.answer.call_args.args[0]
    )
    m = msg()
    await gw.on_steps(m, cmd(task.task_id))
    assert "<code>run</code> succeeded" in m.answer.call_args.kwargs["text"]
    m = msg()
    await gw.on_answer(m, cmd(f"{task.task_id} yes"))
    assert "not waiting" in m.answer.call_args.args[0]
    m = msg(user=STRANGER)
    await gw.on_tool(m, cmd("workspace.list_files {}"))
    m.answer.assert_not_called()


async def test_auto_draft_uses_generic_intake(tg):
    gw, bot, orch = tg
    m = msg()
    await gw.on_task(m, cmd("profile the dataset"))
    labels = [
        b.text for row in m.answer.call_args.kwargs["reply_markup"].inline_keyboard for b in row
    ]
    assert labels[0] == "🤖 Auto" and "📊 Data" in labels and "🛠 Code" in labels
