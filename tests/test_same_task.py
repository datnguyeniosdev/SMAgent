"""One task, one session: a follow-up reaches the session already doing the task, and a lane that
is being opened is opened once (seen in prod: a follow-up given to a fresh session of the same
lane without the context, and one request opening nine dev sessions while its worktree was made)."""

import asyncio

from tests.test_bidding import worker
from tests.test_central_gateway import A, central_setup, settle


async def test_a_follow_up_in_a_busy_topic_lane_waits_for_its_session(
    settings, cfg_dir, tmp_path, monkeypatch
):
    orch, hub, gw, own, game, opened = central_setup(settings, cfg_dir, tmp_path, monkeypatch)
    hub.registry.set_kind(A, "tool")
    first, _ = hub.send(A, "investigate the queue faults")
    hub.pull("Agent Gateway", gw.session_id)
    r = await hub.dispatch(gw.session_id, first["message_id"], "progress-report")
    doing, _, _ = hub.pull(A, None, r["path"], 0, "claude_code", "cli")
    hub.pull(A, doing.session_id)  # working on it

    follow, _ = hub.send(A, "what are you doing on the queue faults?")
    hub.pull("Agent Gateway", gw.session_id)
    r2 = await hub.dispatch(gw.session_id, follow["message_id"], "progress-report")
    assert r2["to"] == f"s{doing.num}" and len(opened) == 1  # queued behind it, no new session
    await hub.reply(doing.session_id, "Dạ xong", first["message_id"])
    _, got, _ = hub.pull(A, doing.session_id)
    assert got[0]["message_id"] == follow["message_id"]


async def test_another_topic_still_runs_in_parallel(settings, cfg_dir, tmp_path, monkeypatch):
    orch, hub, gw, own, game, opened = central_setup(settings, cfg_dir, tmp_path, monkeypatch)
    hub.registry.set_kind(A, "tool")
    shop = await worker(hub, gw, "redesign the shop screen", "ui")
    m, _ = hub.send(A, "shop again", session_id=shop.session_id)
    hub.pull(A, shop.session_id)  # busy
    other, _ = hub.send(A, "lay out the level map")
    hub.pull("Agent Gateway", gw.session_id)
    r = await hub.dispatch(gw.session_id, other["message_id"], "level")
    assert r["new_session"] and len(opened) == 2


async def test_an_agents_follow_up_goes_to_the_session_doing_its_first_ask(
    settings, cfg_dir, tmp_path, monkeypatch
):
    orch, hub, gw, own, game, opened = central_setup(settings, cfg_dir, tmp_path, monkeypatch)
    hub.registry.set_kind(A, "tool")
    asker, _, _ = hub.pull("Glazed Gems (GMA)", None, str(tmp_path), 0, "claude_code", "app")
    ask1 = orch.asks.create(A, "IP check of the app icon", from_session=asker.session_id)
    await settle(opened, 1)
    assert len(opened) == 1 and "glazed-gems-gma" in str(opened[0])  # the game's session, no Gate
    qa, _, _ = hub.pull(A, None, str(opened[0]), 0, "claude_code", "cli")
    _, got, _ = hub.pull(A, qa.session_id)  # working on ask 1, not answered yet
    assert got[0]["message_id"] == ask1["message_id"]

    ask2 = orch.asks.create(
        A, "Follow-up: the production pack is ready", from_session=asker.session_id
    )
    assert ask2["delivered_to"] == qa.session_id and len(opened) == 1
    msg = orch.store.get("channel_msg", ask2["message_id"])
    assert msg["state"] == "queued" and msg["queued_for"] == qa.session_id


async def test_a_lane_being_opened_is_opened_once(settings, cfg_dir, tmp_path, monkeypatch):
    from app import channel as ch

    orch, hub, gw, own, game, opened = central_setup(settings, cfg_dir, tmp_path, monkeypatch)
    slow = ch._worktree

    def worktree(repo, lane):  # a big repo: the copy takes a while
        import time

        time.sleep(0.4)
        return slow(repo, lane)

    monkeypatch.setattr(ch, "_worktree", worktree)
    m, _ = hub.send(A, "fix the chapter complete buttons")
    hub.pull("Agent Gateway", gw.session_id)
    dispatching = asyncio.create_task(hub.dispatch(gw.session_id, m["message_id"], "dev"))
    for _ in range(8):  # the watchdog and other sessions' polls flush meanwhile
        await asyncio.sleep(0.05)
        hub._flush_pending(A)
    r = await dispatching
    await settle(opened, 2)
    assert len(opened) == 1 and r["path"] == str(opened[0])
    msg = orch.store.get("channel_msg", m["message_id"])
    assert msg["wait_cwd"] == r["path"] and msg["state"] == "pending"
    kid, _, _ = hub.pull(A, None, r["path"], 0, "claude_code", "cli")
    _, got, _ = hub.pull(A, kid.session_id)
    assert got[0]["message_id"] == m["message_id"]
