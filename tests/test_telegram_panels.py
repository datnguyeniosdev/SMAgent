"""/menu and /setup panels."""

from types import SimpleNamespace

import pytest

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
        "m:list", "m:agents", "m:queue", "m:open", "m:keys", "m:providers", "m:plan", "m:deploy", "m:setup", "m:mcp", "m:templates", "m:auto", "m:help"]  # fmt: skip
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


async def test_tool_switches_apply_without_restart(settings, cfg_dir):
    from mcp import Client

    from app.mcp_server import TOOL_NAMES, build_mcp_server

    orch, _ = make(settings, cfg_dir, [])
    orch.image_provider = SimpleNamespace(name="fal", model="m", available=True)
    gw = gateway(orch)
    answers, alerts = [], []
    await gw.on_mcp(tg(answers))
    text, kb = answers[-1]
    data = [d for _, d in buttons(kb)]
    assert "All on." in text and data[: len(TOOL_NAMES)] == [f"t:{t}" for t in TOOL_NAMES]
    assert data[len(TOOL_NAMES) :] == ["t:art:images"]  # each art provider has its own switch
    async with Client(build_mcp_server(orch)) as c:
        assert not (await c.call_tool("list_agents", {})).is_error
        await gw.on_mcp_button(press("t:list_agents", answers, alerts))  # off, same server
        assert "list_agents is now off" in answers[-1][0] and "Off: list_agents" in answers[-1][0]
        r = await c.call_tool("list_agents", {})
        assert r.is_error and "switched off by the operator" in r.content[0].text
        await gw.on_mcp_button(press("t:list_agents", answers, alerts))  # back on
        assert not (await c.call_tool("list_agents", {})).is_error
    await gw.on_mcp_button(press("t:generate_art", answers, alerts))  # art off: no image provider
    assert orch.image_provider is None
    await gw.on_mcp_button(press("t:generate_art", answers, alerts))
    assert orch.image_provider.name == "fal"
    await gw.on_mcp_button(press("t:list_agents", answers, alerts, user=STRANGER))
    assert alerts[-1] == "Not authorized." and orch.switches.on("list_agents")


def test_config_enabled_is_only_the_starting_state(settings, cfg_dir):
    from app.store import Store
    from app.switches import Switches

    store = Store(":memory:")
    sw = Switches(store, {"generate_art": False})
    assert not sw.on("generate_art") and sw.on("ask_agent")
    sw.set("generate_art", True)
    assert Switches(store, {"generate_art": False}).on("generate_art")  # the saved choice wins


async def test_agents_panel_groups_and_controls(settings, cfg_dir, monkeypatch):
    from app.connectors.base import AgentResult, UnsupportedCapability
    from app.repo_setup import client_kind
    from tests.test_config_gateway import VISION, Provider

    orch, _ = make(
        settings, cfg_dir, [Provider("openai", VISION, lambda r: AgentResult("ok", "openai", "m"))]
    )
    hub, gw = orch.channel, gateway(orch)
    hub.pull("Proj A", "pull_desk", "/w/a", agent="claude_code", client="app")
    hub.pull("Proj B", "pull_term", "/w/b", agent="codex")  # no client: known CLI
    hub.pull("Proj C", "pull_cur", "/w/c", agent="cursor")  # no client: known app
    answers, alerts = [], []
    await gw.on_agents(tg(answers))
    text, kb = answers[-1]
    app_part, cli_part = text.split("⌨️ <b>CLI</b>")[0], text.split("⌨️ <b>CLI</b>")[1].split("☁️")[0]
    assert "ClaudeCode(App)" in app_part and "Cursor(App)" in app_part and "Codex(CLI)" in cli_part
    assert "🟢 <b>openai</b> · on" in text and "y:sw:api:openai" in [d for _, d in buttons(kb)]

    await gw.on_agents_button(
        press("y:sw:api:openai", answers, alerts)
    )  # API agent off: no routing
    assert "⏸ <b>openai</b> · off" in answers[-1][0] and not orch.router.describe()[0]["on"]
    with pytest.raises(UnsupportedCapability, match="switched off"):
        orch.router.route(VISION, explicit="openai")
    with pytest.raises(UnsupportedCapability):
        orch.router.route(VISION)
    await gw.on_agents_button(press("y:sw:api:openai", answers, alerts))
    assert orch.router.route(VISION).name == "openai"

    s = hub.find("Proj B")
    await gw.on_agents_button(press(f"y:off:s{s.num}", answers, alerts))
    assert not s.enabled
    await gw.on_agents_button(press(f"y:close:s{s.num}", answers, alerts))  # asks first
    assert s in hub.live() and [d for _, d in buttons(answers[-1][1])][0] == f"y:closeyes:s{s.num}"
    await gw.on_agents_button(press(f"y:closeyes:s{s.num}", answers, alerts))
    assert s not in hub.live() and "closed" in answers[-1][0]
    await gw.on_agents_button(press("y:sw:api:openai", answers, alerts, user=STRANGER))
    assert alerts[-1] == "Not authorized." and orch.router.on("openai")

    for entry, kind in (
        ("claude-desktop", "app"),
        ("claude-vscode", "app"),
        ("cli", "cli"),
        ("sdk-py", "cli"),
    ):
        monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", entry)
        assert client_kind() == kind
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT")
    assert client_kind() == ""
