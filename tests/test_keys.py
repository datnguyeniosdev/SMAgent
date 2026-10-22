"""/keys: provider keys set from Telegram (private chat only), kept encrypted in the database,
providers reloaded live."""

import os
from types import SimpleNamespace

import pytest
from aiogram.dispatcher.event.bases import SkipHandler

from app import keys
from app.connectors.fal import FalImageProvider
from tests.test_config_gateway import CONTROL, STRANGER, USER, gateway, make
from tests.test_telegram_panels import buttons, press, tg


def test_saved_encrypted_and_loaded_again(tmp_path, monkeypatch):
    from app.store import Store

    db = tmp_path / "state.db"
    monkeypatch.setenv(keys.SECRET_ENV, keys.new_secret())
    monkeypatch.delenv("FAL_KEY", raising=False)
    store = Store(db)
    keys.save(store, "FAL_KEY", "fal-secret-9876")
    assert os.environ["FAL_KEY"] == "fal-secret-9876" and keys.saved(store) == {"FAL_KEY"}
    store.close()
    assert b"fal-secret-9876" not in db.read_bytes()  # only ciphertext in the database
    monkeypatch.delenv("FAL_KEY")
    assert (
        keys.load(Store(db)) == [] and os.environ["FAL_KEY"] == "fal-secret-9876"
    )  # after a restart
    monkeypatch.setenv(keys.SECRET_ENV, keys.new_secret())  # a different secret cannot read it
    monkeypatch.delenv("FAL_KEY")
    assert keys.load(Store(db)) == ["FAL_KEY"] and "FAL_KEY" not in os.environ
    store = Store(db)
    keys.clear(store, "FAL_KEY", "from-dotenv")
    assert keys.saved(store) == set() and os.environ["FAL_KEY"] == "from-dotenv"
    monkeypatch.delenv(keys.SECRET_ENV)
    with pytest.raises(ValueError, match="AGW_SECRET_KEY is not set"):
        keys.save(store, "FAL_KEY", "x-123456789")
    assert (
        keys.mask("sk-1234567890abcd", True) == "set (…abcd)" and keys.mask("m-1", False) == "m-1"
    )


def dm(answers, deleted, text, user=USER):
    async def answer(t, reply_markup=None):
        answers.append((t, reply_markup))

    async def delete():
        deleted.append(text)

    return SimpleNamespace(text=text, from_user=SimpleNamespace(id=user), answer=answer, delete=delete,
                           chat=SimpleNamespace(id=user, type="private"))  # fmt: skip


async def test_key_from_private_chat_reloads_fal(settings, cfg_dir, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGW_ENV", "prod")
    monkeypatch.delenv("FAL_KEY", raising=False)
    monkeypatch.setenv(keys.SECRET_ENV, keys.new_secret())
    orch, _ = make(settings, cfg_dir, [])
    gw = gateway(orch)

    async def get_me():
        return SimpleNamespace(username="agw_bot")

    gw.bot.get_me = get_me
    answers, alerts, deleted = [], [], []
    await gw.on_keys(tg(answers))
    text, kb = answers[-1]
    assert "<b>FAL_KEY</b> · missing" in text and "x:set:FAL_KEY" in [d for _, d in buttons(kb)]
    assert orch._image_provider is None

    with pytest.raises(SkipHandler):  # nothing pending: private text goes to the other handlers
        await gw.on_key_value(dm(answers, deleted, "hello"))
    await gw.on_keys_button(press("x:set:FAL_KEY", answers, alerts))
    assert "https://t.me/agw_bot" in answers[-1][0]
    with pytest.raises(SkipHandler):  # someone else's private chat: not taken
        await gw.on_key_value(dm(answers, deleted, "fal-stranger-1234", user=STRANGER))
    await gw.on_key_value(dm(answers, deleted, "bad value with spaces"))
    assert "not saved" in answers[-1][0] and deleted == ["bad value with spaces"]
    await gw.on_keys_button(press("x:set:FAL_KEY", answers, alerts))
    await gw.on_key_value(dm(answers, deleted, "fal-secret-9876"))
    reply = answers[-1][0]
    assert "FAL_KEY</b> saved: set (…9876)" in reply and "fal-secret-9876" not in reply
    assert (
        deleted[-1] == "fal-secret-9876" and not (tmp_path / ".env").exists()
    )  # database, not .env
    assert "<b>FAL_KEY</b> · set (…9876) · saved" in (await panel(gw, answers))
    assert isinstance(orch._image_provider, FalImageProvider)  # live, no restart

    await gw.on_keys_button(press("x:clear:FAL_KEY", answers, alerts))  # asks first
    assert [d for _, d in buttons(answers[-1][1])][0] == "x:clearyes:FAL_KEY"
    await gw.on_keys_button(press("x:clearyes:FAL_KEY", answers, alerts))
    assert orch._image_provider is None and "FAL_KEY" not in os.environ
    await gw.on_keys_button(press("x:set:PATH", answers, alerts))
    assert alerts[-1] == "Unknown variable."
    await gw.on_keys_button(press("x:set:FAL_KEY", answers, alerts, user=STRANGER))
    assert alerts[-1] == "Not authorized." and CONTROL


async def panel(gw, answers):
    await gw.on_keys(tg(answers))
    return answers[-1][0]
