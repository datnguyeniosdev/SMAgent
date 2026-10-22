"""Shared providers (presets added from Telegram), several models per provider, per-project plans."""

from types import SimpleNamespace

import pytest
from mcp import Client

from app import catalog
from app.connectors.base import AgentResult
from app.mcp_server import build_mcp_server
from tests.conftest import FakeConnector, wait_done
from tests.test_config_gateway import (
    STRANGER,
    VISION,
    gateway,
    make,
)
from tests.test_telegram_panels import buttons, press, tg

PROJECT = "Neon Ball Shooter"


@pytest.fixture
def env(monkeypatch):
    for k in ("GEMINI_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.setenv(k, "test-key-not-real")


class Rec(FakeConnector):
    """Records which provider and model really ran (a model-specific executor runs on a copy)."""

    used: list = []

    async def execute(self, req):
        Rec.used.append((self.name, self.model))
        return AgentResult(f"{self.name}:{self.model}", self.name, self.model)


async def ask(orch, objective="what is the answer?"):
    async with Client(build_mcp_server(orch)) as c:
        r = await c.call_tool("delegate_task", {"objective": objective, "project": PROJECT})
    task = orch.tasks[r.structured_content["task_id"]]
    await wait_done(orch, task)
    return task


async def test_plan_picks_project_models_then_falls_back(settings, cfg_dir, env):
    Rec.used = []
    orch, _ = make(
        settings,
        cfg_dir,
        [Rec("openai", VISION, model="gpt-default"), Rec("gemini", VISION, model="g-default")],
    )
    await ask(orch)
    assert Rec.used[-1] == ("openai", "gpt-default")  # no plan: config order (openai first)

    catalog.add_model(orch.store, "gemini", "g-pro")
    catalog.set_role(orch.store, PROJECT, "assistant", ["provider:gemini@g-pro", "provider:openai"])
    await ask(orch)
    assert Rec.used[-1] == ("gemini", "g-pro")  # the project's first choice, on its own model
    assert (
        orch.router.connectors["gemini"].model == "g-default"
    )  # the shared connector is untouched

    orch.switches.set("api:gemini", False)  # /agents: gemini off -> next in the plan
    await ask(orch)
    assert Rec.used[-1] == ("openai", "gpt-default")
    other = await ask_other_project(orch)
    assert other == ("openai", "gpt-default")  # other projects keep the defaults


async def ask_other_project(orch):
    async with Client(build_mcp_server(orch)) as c:
        r = await c.call_tool("delegate_task", {"objective": "hi", "project": "Other"})
    await wait_done(orch, orch.tasks[r.structured_content["task_id"]])
    return Rec.used[-1]


def test_presets_added_merged_and_removed(settings, cfg_dir):
    from app.gateway_config import load_config
    from app.store import Store

    cfg, store = load_config(cfg_dir), Store(":memory:")
    d = catalog.add_provider(store, cfg, "openrouter")
    assert d.api_key_env == "OPENROUTER_API_KEY" and cfg.providers[
        "openrouter"
    ].base_url.startswith("https://openrouter.ai")
    with pytest.raises(ValueError, match="already"):
        catalog.add_provider(store, cfg, "openrouter")
    fresh = load_config(cfg_dir)
    catalog.merge(store, fresh)  # after a restart
    assert "openrouter" in fresh.providers
    with pytest.raises(ValueError, match="models.yaml"):
        catalog.remove_provider(store, cfg, "openai")
    catalog.remove_provider(store, cfg, "openrouter")
    again = load_config(cfg_dir)
    catalog.merge(store, again)
    assert "openrouter" not in again.providers and "openrouter" not in cfg.providers


async def test_telegram_providers_and_plan(settings, cfg_dir, env, monkeypatch):
    orch, _ = make(
        settings, cfg_dir, [FakeConnector("openai", VISION), FakeConnector("gemini", VISION)]
    )
    gw, hub = gateway(orch), orch.channel
    hub.project_num(PROJECT)
    answers, alerts = [], []

    async def no_list(d, key, client):
        return ["qwen/qwen3-coder", "meta/llama"]

    monkeypatch.setattr(catalog, "remote_models", no_list)
    await gw.on_providers(tg(answers))
    assert "<b>openai</b>" in answers[-1][0] and "v:add" in [d for _, d in buttons(answers[-1][1])]
    await gw.on_providers_button(press("v:add", answers, alerts))
    assert "v:new:openrouter" in [d for _, d in buttons(answers[-1][1])]
    await gw.on_providers_button(press("v:new:openrouter", answers, alerts))
    assert "added" in answers[-1][0] and "openrouter" in orch.gateway_cfg.providers

    msg = tg(answers)
    await gw.on_addmodel(msg, SimpleNamespace(args="openrouter qwen/qwen3-coder"))
    assert "qwen/qwen3-coder added" in answers[-1][0] and "⚠️" not in answers[-1][0]
    await gw.on_addmodel(msg, SimpleNamespace(args="openrouter typo-model"))
    assert "not in openrouter's model list" in answers[-1][0]
    await gw.on_findmodel(msg, SimpleNamespace(args="openrouter qwen"))
    assert (
        "/addmodel openrouter qwen/qwen3-coder" in answers[-1][0] and "llama" not in answers[-1][0]
    )

    num = hub.project_num(PROJECT)
    await gw.on_plan(msg, SimpleNamespace(args=f"p{num}"))
    assert "<b>assistant</b>: defaults" in answers[-1][0]
    await gw.on_plan_button(press(f"pl:{num}:assistant:add", answers, alerts))
    choices = {t: d for t, d in buttons(answers[-1][1])}
    await gw.on_plan_button(press(choices["openrouter · qwen/qwen3-coder"], answers, alerts))
    await gw.on_plan_button(press(f"pl:{num}:assistant:add", answers, alerts))
    await gw.on_plan_button(
        press({t: d for t, d in buttons(answers[-1][1])}["gemini (default model)"], answers, alerts)
    )
    assert catalog.plan(orch.store, PROJECT)["assistant"] == [
        "provider:openrouter@qwen/qwen3-coder",
        "provider:gemini",
    ]
    await gw.on_plan_button(press(f"pl:{num}:assistant:^:1", answers, alerts))  # gemini first
    assert catalog.plan(orch.store, PROJECT)["assistant"][0] == "provider:gemini"
    await gw.on_plan_button(press(f"pl:{num}:assistant:-:0", answers, alerts))
    assert catalog.plan(orch.store, PROJECT)["assistant"] == [
        "provider:openrouter@qwen/qwen3-coder"
    ]
    await gw.on_plan_button(press(f"pl:{num}:assistant:-:0", answers, alerts, user=STRANGER))
    assert alerts[-1] == "Not authorized." and catalog.plan(orch.store, PROJECT)["assistant"]
