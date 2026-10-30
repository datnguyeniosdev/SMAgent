"""Offer/bid routing (tool projects in bid_projects): a new topic is offered to the project's idle
sessions first; the yes with the most context takes it, else it is placed as dispatch would (a new
session while the project is under tool_parallel). Busy sessions are never asked, a lane that has a
session keeps it, and an offer is never routed as work."""

import asyncio
from pathlib import Path

import pytest

from app.channel import ChannelHub
from tests.test_central_gateway import A, central_setup


async def worker(hub, gw, text, lane):
    """A finished lane session: its request routed by the Gateway Agent, done and answered."""
    m, _ = hub.send(A, text)
    hub.pull("Agent Gateway", gw.session_id)
    r = await hub.dispatch(gw.session_id, m["message_id"], lane)
    kid, _, _ = hub.pull(A, None, r["path"], 0, "claude_code", "cli")
    hub.pull(A, kid.session_id)
    await hub.reply(kid.session_id, "Dạ xong", m["message_id"])
    return kid


async def offer_round(hub, gw, text, lane):
    m, _ = hub.send(A, text)
    hub.pull("Agent Gateway", gw.session_id)
    return m, await hub.dispatch(gw.session_id, m["message_id"], lane)


def offer_of(hub, kid):
    _, got, _ = hub.pull(A, kid.session_id)
    assert len(got) == 1 and got[0]["meta"]["kind"] == "offer"
    return got[0]


async def placed(hub, within=2):
    await asyncio.wait_for(asyncio.gather(*list(hub._bg)), within)


async def pilot(settings, cfg_dir, tmp_path, monkeypatch, wait=30):
    orch, hub, gw, own, game, opened = central_setup(settings, cfg_dir, tmp_path, monkeypatch)
    hub.registry.set_kind(A, "tool")
    shop = await worker(hub, gw, "redesign the shop screen palette and the coin icons", "ui")
    level = await worker(hub, gw, "lay out the level select map", "level")
    orch.s.bid_projects, orch.s.offer_wait_s = [A], wait
    return orch, hub, gw, shop, level, opened


async def test_the_yes_with_the_most_context_gets_the_request_as_soon_as_all_answered(
    settings, cfg_dir, tmp_path, monkeypatch
):
    orch, hub, gw, shop, level, opened = await pilot(settings, cfg_dir, tmp_path, monkeypatch)
    m, r = await offer_round(hub, gw, "shop screen palette: brighter coin icons", "shop-icons")
    assert r["to"] == "offered" and r["offers"] == [f"s{shop.num}", f"s{level.num}"]  # best first
    assert hub.in_progress(gw.session_id) == []  # the Gateway Agent is free at once
    await asyncio.sleep(0)  # the placement is waiting for the bids
    o_shop, o_level = offer_of(hub, shop), offer_of(hub, level)
    assert "Do not start it" in o_shop["text"] and shop.session_id in o_shop["text"]
    assert hub._idle(shop) and hub.open_messages()[0]["message_id"] == m["message_id"]
    with pytest.raises(PermissionError):
        hub.bid(level.session_id, o_shop["message_id"], True)
    hub.bid(level.session_id, o_level["message_id"], True, "high")
    hub._flush_pending(A)  # a flush while the offers are out routes nothing
    hub.bid(shop.session_id, o_shop["message_id"], True, "medium")
    await placed(hub)  # all answered: placed at once, not after the 30 s window

    _, got, _ = hub.pull(A, shop.session_id)  # the same words beat the other's "high"
    assert got[0]["message_id"] == m["message_id"] and len(opened) == 2
    assert gw.queue.empty() and hub.in_progress(gw.session_id) == []
    assert "too late" in hub.bid(level.session_id, o_level["message_id"], True)["note"]

    follow, r2 = await offer_round(hub, gw, "shop icons: one more", "shop-icons")
    assert r2["to"] == f"s{shop.num}"  # the topic stays with the session that took it


async def test_when_every_session_says_no_or_is_silent_a_new_session_takes_it(
    settings, cfg_dir, tmp_path, monkeypatch
):
    orch, hub, gw, shop, level, opened = await pilot(settings, cfg_dir, tmp_path, monkeypatch, 0.2)
    m, r = await offer_round(hub, gw, "add a daily reward popup", "rewards")
    assert set(r["offers"]) == {f"s{shop.num}", f"s{level.num}"}
    hub.bid(shop.session_id, offer_of(hub, shop)["message_id"], False, reason="busy soon")
    await placed(hub)  # level never answers

    assert len(opened) == 3 and Path(opened[-1]).name.startswith("rewards")  # a new session
    assert hub.pull(A, level.session_id)[1] == []  # its unanswered offer was withdrawn
    assert orch.store.get("channel_msg", m["message_id"])["wait_cwd"] == str(opened[-1])
    m2, r2 = await offer_round(hub, gw, "add a weekly reward popup", "rewards-2")
    assert f"s{level.num}" not in r2["offers"]  # silent: not asked again for a while


async def test_at_the_session_limit_a_no_does_not_open_more_sessions(
    settings, cfg_dir, tmp_path, monkeypatch
):
    orch, hub, gw, shop, level, opened = await pilot(settings, cfg_dir, tmp_path, monkeypatch, 0.2)
    orch.s.bid_projects = []
    boss = await worker(hub, gw, "tune the boss fight", "boss")
    orch.s.bid_projects = [A]
    m, r = await offer_round(hub, gw, "add a daily reward popup", "rewards")
    for kid in (shop, level, boss):
        hub.bid(kid.session_id, offer_of(hub, kid)["message_id"], False)
    await placed(hub)
    assert len(opened) == 3  # tool_parallel=3: it waits behind one of them instead
    assert orch.store.get("channel_msg", m["message_id"])["state"] == "queued"


async def test_an_offer_answered_with_reply_message_is_a_bid_not_a_report(
    settings, cfg_dir, tmp_path, monkeypatch
):
    orch, hub, gw, shop, level, opened = await pilot(settings, cfg_dir, tmp_path, monkeypatch)
    reports = []
    monkeypatch.setattr(orch.collab, "submit_report", lambda *a, **k: reports.append(a))
    m, r = await offer_round(hub, gw, "shop screen palette again", "shop-icons")
    await asyncio.sleep(0)
    await hub.reply(shop.session_id, "YES high", offer_of(hub, shop)["message_id"])
    await hub.reply(level.session_id, "Dạ, em đang bận ạ", offer_of(hub, level)["message_id"])
    await placed(hub)
    assert reports == [] and hub.pull(A, shop.session_id)[1][0]["message_id"] == m["message_id"]


async def test_offers_lapse_when_their_session_leaves_or_the_gateway_restarts(
    settings, cfg_dir, tmp_path, monkeypatch
):
    orch, hub, gw, shop, level, opened = await pilot(settings, cfg_dir, tmp_path, monkeypatch)
    m, r = await offer_round(hub, gw, "add a daily reward popup", "rewards")
    hub.disconnect(level)  # left with its offer unread
    assert gw.queue.empty()  # the offer is not routed as a request
    restarted = ChannelHub(orch)  # the gateway restarts in the window: offers lapse
    offers = [x for x in orch.store.list("channel_msg") if x.get("kind") == "offer"]
    assert {o["state"] for o in offers} == {"cancelled"}
    req = orch.store.get("channel_msg", m["message_id"])
    assert req["state"] == "pending" and req["lane"] == "rewards" and not req.get("route")
    assert "You are the Gateway Agent" not in req["text"]  # placed as asked, in its lane
    for t in list(hub._bg):
        t.cancel()
    assert restarted.open_messages()[0]["message_id"] == m["message_id"]


async def test_no_offers_when_switched_off_outside_the_pilot_or_for_a_held_lane(
    settings, cfg_dir, tmp_path, monkeypatch
):
    orch, hub, gw, shop, level, opened = await pilot(settings, cfg_dir, tmp_path, monkeypatch)
    hub.send(A, "shop: one more icon", session_id=shop.session_id)
    hub.pull(A, shop.session_id)  # busy on the shop screen
    m, r = await offer_round(hub, gw, "shop screen: how far along are you?", "ui")
    assert r["to"] == f"s{shop.num}" and hub.pull(A, level.session_id)[1] == []  # its lane

    orch.switches.set("bid", False)  # the operator stops the pilot in /mcp
    m2, r2 = await offer_round(hub, gw, "add a daily reward popup", "rewards")
    assert r2["to"] != "offered"
    orch.switches.set("bid", True)
    orch.s.bid_projects = ["Some Other Tool"]
    m3, r3 = await offer_round(hub, gw, "add a weekly reward popup", "rewards-2")
    assert r3["to"] != "offered" and hub.pull(A, level.session_id)[1] == []


async def test_a_product_is_never_auctioned(settings, cfg_dir, tmp_path, monkeypatch):
    orch, hub, gw, own, game, opened = central_setup(settings, cfg_dir, tmp_path, monkeypatch)
    dev = await worker(hub, gw, "fix the coin bug", "dev")
    orch.s.bid_projects = [A]
    m, r = await offer_round(hub, gw, "make the shop calmer", "ui")
    assert r["to"] == f"s{dev.num}" and r["lane"] == "dev"  # its one worker
