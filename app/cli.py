"""`agw` command line (installed as a uv project script).

uv run agw serve        start the gateway (REST + Telegram + /mcp)
uv run agw opencode     start `opencode serve` with OPENCODE_PASSWORD / OPENCODE_URL from .env
uv run agw dev          OpenCode server (if OPENCODE_ENABLED) + gateway together
uv run agw validate     validate config/ (agents, capabilities, workflows, ...)
uv run agw doctor       show what is configured and reachable
uv run agw mcp-setup    register the gateway MCP server in Claude Code (user scope)
uv run agw init-env     create missing local secrets (API_TOKEN, OPENCODE_PASSWORD) in .env
uv run agw repo-setup <repo...>   standard CLAUDE.md block + .mcp.json + local settings per repo
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

ENV_FILE = Path(".env")
MCP_NAME = "agent-gateway"


def _settings():
    from app.config import Settings

    return Settings()


def _gateway_url(host: str, port: int) -> str:
    wildcard = host in ("0.0.0.0", "::")  # noqa: S104 - only mapped to loopback for client URLs
    return f"http://{'127.0.0.1' if wildcard else host}:{port}"


def _clean_env() -> dict:
    # `claude` refuses to run nested inside a Claude Code session.
    return {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}


# ---------------------------------------------------------------- .env


def set_env_values(values: dict[str, str], path: Path = ENV_FILE) -> list[str]:
    """Set keys in .env only when missing or blank. Never overwrites existing values."""
    lines = path.read_text().splitlines() if path.exists() else []
    present = {}
    for i, line in enumerate(lines):
        if "=" in line and not line.lstrip().startswith("#"):
            present[line.split("=", 1)[0].strip()] = i
    changed = []
    for key, value in values.items():
        if key in present:
            idx = present[key]
            if lines[idx].split("=", 1)[1].strip():
                continue
            lines[idx] = f"{key}={value}"
        else:
            lines.append(f"{key}={value}")
        changed.append(key)
    if changed:
        path.write_text("\n".join(lines) + "\n")
        path.chmod(0o600)
    return changed


def cmd_init_env(args) -> int:
    if not ENV_FILE.exists() and Path(".env.example").exists():
        shutil.copy(".env.example", ENV_FILE)
        print("created .env from .env.example")
    changed = set_env_values(
        {
            "API_TOKEN": secrets.token_urlsafe(32),
            "OPENCODE_PASSWORD": secrets.token_urlsafe(24),
            "TELEGRAM_WEBHOOK_SECRET": secrets.token_urlsafe(32),  # only used in webhook mode
        }
    )
    print(f"generated: {', '.join(changed)}" if changed else "local secrets already set")
    return 0


# ---------------------------------------------------------------- run


def cmd_serve(args) -> int:
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
        timeout_graceful_shutdown=3,  # MCP clients keep streams open; do not wait for them forever
    )
    return 0


def _opencode_cmd(s) -> list[str]:
    exe = shutil.which("opencode")
    if exe is None:
        raise SystemExit("opencode is not installed (brew install opencode)")
    u = urlparse(s.opencode_url)
    return [exe, "serve", "--hostname", u.hostname or "127.0.0.1", "--port", str(u.port or 4096)]


def _opencode_env(s) -> dict:
    if not s.opencode_password:
        raise SystemExit("OPENCODE_PASSWORD is empty: run `uv run agw init-env` first")
    return {
        **os.environ,
        "OPENCODE_SERVER_PASSWORD": s.opencode_password.get_secret_value(),
        "OPENCODE_SERVER_USERNAME": s.opencode_username,
    }


def cmd_opencode(args) -> int:
    s = _settings()
    cwd = s.workspace_root or Path.cwd()
    return subprocess.call(_opencode_cmd(s), env=_opencode_env(s), cwd=cwd)


_TUNNEL_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def start_tunnel(port: int, timeout_s: float = 30) -> tuple[subprocess.Popen, str]:
    """Cloudflare quick tunnel (no account) to the local gateway; returns (process, public URL)."""
    exe = shutil.which("cloudflared")
    if exe is None:
        raise SystemExit(
            "TELEGRAM_MODE=webhook needs a public URL: set TELEGRAM_WEBHOOK_URL, "
            "or install cloudflared (brew install cloudflared) for an automatic tunnel"
        )
    proc = subprocess.Popen(
        [exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    found: list[str] = []
    ready = threading.Event()

    def drain():  # keep reading so the pipe never fills; remember the URL when it appears
        for line in proc.stderr:
            if not found and (m := _TUNNEL_URL.search(line)):
                found.append(m.group(0))
                ready.set()

    threading.Thread(target=drain, daemon=True).start()
    if not ready.wait(timeout_s):
        proc.kill()
        raise SystemExit("cloudflared did not report a tunnel URL in time")
    return proc, found[0]


def _port_open(host: str, port: int) -> bool:
    import socket

    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def cmd_dev(args) -> int:
    s = _settings()
    if _port_open(args.host, args.port):
        print(
            f"gateway already running on {_gateway_url(args.host, args.port)} (port in use); not starting another"
        )
        return 0
    procs = []
    env = dict(os.environ)
    if s.telegram_mode == "webhook" and not s.telegram_webhook_url:
        tunnel, url = start_tunnel(args.port)
        procs.append(tunnel)
        env["TELEGRAM_WEBHOOK_URL"] = url
        print(f"telegram webhook via tunnel {url}")
    oc = urlparse(s.opencode_url)
    if s.opencode_enabled and _port_open(oc.hostname or "127.0.0.1", oc.port or 4096):
        print(f"opencode already running on {s.opencode_url}; reusing it")
    elif s.opencode_enabled:
        procs.append(
            subprocess.Popen(
                _opencode_cmd(s), env=_opencode_env(s), cwd=s.workspace_root or Path.cwd()
            )
        )
        print(f"opencode serve started on {s.opencode_url}")
        time.sleep(2)
    gw = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            args.host,
            "--port",
            str(args.port),
            "--timeout-graceful-shutdown",
            "3",
        ],
        env=env,
    )
    procs.append(gw)
    try:
        return gw.wait()
    except KeyboardInterrupt:
        return 0
    finally:
        for p in procs:
            if p.poll() is None:
                p.send_signal(signal.SIGINT)
        for p in procs:
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()


# ---------------------------------------------------------------- checks


def cmd_validate(args) -> int:
    from app.gateway_config import main

    return main([args.config_dir or str(_settings().config_dir or "config")])


def cmd_doctor(args) -> int:
    import httpx

    s = _settings()
    ok = lambda b: "OK " if b else "-- "  # noqa: E731
    print(f"{ok(shutil.which('uv'))}uv")
    print(f"{ok(s.api_token)}API_TOKEN (needed for REST and /mcp)")
    print(f"{ok(s.config_dir and (s.config_dir / 'app.yaml').exists())}CONFIG_DIR={s.config_dir}")
    print(
        f"{ok(s.workspace_root)}WORKSPACE_ROOT={s.workspace_root or '(not set: code tasks disabled)'}"
    )
    print(
        f"{ok(s.telegram_bot_token)}Telegram bot token"
        + (
            ""
            if s.telegram_control_chat_id not in (None, -1000000000000)
            else "  (placeholder chat ids)"
        )
    )
    hook = s.telegram_mode == "webhook"
    print(
        f"{ok(not hook or s.telegram_webhook_url or shutil.which('cloudflared'))}"
        f"TELEGRAM_MODE={s.telegram_mode}"
        + (f"  url={s.telegram_webhook_url or '(cloudflared tunnel at start)'}" if hook else "")
    )
    print(f"{ok(shutil.which('opencode'))}opencode binary  OPENCODE_ENABLED={s.opencode_enabled}")
    if s.opencode_enabled and s.opencode_password:
        try:
            r = httpx.get(
                s.opencode_url.rstrip("/") + "/api/info",
                timeout=3,
                auth=(s.opencode_username, s.opencode_password.get_secret_value()),
            )
            print(
                f"{ok(r.status_code == 200)}opencode server {s.opencode_url} -> HTTP {r.status_code}"
            )
        except httpx.HTTPError:
            print(f"-- opencode server {s.opencode_url} not reachable (run `uv run agw opencode`)")
    url = _gateway_url(args.host, args.port)
    try:
        r = httpx.get(f"{url}/health", timeout=3)
        print(f"{ok(r.status_code == 200)}gateway {url}/health -> {r.status_code}")
    except httpx.HTTPError:
        print(f"-- gateway {url} not running (run `uv run agw serve`)")
    claude = shutil.which("claude")
    if claude:
        out = subprocess.run(
            [claude, "mcp", "get", MCP_NAME], capture_output=True, text=True, env=_clean_env()
        )
        print(
            f"{ok(out.returncode == 0)}Claude Code MCP '{MCP_NAME}' "
            + ("registered" if out.returncode == 0 else "not registered")
        )
    return 0


def cmd_mcp_setup(args) -> int:
    claude = shutil.which("claude")
    if claude is None:
        raise SystemExit("claude CLI not found")
    s = _settings()
    if s.mcp_require_token and not s.api_token:
        raise SystemExit("API_TOKEN is empty: run `uv run agw init-env` first")
    url = f"{_gateway_url(args.host, args.port)}/mcp"
    env = _clean_env()
    subprocess.run(
        [claude, "mcp", "remove", MCP_NAME, "--scope", args.scope], capture_output=True, env=env
    )
    cmd = [
        claude,
        "mcp",
        "add",
        "--transport",
        "http",
        "--scope",
        args.scope,
        MCP_NAME,
        url,
    ]
    if s.mcp_require_token:
        cmd += ["--header", f"Authorization: Bearer {s.api_token.get_secret_value()}"]
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if r.returncode != 0:
        print(r.stderr or r.stdout)
        return r.returncode
    print(f"registered MCP server '{MCP_NAME}' -> {url} (scope: {args.scope})")
    print("check with: claude mcp list   (the gateway must be running: uv run agw serve)")
    return 0


def cmd_repo_setup(args) -> int:
    from app.repo_setup import setup_repo

    url = f"{_gateway_url(args.host, args.port)}/mcp"
    for raw in args.repos:
        repo = Path(raw).expanduser().resolve()
        if not (repo / ".git").exists():
            print(f"-- {repo}: not a git repository, skipped")
            continue
        r = setup_repo(
            repo, url, args.project if len(args.repos) == 1 else None, _settings().mcp_require_token
        )
        ignored = (
            subprocess.run(
                ["git", "-C", str(repo), "check-ignore", "-q", ".claude/settings.local.json"]
            ).returncode
            == 0
        )
        print(
            f"OK {repo.name}: project={r['project']!r} CLAUDE.md {r['claude_md']}, .mcp.json, "
            f"settings.local.json{'' if ignored else ' (WARNING: not git-ignored)'}"
            + (", deploy section" if r["deploy_section"] else "")
        )
    if _settings().mcp_require_token:
        print("Sessions need AGW_API_TOKEN in their environment (see ~/.zshrc) and a restart.")
    else:
        print("MCP_REQUIRE_TOKEN=false: local sessions connect without a token (restart them).")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="agw", description="Agent Gateway commands")
    sub = p.add_subparsers(dest="cmd", required=True)

    def hp(sp):
        sp.add_argument("--host", default="127.0.0.1")
        sp.add_argument("--port", type=int, default=8000)

    sp = sub.add_parser("serve", help="start the gateway")
    hp(sp)
    sp.add_argument("--reload", action="store_true")
    sp.set_defaults(fn=cmd_serve)
    sub.add_parser("opencode", help="start opencode serve from .env settings").set_defaults(
        fn=cmd_opencode
    )
    sp = sub.add_parser("dev", help="opencode (if enabled) + gateway")
    hp(sp)
    sp.set_defaults(fn=cmd_dev)
    sp = sub.add_parser("validate", help="validate config/")
    sp.add_argument("config_dir", nargs="?")
    sp.set_defaults(fn=cmd_validate)
    sp = sub.add_parser("doctor", help="show configuration and reachability")
    hp(sp)
    sp.set_defaults(fn=cmd_doctor)
    sp = sub.add_parser("mcp-setup", help="register the gateway MCP server in Claude Code")
    hp(sp)
    sp.add_argument("--scope", choices=["user", "project", "local"], default="user")
    sp.set_defaults(fn=cmd_mcp_setup)
    sp = sub.add_parser("repo-setup", help="connect repos' Claude Code sessions to the gateway")
    hp(sp)
    sp.add_argument("repos", nargs="+", help="repository paths")
    sp.add_argument("--project", help="project name (single repo only; default: from README)")
    sp.set_defaults(fn=cmd_repo_setup)
    sub.add_parser("init-env", help="generate missing local secrets in .env").set_defaults(
        fn=cmd_init_env
    )
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
