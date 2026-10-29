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
