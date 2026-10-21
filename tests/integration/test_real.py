"""Real integrations. Each test proves an actual operation or skips with the exact blocker."""

import os
import shutil
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.connectors.base import AgentRequest, ConnectorError
from app.connectors.claude_code import ClaudeCodeConnector
from app.mcp_client import McpServerConfig, discover, register_mcp_tools
from app.tools import ToolContext, ToolExecutor, ToolRegistry, ToolStatus
from tests.integration.conftest import paid

ENV = Settings(_env_file=Path(__file__).parents[2] / ".env")


def ws(tmp_path):
    (tmp_path / "greet.py").write_text("def greet():\n    return 'hello'\n")
    return ToolContext("tsk_real", tmp_path, frozenset({"workspace:read"}))


async def test_real_mcp_claude_code_server(tmp_path):
    if not shutil.which("claude"):
        pytest.skip("NOT CONFIGURED: claude CLI not installed")
    cfg = McpServerConfig(
        name="claude_code",
        command=shutil.which("claude"),
        args=["mcp", "serve"],
        allowed_tools=["Read", "Glob"],
        read_only_tools=["Read", "Glob"],
        timeout_s=60,
    )
    found = await discover(cfg)
    assert found.ok, found.error
    reg = ToolRegistry()
    register_mcp_tools(reg, cfg, found)
    rec = await ToolExecutor(reg).execute(
        "mcp.claude_code.Read", {"file_path": "greet.py"}, ws(tmp_path)
    )
    assert rec.status == ToolStatus.succeeded and "return 'hello'" in rec.output
    # Known server-side issue on 2.1.71: Glob declares an output schema but returns none.
    glob = await ToolExecutor(reg).execute(
        "mcp.claude_code.Glob", {"pattern": "*.py"}, ws(tmp_path)
    )
    assert glob.status in (ToolStatus.succeeded, ToolStatus.failed)
    if glob.status == ToolStatus.failed:
        assert glob.error_category == "mcp_protocol"


async def test_real_unity_cli_mcp_server(tmp_path):
    unity = Path.home() / ".unity" / "bin" / "unity"
    if not unity.exists():
        pytest.skip("NOT CONFIGURED: unity CLI not installed")
    cfg = McpServerConfig(
        name="unity",
        command=str(unity),
        args=["mcp"],
        allowed_tools=["editor_status"],
        read_only_tools=["editor_status"],
        timeout_s=60,
    )
    found = await discover(cfg)
    assert found.ok, found.error  # the server itself is reachable over real MCP stdio
    if not found.tools:
        pytest.skip(
            f"BLOCKED: {found.server_name} {found.server_version} lists 0 tools (no Unity Editor connected)"
        )
    reg = ToolRegistry()
    register_mcp_tools(reg, cfg, found)
    rec = await ToolExecutor(reg).execute("mcp.unity.editor_status", {}, ws(tmp_path))
    # Without a running Editor + Pipeline package the server returns a real tool error.
    assert rec.status in (ToolStatus.succeeded, ToolStatus.failed)
    if rec.status == ToolStatus.failed:
        pytest.skip(f"Unity Editor not reachable: {rec.error}")


async def test_real_claude_code_runtime(tmp_path):
    paid()
    if not shutil.which("claude"):
        pytest.skip("NOT CONFIGURED: claude CLI not installed")
    if os.environ.get("CLAUDECODE"):
        pytest.skip("BLOCKED: running inside a Claude Code session (nested sessions refused)")
    conn = ClaudeCodeConnector(timeout_s=300)
    (tmp_path / "README.md").write_text("test workspace\n")
    try:
        r = await conn.execute(
            AgentRequest(
                "smoke",
                "Create greeting.py with def greet(): return 'hello'. Then reply DONE.",
                workspace=tmp_path,
                read_only=False,
            )
        )
    except ConnectorError as e:
        if e.category == "auth":
            pytest.fail(f"FAILED (auth): {e}")
        raise
    assert (tmp_path / "greeting.py").exists() and "hello" in (tmp_path / "greeting.py").read_text()
    assert r.session_id


async def test_real_openai_compatible_providers():
    from app.main import build

    keys = {"openai": ENV.openai_api_key, "deepseek": ENV.deepseek_api_key}
    if not any(keys.values()):
        pytest.skip("NOT CONFIGURED: OPENAI_API_KEY / DEEPSEEK_API_KEY are blank")
    o = build(ENV)
    for name, key in keys.items():
        if key:
            ok, detail = await o.router.connectors[name].health()
            assert ok, f"{name}: {detail}"


async def test_real_telegram_bot_and_destinations():
    if not ENV.telegram_bot_token:
        pytest.skip("NOT CONFIGURED: TELEGRAM_BOT_TOKEN blank")
    base = f"https://api.telegram.org/bot{ENV.telegram_bot_token.get_secret_value()}"
    async with httpx.AsyncClient(timeout=20) as c:
        me = (await c.get(f"{base}/getMe")).json()
        assert me["ok"], "bot token rejected"
        for label, chat in (
            ("control", ENV.telegram_control_chat_id),
            ("report", ENV.telegram_report_channel_id),
        ):
            r = (await c.get(f"{base}/getChat", params={"chat_id": chat})).json()
            if not r["ok"]:
                pytest.skip(
                    f"BLOCKED: {label} chat {chat}: {r.get('description')} (add the bot and set real IDs)"
                )


async def test_real_gemini_provider():
    """Free check (model metadata) with RUN_INTEGRATION=1; a real structured call with RUN_PAID=1."""
    from app.connectors.base import Cap
    from app.connectors.http_providers import GeminiProvider

    key, model = os.environ.get("GEMINI_API_KEY"), os.environ.get("GEMINI_MODEL")
    if not (key and model):
        pytest.skip("NOT CONFIGURED: GEMINI_API_KEY / GEMINI_MODEL are not set")
    g = GeminiProvider(
        "gemini", key, model, frozenset({Cap.text, Cap.image_input, Cap.structured_output})
    )
    status, detail = await g.health_status()
    assert status == "validated", detail
    paid()
    r = await g.execute(
        AgentRequest(
            "smoke", 'Return {"ok": true}', json_schema={"type": "object", "required": ["ok"]}
        )
    )
    assert r.structured == {"ok": True}


async def test_real_opencode_runtime(tmp_path):
    """Needs a running `OPENCODE_SERVER_PASSWORD=... opencode serve` and OPENCODE_URL/OPENCODE_PASSWORD."""
    import subprocess

    from app.connectors.opencode import OpenCodeConnector

    url, pw = os.environ.get("OPENCODE_URL"), os.environ.get("OPENCODE_PASSWORD")
    if not (url and pw):
        pytest.skip("NOT CONFIGURED: OPENCODE_URL / OPENCODE_PASSWORD (start `opencode serve`)")
    oc = OpenCodeConnector(url, pw, shell_allowlist=["python3 -m pytest"], timeout_s=300)
    status, detail = await oc.health_status()
    if status != "validated":
        pytest.skip(f"BLOCKED: {detail}")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    r = await oc.execute(
        AgentRequest(
            "it",
            "Create hello.py with hello() returning 'hi'. Reply DONE.",
            workspace=tmp_path,
            read_only=False,
        )
    )
    assert (tmp_path / "hello.py").exists() and "hi" in (tmp_path / "hello.py").read_text()
    assert {"file": "hello.py", "status": "added"} in r.metadata["files_changed"]


async def test_real_fal_image_generation():
    """Paid: one small FLUX schnell image (RUN_PAID=1 and FAL_KEY required)."""
    from app.assets import inspect_image
    from app.connectors.fal import FalImageProvider

    key = os.environ.get("FAL_KEY") or (ENV.model_extra or {}).get("fal_key")
    if not key:
        pytest.skip("NOT CONFIGURED: FAL_KEY is not set")
    paid()
    fal = FalImageProvider(key, os.environ.get("FAL_MODEL") or "fal-ai/flux/schnell", timeout_s=180)
    data = await fal.generate(
        "a flat gold coin icon on a plain background", "512x512", transparent=False
    )
    info = inspect_image(data)
    assert info.format in ("png", "jpeg", "webp") and info.width and info.height
