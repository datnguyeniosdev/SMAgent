"""/menu and /setup panels."""

from types import SimpleNamespace

from app import agents_setup, doctor, global_setup
from tests.test_config_gateway import CONTROL, STRANGER, USER, gateway, make


def tg(answers, user=USER):
    async def answer(text, reply_markup=None):
        answers.append((text, reply_markup))

    return SimpleNamespace(chat=SimpleNamespace(id=CONTROL), from_user=SimpleNamespace(id=user, full_name="D"),
                           answer=answer)  # fmt: skip


def press(data, answers, alerts, user=USER):
    async def q_answer(text=None, show_alert=False):
        alerts.append(text)

    return SimpleNamespace(
        data=data, from_user=SimpleNamespace(id=user), answer=q_answer, message=tg(answers)
    )


def buttons(kb):
    return [(b.text, b.callback_data) for row in kb.inline_keyboard for b in row]


async def test_menu_panel_and_buttons(settings, cfg_dir):
    orch, _ = make(settings, cfg_dir, [])
    gw = gateway(orch)
    answers, alerts = [], []
    await gw.on_menu(tg(answers))
    text, kb = answers[-1]
    assert "0 sessions" in text and [d for _, d in buttons(kb)] == [
        "m:list", "m:queue", "m:open", "m:setup", "m:templates", "m:help"]  # fmt: skip
    await gw.on_menu_button(press("m:list", answers, alerts))
    assert answers[-1][0].startswith("🖥 <b>SESSIONS</b>")
    await gw.on_menu_button(press("m:queue", answers, alerts))
    assert answers[-1][0].startswith("📥 <b>QUEUE</b>")
    await gw.on_menu_button(press("m:list", answers, alerts, user=STRANGER))
    assert alerts[-1] == "Not authorized."


async def test_setup_panel_install_and_confirmed_remove(settings, cfg_dir, monkeypatch):
    orch, _ = make(settings, cfg_dir, [])
    gw = gateway(orch)
    monkeypatch.setattr(doctor, "collect", lambda s, check_gateway=True: [
        doctor.Check(True, "gateway", "ok"), doctor.Check(False, "global setup", "not installed")])  # fmt: skip
    calls = []
    monkeypatch.setattr(agents_setup, "detected", lambda: ["codex"])
    monkeypatch.setattr(agents_setup, "configured", lambda a: False)
    monkeypatch.setattr(agents_setup, "install", lambda a: calls.append(f"agent {a}") or f"{a}: ok")
    monkeypatch.setattr(global_setup, "install", lambda *a: calls.append("install") or ["skill: x"])
    monkeypatch.setattr(
        global_setup, "remove", lambda *a: calls.append("remove") or ["skill removed"]
    )
    answers, alerts = [], []

    await gw.on_setup(tg(answers))
    text, kb = answers[-1]
    assert "✅ <b>gateway</b>" in text and "⚠️ <b>global setup</b>: not installed" in text
    assert [d for _, d in buttons(kb)] == ["g:refresh", "g:install", "g:agenton:codex", "g:remove"]
    await gw.on_setup_button(press("g:agenton:codex", answers, alerts))
    assert calls.pop() == "agent codex"

    await gw.on_setup_button(press("g:install", answers, alerts))
    assert calls == ["install"] and "Installed / updated" in answers[-1][0]
    await gw.on_setup_button(press("g:remove", answers, alerts))  # asks first
    assert calls == ["install"] and "Remove the global setup" in answers[-1][0]
    assert [d for _, d in buttons(answers[-1][1])] == ["g:remove_yes", "g:refresh"]
    await gw.on_setup_button(press("g:remove_yes", answers, alerts))
    assert calls == ["install", "remove"] and "Removed" in answers[-1][0]
    await gw.on_setup_button(press("g:install", answers, alerts, user=STRANGER))
    assert alerts[-1] == "Not authorized." and calls == ["install", "remove"]
