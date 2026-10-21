"""OpenCode connector against a mocked v2 HTTP API (real-server tests are in tests/integration)."""

import json

import httpx
import pytest

from app.connectors.base import AgentRequest, ConnectorError, UnsupportedCapability
from app.connectors.opencode import OpenCodeConnector, command_allowed, ruleset


def test_ruleset_is_deny_by_default():
    rw, ro = ruleset(False), ruleset(True)
    assert rw[0] == {"action": "*", "resource": "*", "effect": "deny"}
    assert {"action": "edit", "resource": "*", "effect": "allow"} in rw
    assert {"action": "shell", "resource": "*", "effect": "ask"} in rw
    assert not any(r["action"] in ("edit", "shell") for r in ro)
    assert rw[-1] == {"action": "read", "resource": "*.env.*", "effect": "deny"}  # last match wins


@pytest.mark.parametrize(
    "cmd,ok",
    [
        ("python3 -m pytest -q", True),
        ("python3 -m pytest", True),
        ("pip3 install pytest", False),
        ("python3 -m pytest; rm -rf /", False),
        ("python3 -m pytest && curl x", False),
        ("python3 -c 'x'", False),
        ("python3 -m pytest $(whoami)", False),
        ("git status --short", True),
        ("rm -rf .pytest_cache", False),
    ],
)
def test_command_allowlist(cmd, ok):
    assert command_allowed(cmd, ["python3 -m pytest", "git status"]) is ok


def server(outcome="succeeded", permissions=(), status=200):
    log = {"replies": [], "created": None, "interrupt": 0}
    perms = list(permissions)

    def h(req: httpx.Request):
        p = req.url.path
        if req.headers.get("authorization") is None:
            return httpx.Response(401)
        if status != 200:
            return httpx.Response(status, text="boom")
        if p == "/api/session" and req.method == "POST":
            log["created"] = json.loads(req.content)
            return httpx.Response(200, json={"data": {"id": "ses_1"}})
        if p.endswith("/prompt"):
            return httpx.Response(200, json={"data": {"id": "msg_1"}})
        if p.endswith("/permission") and req.method == "GET":
            return httpx.Response(
                200,
                json={"data": [x for x in perms if x["id"] not in [r[0] for r in log["replies"]]]},
            )
        if "/permission/" in p and p.endswith("/reply"):
            log["replies"].append((p.split("/")[-2], json.loads(req.content)))
            return httpx.Response(200, json={"data": True})
        if p.endswith("/wait"):
            return httpx.Response(204)
        if p == "/api/session/ses_1":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": "ses_1",
                        "outcome": outcome,
                        "cost": 0,
                        "tokens": {"input": 10, "output": 5},
                    }
                },
            )
        if p.endswith("/message"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "type": "assistant",
                            "model": {"id": "m", "providerID": "p"},
                            "content": [{"type": "text", "text": "Done."}],
                        }
                    ]
                },
            )
        if p.endswith("/diff"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "file": "a.py",
                            "status": "added",
                            "patch": "",
                            "additions": 1,
                            "deletions": 0,
                        }
                    ]
                },
            )
        if p.endswith("/interrupt"):
            log["interrupt"] += 1
            return httpx.Response(200, json={"interrupted": True})
        if p == "/api/info":
            return httpx.Response(200, json={"data": {"version": "2.1.0"}})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(h), auth=("opencode", "pw"))
    return OpenCodeConnector(
        "http://oc", "pw", shell_allowlist=["python3 -m pytest"], client=client
    ), log


async def test_execute_creates_restricted_session_and_answers_permissions(tmp_path):
    perms = [
        {"id": "per_1", "action": "shell", "resources": ["python3 -m pytest -q"]},
        {"id": "per_2", "action": "shell", "resources": ["pip3 install x"]},
        {"id": "per_3", "action": "external_directory", "resources": ["/etc"]},
    ]
    oc, log = server(permissions=perms)
    r = await oc.execute(AgentRequest("t1", "do it", workspace=tmp_path, read_only=False))
    assert log["created"]["location"] == {"directory": str(tmp_path.resolve())}
    assert log["created"]["permissions"] == ruleset(False)
    assert dict(log["replies"]) == {
        "per_1": {"decision": "once"},
        "per_2": {"decision": "reject", "message": "not allowed by gateway policy"},
        "per_3": {"decision": "reject", "message": "not allowed by gateway policy"},
    }
    assert (r.text, r.session_id, r.model, r.input_tokens) == ("Done.", "ses_1", "p/m", 10)
    assert r.metadata["files_changed"] == [{"file": "a.py", "status": "added"}]
    assert [d["decision"] for d in r.metadata["permission_decisions"]] == [
        "once",
        "reject",
        "reject",
    ]


async def test_failed_and_interrupted_outcomes_and_errors(tmp_path):
    oc, _ = server(outcome="failed")
    with pytest.raises(ConnectorError, match="outcome 'failed'"):
        await oc.execute(AgentRequest("t", "x", workspace=tmp_path, read_only=False))
    oc, _ = server(outcome="interrupted")
    with pytest.raises(ConnectorError) as e:
        await oc.execute(AgentRequest("t", "x", workspace=tmp_path, read_only=False))
    assert e.value.outcome == "unknown"
    oc, _ = server(status=500)
    with pytest.raises(ConnectorError, match="HTTP 500"):
        await oc.execute(AgentRequest("t", "x", workspace=tmp_path, read_only=False))
    with pytest.raises(ConnectorError, match="workspace"):
        await oc.execute(AgentRequest("t", "x"))
    from app.models import Attachment
    from tests.conftest import make_png  # noqa: F401

    att = Attachment(
        filename="a.png",
        mime_type="image/png",
        size_bytes=1,
        sha256="x",
        storage_path=tmp_path,
        source="t",
    )
    with pytest.raises(UnsupportedCapability):
        await oc.execute(AgentRequest("t", "x", workspace=tmp_path, attachments=[(att, b"x")]))


async def test_health_and_unreachable_server(tmp_path):
    oc, _ = server()
    assert (await oc.health_status())[0] == "validated"
    dead = OpenCodeConnector("http://127.0.0.1:9", "pw")
    status, detail = await dead.health_status()
    assert status == "unavailable" and "unreachable" in detail
    off = OpenCodeConnector(enabled=False)
    assert (await off.health_status())[0] == "unavailable"
