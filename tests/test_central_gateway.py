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
    hub.registry.set_kind(A, "tool")  # a product has no qa lane: its worker takes everything
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


async def test_a_product_has_one_worker_and_one_deploy_session(
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
    first = orch.store.get("channel_msg", d1["message_id"])
    assert "fresh copy" in first["text"] and "report the result yourself" in first["text"]
    d2, _ = hub.send(A, "release again after the fix")  # the deploy session is still starting
    await settle(opened, 3)
    assert [p.name.split("-")[0] for p in opened] == ["dev", "deploy"]  # one deploy session
    second = orch.store.get("channel_msg", d2["message_id"])
    assert second["wait_cwd"] == str(opened[1]) and "used again" in second["text"]
    dep, _, _ = hub.pull(A, None, str(opened[1]), 0, "claude_code", "cli")
    _, got, _ = hub.pull(A, dep.session_id)
    assert got[0]["message_id"] == d1["message_id"]  # in order, d2 next
    m3, target = hub.send(A, "qa: retest level 3")
    assert target is dev and len(opened) == 2  # QA, UI, art...: the worker's, in order
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


def make_skill(root, name, body="# skill\n"):
    d = root / ".claude" / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\n---\n{body}")
    return d


async def test_the_gateway_agent_gives_a_lane_skills_and_imports_other_projects_ones(
    settings, cfg_dir, tmp_path, monkeypatch
):
    from pathlib import Path

    from mcp.client import Client

    from app import skills as sk
    from app.mcp_server import build_mcp_server

    monkeypatch.setattr(sk, "GLOBAL", tmp_path / "home_skills")
    (tmp_path / "home_skills").mkdir()
    gdir = tmp_path / "home_skills" / "game-review"
    gdir.mkdir()
    (gdir / "SKILL.md").write_text("shared")
    orch, hub, gw, own, game, opened = central_setup(settings, cfg_dir, tmp_path, monkeypatch)
    make_skill(game, "unity-build")
    uiux = tmp_path / "uiux"
    make_skill(uiux, "uiux-art-director")
    hub.registry.note_root("UIUXAgent", str(uiux))
    hub.registry.note_root(A, str(game))

    cat = hub.skills_catalog(A)
    assert cat["project"] == ["unity-build"] and cat["global"] == ["game-review"]
    assert cat["other"] == {"UIUXAgent": ["uiux-art-director"]}
    with pytest.raises(ValueError, match="unknown skill"):
        hub.resolve_skills(A, ["invented-skill"])

    m, _ = hub.send(A, "redo the shop screen")
    _, got, _ = hub.pull("Agent Gateway", gw.session_id)
    assert (
        "unity-build" in got[0]["text"] and "list_skills" in got[0]["text"]
    )  # Gate sees the skills
    with pytest.raises(ValueError, match="unknown skill"):  # refused before anything moves
        await hub.dispatch(gw.session_id, m["message_id"], "ui", skills=["nope"])
    assert orch.store.get("channel_msg", m["message_id"])["session_id"] == gw.session_id

    r = await hub.dispatch(gw.session_id, m["message_id"], "ui",
                           skills=["unity-build", "uiux-art-director", "game-review", "unity-build"])  # fmt: skip
    path = Path(r["path"])
    imported = path / ".claude" / "skills" / "uiux-art-director" / "SKILL.md"
    assert imported.read_text().endswith("# skill\n")  # another project's skill, imported
    msg = orch.store.get("channel_msg", m["message_id"])
    assert [x["name"] for x in msg["skill_refs"]] == [
        "unity-build",
        "uiux-art-director",
        "game-review",
    ]
    assert {x["source"] for x in msg["skill_refs"]} == {A, "UIUXAgent", "global"}
    assert (
        all(len(x["version"]) == 12 for x in msg["skill_refs"])
        and "load `unity-build`" in msg["text"]
    )

    async with Client(build_mcp_server(orch)) as c:
        listed = (await c.call_tool("list_skills", {"project": A})).structured_content
        assert listed["other"] == {"UIUXAgent": ["uiux-art-director"]}
        before = len(orch.store.list("channel_msg"))
        bad = await c.call_tool("dispatch", {"session_id": gw.session_id, "lane": "ui", "text": "x",
                                             "skills": ["nope"]})  # fmt: skip
        assert bad.is_error and len(orch.store.list("channel_msg")) == before  # nothing left behind


async def test_the_health_check_follows_the_gate_pid_and_restarts_a_dead_one(
    settings, cfg_dir, tmp_path, monkeypatch
):
    import asyncio

    from app import channel as ch
    from app import projects

    gate_repo = tmp_path / "agent_tool"
    gate_repo.mkdir()
    monkeypatch.setattr(projects, "projects_root", lambda: tmp_path)
    monkeypatch.setattr(ch, "CLAUDE_JSON", tmp_path / "claude.json")
    alive = {4242: True}
    monkeypatch.setattr(ch, "claude_alive", lambda pid: alive.get(pid, False))
    orch, _ = make(settings, cfg_dir, [])
    orch.s.gate_cli = True
    hub, opened = orch.channel, []
    hub.launch = lambda path, agent, model="": opened.append(path)
    hub.registry.note_root("Agent Gateway", str(gate_repo))
    gate = hub.gate_path()
    gate.mkdir(parents=True)

    s, _, _ = hub.pull("Agent Gateway", None, str(gate), 0, "claude_code", "cli", pid=4242)
    st = hub.gate_status()
    assert (st["pid"], st["process_alive"], st["healthy"], st["gateway_agent"]) == (
        4242,
        True,
        True,
        True,
    )
    assert orch.store.get("channel_session", s.session_id)["claude_pid"] == 4242
    hub.ensure_gate()
    await asyncio.sleep(0.05)
    assert opened == []  # alive and connected: nothing to do

    alive[4242] = False  # the gate's Claude process died (its session still looks listed)
    hub.ensure_gate()
    for _ in range(50):
        if opened:
            break
        await asyncio.sleep(0.02)
    assert opened == [gate] and s.session_id not in hub.sessions
    rec = orch.store.get("channel_setting", "gate")
    assert rec["launched_at"] and rec["pid"] is None


async def test_health_reports_the_gate_and_the_waiter_sends_its_claude_pid(
    settings, cfg_dir, tmp_path, monkeypatch
):
    from fastapi.testclient import TestClient

    from app import main as main_mod
    from app import waiter as w
    from tests.test_waiter import Resp

    seen = []
    monkeypatch.setenv("CLAUDE_PID", "777")
    monkeypatch.setattr(w, "WAITERS", tmp_path / "waiters")

    def urlopen(url, timeout=None):
        seen.append(url)
        return Resp({"session_id": "x", "queued": True, "next": "chm_1"})

    monkeypatch.setattr(w.urllib.request, "urlopen", urlopen)
    w.wait("P", "/w/p", "app", 8000)
    assert "pid=777" in seen[0]

    orch, _ = make(settings, cfg_dir, [])
    orch.s.gate_cli = True
    main_mod.app.state.orch = orch
    body = TestClient(main_mod.app).get("/health").json()
    assert body["status"] == "ok" and body["gate"]["healthy"] is False


async def test_a_lanes_current_work_names_the_skills_it_must_use(
    settings, cfg_dir, tmp_path, monkeypatch
):
    from pathlib import Path

    from app import skills as sk

    monkeypatch.setattr(sk, "GLOBAL", tmp_path / "none")
    orch, hub, gw, own, game, opened = central_setup(settings, cfg_dir, tmp_path, monkeypatch)
    make_skill(game, "unity-build")
    orch.s.core_skills = {A: ["unity-build"]}
    m, _ = hub.send(A, "fix the level")
    assert "MANDATORY core skills" in m["text"] and m["required_skills"] == ["unity-build"]
    r = await hub.dispatch(gw.session_id, m["message_id"], "dev", skills=["unity-build"])
    kid, _, _ = hub.pull(A, None, str(Path(r["path"])), 0, "claude_code", "cli")
    hub.pull(A, kid.session_id)
    assert hub.required_skills(kid.session_id) == [
        {"message_id": m["message_id"], "skills": ["unity-build"], "commands": []}
    ]
    assert "MANDATORY skills" in orch.store.get("channel_msg", m["message_id"])["text"]


async def test_a_former_main_conversation_folds_back_so_answers_reach_it(
    settings, cfg_dir, tmp_path, monkeypatch
):
    """Seen in prod (hin_14e92eca79): the unpinned UIUX Main asked with the folder's shared session
    while its waiter still listened on its old #Main session, so the operator's answer sat queued."""
    orch, hub, game, opened = no_router_setup(settings, cfg_dir, tmp_path, monkeypatch, "tool")
    main, _ = hub.claim_main(A, "claude_code", str(game), "app", "local_ui")
    base = hub.pull_id(A, "claude_code", str(game), "app")
    assert hub.conversation_sid(A, "claude_code", str(game), "app", "local_ui") == main.session_id
    hub.adopt(main, "task")  # unpinned by the migration
    assert hub.conversation_sid(A, "claude_code", str(game), "app", "local_ui") == base
    state = hub.peek(A, "claude_code", str(game), "app", instance="local_ui")
    assert state["session_id"] == base  # its waiter switches to the shared session
    hi = await orch.collab.request_human_input("Dịch ngược APK?", project=A, session_id=base)
    orch.collab.answer_human_input(hi.request_id, "Đồng ý", "1", free=True)
    _, got, _ = hub.pull(A, base)
    assert any("Đồng ý" in m["text"] for m in got)


def test_a_task_rule_binds_matching_requests_in_any_project(settings, cfg_dir):
    from app.gateway_config import TaskRule

    orch, _ = make(settings, cfg_dir, [])
    orch.gateway_cfg.routing.task_rules = [
        TaskRule(match=r"(?i)dịch ngược|\bapk\b", rule="use rea first", require_command="rea")
    ]
    hub = orch.channel
    s, _, _ = hub.pull(A, None, "/w/a")
    m, _ = hub.send(A, "dịch ngược APK Colorever")
    assert "MANDATORY rule: use rea first" in m["text"] and m["required_commands"] == ["rea"]
    plain, _ = hub.send(A, "đổi màu nút")
    assert "MANDATORY rule" not in plain["text"]
    _, got, _ = hub.pull(A, s.session_id)
    assert hub.required_skills(s.session_id) == [{"message_id": got[0]["message_id"], "skills": [],
                                                  "commands": ["rea"]}]  # fmt: skip


async def test_gate_dispatches_by_role_and_the_lane_reads_its_task_brief(
    settings, cfg_dir, tmp_path, monkeypatch
):
    from pathlib import Path

    from mcp.client import Client

    from app import skills as sk
    from app.gateway_config import RoleDef
    from app.mcp_server import build_mcp_server

    monkeypatch.setattr(sk, "GLOBAL", tmp_path / "none")
    orch, hub, gw, own, game, opened = central_setup(settings, cfg_dir, tmp_path, monkeypatch)
    qa = tmp_path / "qa"
    make_skill(qa, "qa-agent")
    po = tmp_path / "po"
    (po / "skills" / "po-agent").mkdir(parents=True)  # PO keeps its skills in skills/, not .claude/
    (po / "skills" / "po-agent" / "SKILL.md").write_text("po")
    hub.registry.note_root("QAIPAgent", str(qa))
    hub.registry.note_root("PO Game Agent", str(po))
    orch.gateway_cfg.routing.roles = {
        "qa": RoleDef(description="testing", skills=["QAIPAgent/qa-agent"], prompt="You are QA."),
        "po": RoleDef(
            description="product", skills=["PO Game Agent/po-agent"], prompt="You are PO."
        ),
    }
    m, _ = hub.send(A, "retest the shop flow")
    _, got, _ = hub.pull("Agent Gateway", gw.session_id)
    assert "dispatch(role=...)" in got[0]["text"] and "qa: testing" in got[0]["text"]
    with pytest.raises(ValueError, match="unknown role"):
        await hub.dispatch(gw.session_id, m["message_id"], "qa", role="tester")

    r = await hub.dispatch(
        gw.session_id, m["message_id"], "qa", role="qa", brief="focus on payments"
    )
    path = Path(r["path"])
    assert (path / ".claude/skills/qa-agent/SKILL.md").exists()  # the role's skill, imported
    kid, _, _ = hub.pull(A, None, str(path), 0, "claude_code", "cli")
    _, got, _ = hub.pull(A, kid.session_id)
    assert (
        "MANDATORY role: qa. You are QA." in got[0]["text"] and "get_task_brief" in got[0]["text"]
    )
    async with Client(build_mcp_server(orch)) as c:
        brief = (await c.call_tool("get_task_brief", {"message_id": m["message_id"],
                                                      "session_id": kid.session_id})).structured_content  # fmt: skip
        assert (brief["role"], brief["role_prompt"], brief["brief"]) == (
            "qa",
            "You are QA.",
            "focus on payments",
        )
        assert (
            brief["required_skills"] == ["qa-agent"] and brief["request"] == "retest the shop flow"
        )
        other = await c.call_tool(
            "get_task_brief", {"message_id": m["message_id"], "session_id": gw.session_id}
        )
        assert other.is_error  # only the session that holds it
    assert hub.skills_catalog("PO Game Agent")["project"] == ["po-agent"]


async def test_system_notices_go_to_the_notice_channel_and_work_stays_in_the_group(
    settings, cfg_dir
):
    import asyncio

    from tests.test_config_gateway import CONTROL, gateway

    orch, _ = make(settings, cfg_dir, [])
    gw = gateway(orch)
    hub = orch.channel
    hub._ops("🔌 s1 connected")
    await asyncio.sleep(0.01)
    assert gw.bot.sent[-1][1]["chat_id"] == CONTROL  # no notice channel set: the group as before
    orch.s.telegram_notice_chat_id = -100999
    hub._ops("🧭 m1 → lane ui")
    await asyncio.sleep(0.01)
    assert gw.bot.sent[-1][1]["chat_id"] == -100999 and gw.bot.sent[-1][1]["disable_notification"]
    await orch.collab.request_human_input("Duyệt commit?", project=A, options=["Có", "Không"])
    assert gw.bot.sent[-1][1]["chat_id"] == CONTROL  # a question stays with the operator's work


async def test_only_the_gateway_project_has_a_main(settings, cfg_dir, tmp_path, monkeypatch):
    settings.main_projects = []  # the default: no MAIN_PROJECTS
    orch, hub, gw, own, game, opened = central_setup(settings, cfg_dir, tmp_path, monkeypatch)
    hub.registry.set_routing_mode(A, "main")  # ignored: a game has no main
    assert hub.main_for(A) is None and hub.main_for("Agent Gateway") is gw
    with pytest.raises(ValueError):
        hub.set_main(own)
    m, target = hub.send(A, "fix the hud")
    assert target is gw and "do not do it yourself" in m["text"].lower()  # the gateway routes it
