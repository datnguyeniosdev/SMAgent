"""Central Gateway Phase 1: stable project ids (aliases, routing mode), message envelope fields
(project_id, correlation_id, idempotency), sessions tagged with project/agent ids and health."""

import pytest

from app.project_registry import ProjectRegistry
from tests.test_config_gateway import make

A, B = "2048 Shooter (K05)", "PocketSort (ttg-002-coin_sort)"


def test_projects_get_stable_ids_and_keep_old_names_as_aliases(settings, cfg_dir):
    orch, _ = make(settings, cfg_dir, [])
    reg = ProjectRegistry(orch.store)
    a, b = reg.ensure(A), reg.ensure(B)
    assert (
        a.project_id != b.project_id and reg.ensure("2048 shooter k05").project_id == a.project_id
    )
    assert a.routing_mode == "main"  # today's behaviour until a project is moved

    renamed = reg.rename(A, "Toss Mallow (K05)")
    assert renamed.project_id == a.project_id and A in renamed.aliases
    assert reg.resolve(A).name == "Toss Mallow (K05)"  # the old name still finds it
    assert reg.resolve(a.project_id).name == "Toss Mallow (K05)"
    with pytest.raises(ValueError):
        reg.rename(B, "Toss Mallow (K05)")  # a name belongs to one project

    assert reg.set_routing_mode(b.project_id, "central").routing_mode == "central"
    assert reg.get(b.project_id).routing_mode == "central"


def test_messages_carry_their_project_id_and_correlation_and_are_sent_once(settings, cfg_dir):
    orch, _ = make(settings, cfg_dir, [])
    hub = orch.channel
    a, _, _ = hub.pull(A, None, "/w/a")
    b, _, _ = hub.pull(B, None, "/w/b")
    m1, t1 = hub.send(A, "build level 2", extra={"idempotency_key": "tg:42"})
    m2, t2 = hub.send(A, "build level 2", extra={"idempotency_key": "tg:42"})  # a retried call
    assert m2["message_id"] == m1["message_id"] and t1 is t2 is a
    other, _ = hub.send(B, "sort coins")
    assert m1["project_id"] != other["project_id"]  # no cross-project mixing
    assert m1["project_id"] == hub.registry.resolve(A).project_id
    assert m1["correlation_id"].startswith("cor_")
    linked, _ = hub.send(A, "follow-up", extra={"correlation_id": m1["correlation_id"]})
    assert linked["correlation_id"] == m1["correlation_id"]

    hub.cancel(m1["message_id"])
    again, _ = hub.send(A, "build level 2", extra={"idempotency_key": "tg:42"})
    assert again["message_id"] != m1["message_id"]  # a cancelled one is not the same job any more


def test_sessions_are_recorded_with_project_agent_and_health(settings, cfg_dir):
    orch, _ = make(settings, cfg_dir, [])
    hub = orch.channel
    s, _, _ = hub.pull(A, None, "/w/a", 0, "codex", "cli")
    rec = orch.store.get("channel_session", s.session_id)
    assert rec["project_id"] == hub.registry.resolve(A).project_id
    assert (rec["agent_id"], rec["health"]) == ("codex", "ok")
    s.enabled = False
    hub._save(s)
    assert orch.store.get("channel_session", s.session_id)["health"] == "off"


def test_a_renamed_project_is_still_found_by_its_old_name(settings, cfg_dir):
    orch, _ = make(settings, cfg_dir, [])
    hub = orch.channel
    hub.pull("Old Name", None, "/w/x")
    hub.registry.rename("Old Name", "New Name")
    hub.pull("New Name", None, "/w/x2")
    assert hub.resolve_project("Old Name") == "New Name"


def central_setup(settings, cfg_dir, tmp_path, monkeypatch, rules=""):
    from app import channel as ch
    from app import projects

    game = tmp_path / "game"
    game.mkdir()
    (game / "README.md").write_text(f"# {A}\n")
    (game / "plan.md").write_text("v1\n")
    gate = tmp_path / "gate"
    gate.mkdir()
    monkeypatch.setattr(projects, "projects_root", lambda: tmp_path)
    monkeypatch.setattr(ch, "CLAUDE_JSON", tmp_path / "claude.json")
    from app.gateway_config import ChannelRule

    orch, _ = make(settings, cfg_dir, [])
    orch.gateway_cfg.routing.channel_rules = [ChannelRule(**r) for r in rules or []]
    hub, opened = orch.channel, []
    hub.launch = lambda path, agent, model="": opened.append(path)
    gw, _ = hub.claim_main("Agent Gateway", "claude_code", str(gate), "app", "local_gate")
    own, _, _ = hub.pull(A, None, str(game), 0, "claude_code", "app")  # the project's own main
    hub.registry.set_routing_mode(A, "central")
    return orch, hub, gw, own, game, opened


async def test_a_central_project_request_goes_to_the_gateway_agent_not_its_own_main(
    settings, cfg_dir, tmp_path, monkeypatch
):
    from pathlib import Path

    orch, hub, gw, own, game, opened = central_setup(settings, cfg_dir, tmp_path, monkeypatch)
    m, target = hub.send(A, "make the shop screen calmer")
    assert target is gw and own is not gw
    _, got, _ = hub.pull("Agent Gateway", gw.session_id)
    assert "You are the Gateway Agent" in got[0]["text"] and A in got[0]["text"]
    assert hub.in_progress(own.session_id) == []  # the project's own main is not involved

    with pytest.raises(PermissionError, match="only the Gateway Agent"):
        await hub.dispatch(own.session_id, m["message_id"], "ui")
    r = await hub.dispatch(
        gw.session_id, m["message_id"], "UI", brief="calmer palette, report a shot"
    )
    path = Path(r["path"])
    assert (
        r["new_session"] and path.parent == game / ".agw-children"
    )  # the project's copy, not the gateway's
    assert (path / "plan.md").read_text() == "v1\n" and opened == [path]
    kid, _, _ = hub.pull(A, None, str(path), 0, "claude_code", "cli")
    _, got, _ = hub.pull(A, kid.session_id)
    assert got[0]["message_id"] == m["message_id"] and "calmer palette" in got[0]["text"]
    assert hub.in_progress(gw.session_id) == []  # the Gateway Agent is free again


async def test_a_routing_rule_places_work_without_any_llm_and_reuses_the_lane(
    settings, cfg_dir, tmp_path, monkeypatch
):
    import asyncio
    from pathlib import Path

    rules = [{"project": "*", "match": r"(?i)^\[qa\]", "lane": "qa"}]
    orch, hub, gw, own, game, opened = central_setup(
        settings, cfg_dir, tmp_path, monkeypatch, rules
    )
    m, target = hub.send(A, "[qa] retest level 3")
    assert target is None  # the gateway opens the qa lane itself
    for _ in range(50):
        if opened:
            break
        await asyncio.sleep(0.02)
    path = opened[0]
    assert Path(path).name.startswith("qa-")
    hub._flush_pending(A)  # flushed again while opening: not opened twice
    await asyncio.sleep(0.05)
    assert len(opened) == 1
    kid, _, _ = hub.pull(A, None, str(path), 0, "claude_code", "cli")
    _, got, _ = hub.pull(A, kid.session_id)
    assert got[0]["message_id"] == m["message_id"]
    assert hub.in_progress(gw.session_id) == [] and hub.in_progress(own.session_id) == []
    await hub.reply(kid.session_id, "Dạ xong", m["message_id"])

    m2, target = hub.send(A, "[qa] and level 4")  # the open qa child takes the next one
    assert target is kid and len(opened) == 1


async def test_main_mode_projects_still_route_through_their_own_main(
    settings, cfg_dir, tmp_path, monkeypatch
):
    orch, hub, gw, own, game, opened = central_setup(settings, cfg_dir, tmp_path, monkeypatch)
    hub.registry.set_routing_mode(A, "main")
    m, target = hub.send(A, "anything")
    assert target is own and "main session of" in m["text"]


async def test_the_operator_moves_a_project_between_main_and_central_routing(settings, cfg_dir):
    from types import SimpleNamespace

    from tests.test_config_gateway import CONTROL, USER, gateway

    orch, _ = make(settings, cfg_dir, [])
    gw = gateway(orch)
    orch.channel.pull(A, None, "/w/a")
    answers = []

    async def answer(t, **k):
        answers.append(t)

    def cmd(args):
        m = SimpleNamespace(chat=SimpleNamespace(id=CONTROL), from_user=SimpleNamespace(id=USER),
                            answer=answer)  # fmt: skip
        return m, SimpleNamespace(args=args, command="route")

    await gw.on_route(*cmd("p1 central"))
    assert "the gateway routes it" in answers[-1]
    assert orch.channel.registry.resolve(A).routing_mode == "central"
    await gw.on_route(*cmd(""))
    assert "Gateway Agent: offline" in answers[-1] and "<b>central · product</b>" in answers[-1]
    await gw.on_route(*cmd("p1 main"))
    assert orch.channel.registry.resolve(A).routing_mode == "main"


def no_router_setup(settings, cfg_dir, tmp_path, monkeypatch, kind):
    """A central project with no Gateway Agent online: the gateway places everything itself."""
    from app import channel as ch
    from app import projects

    game = tmp_path / "game"
    game.mkdir()
    (game / "README.md").write_text(f"# {A}\n")
    monkeypatch.setattr(projects, "projects_root", lambda: tmp_path)
    monkeypatch.setattr(ch, "CLAUDE_JSON", tmp_path / "claude.json")
    orch, _ = make(settings, cfg_dir, [])
    hub, opened = orch.channel, []
    hub.launch = lambda path, agent, model="": opened.append(path)
    hub.registry.note_root(A, str(game))
    hub.registry.set_routing_mode(A, "central")
    hub.registry.set_kind(A, kind)
    return orch, hub, game, opened


async def settle(opened, n):
    import asyncio

    for _ in range(100):
        if len(opened) >= n:
            return
        await asyncio.sleep(0.02)


async def test_a_tool_project_runs_up_to_three_sessions_in_parallel_then_queues(
    settings, cfg_dir, tmp_path, monkeypatch
):
    orch, hub, game, opened = no_router_setup(settings, cfg_dir, tmp_path, monkeypatch, "tool")
    kids = []
    for i in range(3):  # each busy with its request: the next one gets a new session
        m, _ = hub.send(A, f"review screen {i}")
        await settle(opened, i + 1)
        kid, _, _ = hub.pull(A, None, str(opened[i]), 0, "claude_code", "cli")
        _, got, _ = hub.pull(A, kid.session_id)
        assert got[0]["message_id"] == m["message_id"]
        kids.append(kid)
    assert len(opened) == 3 and all(p.name.startswith("task-") for p in opened)
    m4, target = hub.send(A, "review screen 4")  # at the limit: queued behind a busy one
    assert target in kids and len(opened) == 3
    await hub.reply(
        kids[1].session_id, "Dạ xong", hub.in_progress(kids[1].session_id)[0]["message_id"]
    )
    m5, target = hub.send(A, "review screen 5")
    assert target is kids[1]  # an idle session first


async def test_a_product_works_in_order_in_dev_and_deploys_from_fresh_copies(
    settings, cfg_dir, tmp_path, monkeypatch
):
    orch, hub, game, opened = no_router_setup(settings, cfg_dir, tmp_path, monkeypatch, "product")
    sent = []
    hub._notify = lambda name, *a, **k: sent.append(name)
    m1, _ = hub.send(A, "fix the coin bug")
    await settle(opened, 1)
    assert opened[0].name.startswith("dev-") and sent.count("send_main_offline") == 1
    dev, _, _ = hub.pull(A, None, str(opened[0]), 0, "claude_code", "cli")
    hub.pull(A, dev.session_id)
    m2, target = hub.send(A, "then tune the jump")
    assert target is dev and len(opened) == 1  # one worker on the code, in order

    d1, _ = hub.send(A, "deploy the build to UAT")
    await settle(opened, 2)
    d2, _ = hub.send(A, "release again after the fix")
    await settle(opened, 3)
    assert [p.name.split("-")[0] for p in opened] == ["dev", "deploy", "deploy"]  # never reused
    first = orch.store.get("channel_msg", d1["message_id"])
    assert "fresh copy" in first["text"] and "report the result yourself" in first["text"]
    m3, target = hub.send(A, "qa: retest level 3")
    await settle(opened, 4)
    assert opened[3].name.startswith("qa-")
    assert sent.count("send_main_offline") == 1  # told once; nothing waited for the router


async def test_a_reply_for_a_gone_lane_session_reopens_its_own_folder(
    settings, cfg_dir, tmp_path, monkeypatch
):
    orch, hub, game, opened = no_router_setup(settings, cfg_dir, tmp_path, monkeypatch, "tool")
    hub.send(A, "design the shop")
    await settle(opened, 1)
    first, _, _ = hub.pull(A, None, str(opened[0]), 0, "claude_code", "cli")
    _, got, _ = hub.pull(A, first.session_id)
    await hub.reply(first.session_id, "Dạ, bản 1 đây", got[0]["message_id"])
    hub.close(first)
    reply, target = hub.send(A, "make it warmer", session_id=first.session_id)  # a Telegram reply
    await settle(opened, 2)
    assert target is None and opened[1] == opened[0]  # its own folder reopened, its context kept
    again, _, _ = hub.pull(A, None, str(opened[0]), 0, "claude_code", "cli")
    _, got, _ = hub.pull(A, again.session_id)
    assert got[0]["message_id"] == reply["message_id"]


async def test_a_former_main_works_as_an_adopted_dev_worker_and_is_never_closed(
    settings, cfg_dir, tmp_path, monkeypatch
):
    from datetime import timedelta

    from app.models import now

    orch, hub, game, opened = no_router_setup(settings, cfg_dir, tmp_path, monkeypatch, "product")
    old_main, _ = hub.claim_main(A, "claude_code", str(game), "app", "local_k05")
    rec = hub.adopt(old_main, "dev")
    assert rec["provider"] == "adopted" and hub.main_for(A) is not old_main
    m, target = hub.send(A, "fix the coin bug")
    assert target is old_main and opened == []  # the project's own conversation does the work
    _, got, _ = hub.pull(A, old_main.session_id)
    await hub.reply(old_main.session_id, "Dạ xong", got[0]["message_id"])
    old_main.last_work = now() - timedelta(hours=2)
    hub._close_idle_children()
    assert old_main.session_id in hub.sessions  # a user's conversation is never closed for idling
    with pytest.raises(ValueError, match="adopted"):
        hub.prune_child(old_main.cwd)


def test_repo_setup_records_the_project_kind(settings, monkeypatch):
    from app import cli

    monkeypatch.setattr(cli, "_settings", lambda: settings)
    assert cli._project_kind("New Game", None) == "product"  # new projects are products
    assert cli._project_kind("UIUXAgent", "tool") == "tool"
    assert cli._project_kind("UIUXAgent", None) == "tool"  # kept when not given


async def test_the_gateway_keeps_its_own_cli_gate_session_alive_as_the_gateway_agent(
    settings, cfg_dir, tmp_path, monkeypatch
):
    import asyncio
    from datetime import timedelta

    from app import channel as ch
    from app import projects
    from app.models import now

    gate_repo = tmp_path / "agent_tool"
    gate_repo.mkdir()
    monkeypatch.setattr(projects, "projects_root", lambda: tmp_path)
    monkeypatch.setattr(ch, "CLAUDE_JSON", tmp_path / "claude.json")
    orch, _ = make(settings, cfg_dir, [])
    orch.s.gate_cli = True
    hub, opened = orch.channel, []
    hub.launch = lambda path, agent, model="": opened.append((path, model))
    app_main, _ = hub.claim_main(
        "Agent Gateway", "claude_code", str(gate_repo / "wt"), "app", "local_g"
    )
    hub.registry.note_root("Agent Gateway", str(gate_repo))
    gate = gate_repo / ".agw-children" / "gate"
    assert hub.gate_path() == gate

    hub.ensure_gate()
    for _ in range(50):
        if opened:
            break
        await asyncio.sleep(0.02)
    assert opened == [(gate, "sonnet")] and (gate / "CLAUDE.md").exists()
    hub.ensure_gate()  # still opening: not opened twice
    await asyncio.sleep(0.05)
    assert len(opened) == 1

    cli, _, _ = hub.pull("Agent Gateway", None, str(gate), 0, "claude_code", "cli")
    assert hub.gateway_agent() is cli  # the CLI gate is the router now
    assert hub.child(app_main)["provider"] == "adopted"  # the app conversation works as dev

    hub.disconnect(cli)  # the gate dropped
    assert hub.gateway_agent() is None
    hub.ensure_gate()
    await asyncio.sleep(0.05)
    assert len(opened) == 1  # opened less than START_WAIT ago: waits for it to connect
    orch.store.put(
        "channel_setting", "gate", {"launched_at": (now() - timedelta(hours=1)).isoformat()}
    )
    hub.ensure_gate()
    for _ in range(50):
        if len(opened) == 2:
            break
        await asyncio.sleep(0.02)
    assert len(opened) == 2  # another gate session
