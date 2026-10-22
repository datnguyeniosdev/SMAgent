"""Health and setup status, shared by `agw doctor` and Telegram /setup."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

import httpx


@dataclass
class Check:
    ok: bool
    label: str
    detail: str = ""


def _claude_mcp(name: str) -> bool | None:
    """Is this MCP server registered for Claude Code (any scope)? None when claude is not installed."""
    claude = shutil.which("claude")
    if claude is None:
        return None
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    r = subprocess.run(
        [claude, "mcp", "get", name], capture_output=True, text=True, env=env, timeout=30
    )
    return r.returncode == 0


def collect(
    s, gateway_url: str = "http://127.0.0.1:8000", check_gateway: bool = True
) -> list[Check]:
    from app import global_setup
    from app.config import env_file, env_name
    from app.projects import projects_root

    out = [
        Check(True, "environment", f"{env_name()} ({env_file()}, data in {s.data_dir})"),
        Check(
            bool(s.config_dir and (s.config_dir / "app.yaml").exists()), "config", str(s.config_dir)
        ),
        Check(
            bool(s.telegram_bot_token) and s.telegram_control_chat_id not in (None, -1000000000000),
            "Telegram",
            f"{s.telegram_mode}, control group {s.telegram_control_chat_id}",
        ),  # fmt: skip
        Check(
            bool(s.api_token) or not s.mcp_require_token,
            "MCP auth",
            "token required" if s.mcp_require_token else "local requests without a token",
        ),  # fmt: skip
    ]
    if check_gateway:
        try:
            r = httpx.get(f"{gateway_url}/health", timeout=3)
            out.append(Check(r.status_code == 200, "gateway", f"{gateway_url} -> {r.status_code}"))
        except httpx.HTTPError:
            out.append(Check(False, "gateway", f"{gateway_url} not running (uv run agw dev)"))
    if s.opencode_enabled and s.opencode_password:
        try:
            r = httpx.get(s.opencode_url.rstrip("/") + "/api/info", timeout=3,
                          auth=(s.opencode_username, s.opencode_password.get_secret_value()))  # fmt: skip
            out.append(
                Check(
                    r.status_code == 200, "OpenCode server", f"{s.opencode_url} -> {r.status_code}"
                )
            )
        except httpx.HTTPError:
            out.append(Check(False, "OpenCode server", f"{s.opencode_url} not reachable"))
    else:
        out.append(Check(bool(shutil.which("opencode")), "OpenCode", "installed, server disabled"))
    fal_key = os.environ.get("FAL_KEY")
    out.append(
        Check(
            bool(fal_key),
            "fal (art)",
            os.environ.get("FAL_MODEL", "") if fal_key else "FAL_KEY not set",
        )
    )
    root = projects_root()
    repos = [d for d in root.iterdir() if (d / ".git").exists()] if root.is_dir() else []
    out.append(Check(bool(repos), "projects", f"{len(repos)} git repos in {root}"))
    done = global_setup.installed()
    out.append(
        Check(done, "global setup", "skill + hook + permissions" if done else "not installed")
    )
    for name in ("agent-gateway", "agent-gateway-channel"):
        reg = _claude_mcp(name)
        state = (
            "registered" if reg else ("claude CLI not found" if reg is None else "not registered")
        )
        out.append(Check(bool(reg), f"Claude Code MCP {name}", state))
    from app import agents_setup

    for agent in agents_setup.detected():
        on = agents_setup.configured(agent)
        out.append(Check(on, f"agent {agent}", "connected" if on else "found, not connected"))
    return out


def as_text(checks: list[Check]) -> str:
    return "\n".join(f"{'OK ' if c.ok else '-- '}{c.label}: {c.detail}" for c in checks)
