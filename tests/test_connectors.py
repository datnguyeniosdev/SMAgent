"""Connector behaviour against mocked transports and a fake CLI binary (no real APIs)."""

import json
import stat

import httpx
import pytest

from app.connectors import http_providers
from app.connectors.base import AgentRequest, Cap, ConnectorError, UnsupportedCapability
from app.connectors.claude_code import ClaudeCodeConnector
from app.connectors.http_providers import ChatCompletionsProvider, OpenAIImageProvider
from app.models import Attachment
from tests.conftest import make_png

OK_BODY = {
    "model": "m-1",
    "choices": [{"message": {"content": '{"x": 1}'}}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 3},
}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def fast(_):
        return None

    monkeypatch.setattr(http_providers, "_sleep", fast)


def provider(handler, caps=frozenset({Cap.text}), schema_mode=True):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ChatCompletionsProvider(
        "p", "https://api.test/v1", "sk-test", "m", caps, schema_mode, client=client
    )


def att(mime, data, name="f"):
    a = Attachment(
        filename=name,
        mime_type=mime,
        size_bytes=len(data),
        sha256="x",
        storage_path="/x",
        source="t",
    )
    return (a, data)


async def test_success_usage_structured():
    seen = {}

    def h(req):
        seen["body"] = json.loads(req.content)
        seen["auth"] = req.headers["authorization"]
        return httpx.Response(200, json=OK_BODY)

    p = provider(h, schema_mode=False)
    r = await p.execute(AgentRequest("t", "hi", json_schema={"type": "object"}))
    assert r.structured == {"x": 1} and r.input_tokens == 10 and r.model == "m-1"
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert "schema" in seen["body"]["messages"][0]["content"]
    assert seen["auth"] == "Bearer sk-test"


async def test_invalid_credentials():
    p = provider(lambda r: httpx.Response(401, json={"error": "bad key sk-test"}))
    with pytest.raises(ConnectorError) as e:
        await p.execute(AgentRequest("t", "hi"))
    assert e.value.category == "auth" and "sk-test" not in str(e.value)


async def test_rate_limit_retry_then_success():
    calls = []

    def h(req):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, headers={"retry-after": "1"})
        return httpx.Response(200, json=OK_BODY)

    r = await provider(h).execute(AgentRequest("t", "hi"))
    assert len(calls) == 3 and r.text


async def test_rate_limit_exhausted():
    p = provider(lambda r: httpx.Response(429))
    with pytest.raises(ConnectorError) as e:
        await p.execute(AgentRequest("t", "hi"))
    assert e.value.category == "rate_limit" and e.value.retryable


async def test_timeout_is_unknown_outcome():
    def h(req):
        raise httpx.ReadTimeout("slow", request=req)

    with pytest.raises(ConnectorError) as e:
        await provider(h).execute(AgentRequest("t", "hi"))
    assert e.value.category == "timeout" and e.value.outcome == "unknown"


async def test_unsupported_image_and_file_inputs():
    p = provider(lambda r: httpx.Response(200, json=OK_BODY))
    with pytest.raises(UnsupportedCapability, match="image"):
        await p.execute(AgentRequest("t", "hi", attachments=[att("image/png", make_png(1, 1))]))
    with pytest.raises(UnsupportedCapability, match="application/pdf"):
        await p.execute(AgentRequest("t", "hi", attachments=[att("application/pdf", b"%PDF-")]))
    with pytest.raises(UnsupportedCapability):
        await p.execute(AgentRequest("t", "hi", read_only=False))
    with pytest.raises(UnsupportedCapability):
        await p.execute(AgentRequest("t", "hi", resume_session="s"))


async def test_image_input_encoded_when_supported():
    body = {}

    def h(req):
        body.update(json.loads(req.content))
        return httpx.Response(200, json=OK_BODY)

    p = provider(h, caps=frozenset({Cap.text, Cap.image_input}))
    await p.execute(
        AgentRequest(
            "t", "hi", attachments=[att("image/png", make_png(1, 1)), att("text/plain", b"notes")]
        )
    )
    parts = body["messages"][-1]["content"]
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "notes" in parts[2]["text"]


async def test_disabled_provider():
    p = ChatCompletionsProvider("p", "https://x", None, None, frozenset({Cap.text}), True)
    assert not p.available
    with pytest.raises(ConnectorError, match="not configured"):
        await p.execute(AgentRequest("t", "hi"))


async def test_image_provider_decodes_b64():
    import base64

    png = make_png(2, 2)
    sent = {}

    def h(req):
        sent.update(json.loads(req.content))
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(png).decode()}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(h))
    p = OpenAIImageProvider("sk-test", "img-model", "https://api.test/v1", client)
    assert await p.generate("a coin", "1024x1024", transparent=True) == png
    assert sent["background"] == "transparent" and sent["output_format"] == "png"


# ---------- Claude Code CLI (fake binary) ----------


def fake_cli(tmp_path, script: str):
    path = tmp_path / "fake-claude"
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def test_claude_argv_read_only_vs_write(tmp_path):
    c = ClaudeCodeConnector("claude", model="sonnet", allowed_tools="Bash(uv run pytest *)")
    a = att("image/png", b"x", "ref.png")
    a[0].storage_path = tmp_path / "ref.png"
    ro = c.build_argv(AgentRequest("t", "review", attachments=[a], json_schema={"type": "object"}))
    assert ["--permission-mode", "dontAsk"] == ro[4:6] and "--tools" in ro
    assert ro[ro.index("--add-dir") + 1] == str(tmp_path)
    assert ro[-2] == "--" and "ref.png" in ro[-1]
    rw = c.build_argv(AgentRequest("t", "fix", read_only=False, resume_session="abc"))
    assert "acceptEdits" in rw and "--allowedTools" in rw and rw[rw.index("--resume") + 1] == "abc"


async def test_claude_success_parse(tmp_path):
    out = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "done",
        "session_id": "s-1",
        "total_cost_usd": 0.01,
        "usage": {"input_tokens": 5, "output_tokens": 2},
        "modelUsage": {"claude-x": {}},
    }
    c = ClaudeCodeConnector(fake_cli(tmp_path, f"echo '{json.dumps(out)}'"))
    r = await c.execute(AgentRequest("t", "hi", workspace=tmp_path))
    assert (r.text, r.session_id, r.cost_usd, r.model) == ("done", "s-1", 0.01, "claude-x")


async def test_claude_is_error_with_success_subtype(tmp_path):
    # Observed for real on Claude Code 2.1.71: auth failure reported as subtype=success.
    out = {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "result": "Failed to authenticate. API Error: 401",
    }
    c = ClaudeCodeConnector(fake_cli(tmp_path, f"echo '{json.dumps(out)}'; exit 1"))
    with pytest.raises(ConnectorError) as e:
        await c.execute(AgentRequest("t", "hi", workspace=tmp_path))
    assert e.value.category == "auth"


async def test_claude_non_json_and_missing_binary(tmp_path):
    c = ClaudeCodeConnector(fake_cli(tmp_path, "echo 'Error: nested session' >&2; exit 1"))
    with pytest.raises(ConnectorError, match="nested session"):
        await c.execute(AgentRequest("t", "hi", workspace=tmp_path))
    with pytest.raises(ConnectorError) as e:
        await ClaudeCodeConnector(str(tmp_path / "nope")).execute(
            AgentRequest("t", "hi", workspace=tmp_path)
        )
    assert e.value.category == "unavailable"


async def test_claude_timeout_write_is_unknown(tmp_path):
    c = ClaudeCodeConnector(fake_cli(tmp_path, "sleep 30"), timeout_s=0.3)
    with pytest.raises(ConnectorError) as e:
        await c.execute(AgentRequest("t", "hi", workspace=tmp_path, read_only=False))
    assert e.value.category == "timeout" and e.value.outcome == "unknown"


async def test_claude_cancel_kills_process(tmp_path):
    import asyncio

    c = ClaudeCodeConnector(fake_cli(tmp_path, "sleep 30"))
    run = asyncio.create_task(c.execute(AgentRequest("t1", "hi", workspace=tmp_path)))
    await asyncio.sleep(0.3)
    assert await c.cancel("t1") is True
    with pytest.raises(ConnectorError):
        await asyncio.wait_for(run, 10)
    assert await c.cancel("t1") is False  # nothing in flight


async def test_claude_strips_nested_session_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:secret")
    out = '{"is_error": false, "result": "$CLAUDECODE|$TELEGRAM_BOT_TOKEN"}'
    c = ClaudeCodeConnector(fake_cli(tmp_path, f'echo "{out.replace(chr(34), chr(92) + chr(34))}"'))
    r = await c.execute(AgentRequest("t", "hi", workspace=tmp_path))
    assert r.text == "|"
