"""`agw` command line (installed as a uv project script).

uv run agw setup                 set up this machine: local secrets + global setup for every repo
uv run agw setup --repo PATH...  per-repo files instead (CLAUDE.md, .mcp.json; --template for templates)
uv run agw setup --remove        undo the global setup
uv run agw dev                   OpenCode server (if OPENCODE_ENABLED) + gateway together
uv run agw doctor                show what is configured and reachable
uv run agw open <name>           open a project folder in a Claude session (Terminal window)
agw session                      (in a repo) Claude Code that takes work from Telegram
uv run agw serve | opencode | validate   start one part only / check config/

Older names still work: init-env, mcp-setup, global-setup, repo-setup. `channel` and
`hook-session-start` are run by Claude Code itself.
"""

from __future__ import annotations

import argparse
import json
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
    from app.config import load_settings

    return load_settings()


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
    from dotenv import load_dotenv

    from app.config import env_file
    from app.doctor import as_text, collect

    load_dotenv(env_file(), override=False)  # provider keys (FAL_KEY, ...) live in .env
    print(as_text(collect(_settings(), _gateway_url(args.host, args.port))))
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
            repo,
            url,
            args.project if len(args.repos) == 1 else None,
            _settings().mcp_require_token,
            template=args.template,
        )
        if args.template:
            print(
                f"OK {repo.name}: template, CLAUDE.md {r['claude_md']} (project {r['project']!r}; "
                "no .mcp.json / local settings: games made from it run `agw setup --repo` themselves)"
            )
            continue
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


CHANNEL_NAME = "agent-gateway-channel"
CHANNEL_MIN_VERSION = (2, 1, 80)  # Claude Code channels (research preview)


def cmd_channel(args) -> int:
    """stdio MCP shim that Claude Code spawns (from .mcp.json); see app/channel_shim.py."""
    from app.channel_shim import main as shim_main

    return shim_main(args.project)


def _claude_version(claude: str) -> tuple[int, ...]:
    out = subprocess.run(
        [claude, "--version"], capture_output=True, text=True, env=_clean_env()
    ).stdout
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
    return tuple(int(x) for x in m.groups()) if m else ()


def cmd_session(args) -> int:
    """Open an interactive Claude Code session in this repo that receives pushed work (channel)."""
    claude = shutil.which("claude")
    if claude is None:
        raise SystemExit("claude CLI not found")
    version = _claude_version(claude)
    if version < CHANNEL_MIN_VERSION:
        have = ".".join(map(str, version)) or "unknown"
        raise SystemExit(
            f"Claude Code {have} has no channels; need >= 2.1.80: "
            "npm install -g @anthropic-ai/claude-code@latest"
        )
    from app.global_setup import installed as global_installed
    from app.projects import project_for

    mcp = json.loads(Path(".mcp.json").read_text()) if Path(".mcp.json").exists() else {}
    local = mcp.get("mcpServers", {}).get(CHANNEL_NAME)
    if local is None and not global_installed():
        raise SystemExit(
            "no channel server here: run `uv run agw setup` (all repos) or `agw setup --repo <repo>`"
        )
    env = {**_clean_env(), "AGW_CHANNEL": "1"}  # the shim only listens in sessions started here
    if (
        not args.claude_args and not args.no_loop
    ):  # fresh session: also pull (resumed ones restore it)
        from app.repo_setup import loop_prompt

        project = (local or {}).get("args", [None])[-1] or project_for(Path.cwd())
        if project:
            args.claude_args = [f"/loop 1m {loop_prompt(project)}"]
    # the channel option takes several values, so it goes last: a prompt after it would be read as a channel
    argv = [
        claude,
        *args.claude_args,
        "--dangerously-load-development-channels",
        f"server:{CHANNEL_NAME}",
    ]
    os.execvpe(claude, argv, env)  # noqa: S606 - no shell on purpose; the terminal becomes the session


def cmd_open(args) -> int:
    """Find a project folder under PROJECTS_ROOT and open a Claude session there (Terminal window)."""
    from app.launcher import open_project

    s = _settings()
    root = s.projects_root or Path(__file__).resolve().parents[2]
    status, hits = open_project(root, args.query, new=True, token=s.mcp_require_token)
    if status == "none":
        print(f"no project folder matches {args.query!r} in {root}")
        return 1
    if status == "many":
        print("several match:\n" + "\n".join(f"  {d.name}" for d in hits))
        return 1
    extra = " (connected it to the gateway first)" if status == "opened_connected" else ""
    print(f"opening {hits[0]} in a Terminal window{extra}")
    return 0


def cmd_global_setup(args) -> int:
    """Connect every git repo under PROJECTS_ROOT via Claude Code's user-level config (see app/global_setup.py)."""
    from app import global_setup

    if args.remove:
        steps = global_setup.remove()
    else:
        settings = _settings()
        token = (
            settings.api_token.get_secret_value()
            if settings.mcp_require_token and settings.api_token
            else None
        )
        steps = global_setup.install(
            gateway_url=f"{_gateway_url(args.host, args.port)}/mcp", token=token
        )
    for line in steps:
        print(f"- {line}")
    print("Restart open Claude Code sessions (/exit, then claude --continue) so they load it.")
    return 0


def cmd_hook_session_start(args) -> int:
    """SessionStart hook (from .claude/settings.local.json): its stdout becomes Claude's context."""
    from app.projects import project_for
    from app.repo_setup import session_start_hint

    project = args.project or project_for(Path.cwd())
    if project is None:  # global hook in a folder that is not a connected repo: stay silent
        return 0
    print(session_start_hint(project))
    print(
        f"Use project '{project}' for every agent-gateway call; the agent-gateway skill has the details "
        "(short reports, Telegram requests, reviewed art, refreshing the session after setup)."
    )
    return 0


ENV_TEMPLATE = """# agw environment "{env}": runs beside prod on port {port}, own data in var/{env}.
# Agent configs (Claude Code, OpenCode, Codex, ...) stay on prod; nothing here changes them.
DATA_DIR=var/{env}
MCP_REQUIRE_TOKEN=false
OPENCODE_ENABLED=false
# Telegram: a separate bot (@BotFather) and test group. Prod's bot token is refused here.
TELEGRAM_BOT_TOKEN=
TELEGRAM_CONTROL_CHAT_ID=
TELEGRAM_REPORT_CHANNEL_ID=
TELEGRAM_ALLOWED_USER_IDS={users}
# Paid providers stay off until you add their keys here (FAL_KEY=..., FAL_MODEL=...).
"""


def cmd_env_setup(args) -> int:
    """dev/uat: a new .env.<env> beside prod's. Agent configs and prod's files are not touched."""
    from dotenv import dotenv_values

    from app.config import env_file, env_name

    env, path = env_name(), Path(env_file())
    if args.agents or args.remove:
        print(f"{env}: agent configs are changed from prod only (run without --env)")
        return 1
    if args.repo:  # a test repo connected to this environment (its .mcp.json only)
        args.repos = args.repo
        return cmd_repo_setup(args)
    if not path.exists():
        users = dotenv_values(ENV_FILE).get("TELEGRAM_ALLOWED_USER_IDS") or ""
        path.write_text(ENV_TEMPLATE.format(env=env, port=args.port, users=users))
        print(f"created {path}")
    set_env_values({"API_TOKEN": secrets.token_urlsafe(32),
                    "OPENCODE_PASSWORD": secrets.token_urlsafe(24),
                    "TELEGRAM_WEBHOOK_SECRET": secrets.token_urlsafe(32)}, path)  # fmt: skip
    path.chmod(0o600)
    print(f"{env}: start it with `uv run agw --env {env} dev` ({_gateway_url(args.host, args.port)}); "
          f"prod and agent configs unchanged. Telegram stays off until {path} has its own bot.")  # fmt: skip
    return 0


def cmd_setup(args) -> int:
    """One setup command: secrets + global setup (default), --repo for per-repo files, --agents for
    other coding agents, --remove to undo. With --env dev|uat: that environment only."""
    from app.config import env_name

    if env_name() != "prod":
        return cmd_env_setup(args)
    if args.agents:
        from app import agents_setup

        url = f"{_gateway_url(args.host, args.port)}/mcp"
        for name in agents_setup.resolve(args.agents):
            if name == "claude":
                cmd_global_setup(args)
            elif args.print_rules:
                print(agents_setup.rules_text(name))
            else:
                print(agents_setup.remove(name) if args.remove else agents_setup.install(name, url))
        return 0
    if args.remove:
        args.remove = True
        return cmd_global_setup(args)
    if args.repo:
        args.repos = args.repo
        return cmd_repo_setup(args)
    cmd_init_env(args)
    args.remove = False
    rc = cmd_global_setup(args)
    from app import agents_setup

    others = [a for a in agents_setup.detected() if not agents_setup.configured(a)]
    if others:
        print(
            f"Other agents found: {', '.join(others)}. Connect them with: uv run agw setup --agents detected"
        )
    return rc


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="agw",
        description="Agent Gateway. Start with `uv run agw setup`, then `uv run agw dev`.",
    )
    p.add_argument("--env", choices=["prod", "dev", "uat"],
                   help="environment (default $AGW_ENV or prod): own .env.<env>, port and data")  # fmt: skip
    sub = p.add_subparsers(dest="cmd", required=True, metavar="<command>")
    # aliases and internal commands get no help text: they work but are not listed

    def hp(sp):
        sp.add_argument("--host", default="127.0.0.1")
        sp.add_argument(
            "--port", type=int, default=None, help="default: 8000 prod, 8001 dev, 8002 uat"
        )

    def repo_args(sp, required: bool):
        sp.add_argument("--project", help="project name (single repo only; default: from README)")
        sp.add_argument("--template", action="store_true",
                        help="template repo: CLAUDE.md block only, placeholder project, no machine files")  # fmt: skip
        if required:
            sp.add_argument("repos", nargs="+", help="repository paths")

    # ---- everyday commands ----
    sp = sub.add_parser(
        "setup", help="set up this machine: secrets + global setup (or --repo / --remove)"
    )
    hp(sp)
    sp.add_argument(
        "--repo",
        nargs="+",
        metavar="PATH",
        help="write per-repo files instead (CLAUDE.md, .mcp.json)",
    )
    repo_args(sp, required=False)
    sp.add_argument("--remove", action="store_true", help="undo the global setup (or of --agents)")
    sp.add_argument(
        "--agents", metavar="LIST",
        help="connect other agents: opencode,codex,antigravity,cursor | detected | all",
    )  # fmt: skip
    sp.add_argument(
        "--print-rules", action="store_true", help="with --agents cursor: print rules to paste"
    )
    sp.set_defaults(fn=cmd_setup)
    sp = sub.add_parser("dev", help="start OpenCode (if enabled) + the gateway")
    hp(sp)
    sp.set_defaults(fn=cmd_dev)
    sp = sub.add_parser("doctor", help="show what is configured and reachable")
    hp(sp)
    sp.set_defaults(fn=cmd_doctor)
    sp = sub.add_parser("open", help="open a project folder (fuzzy name) in a Claude session")
    sp.add_argument("query")
    sp.set_defaults(fn=cmd_open)
    sp = sub.add_parser(
        "session", help="(in a repo) open Claude Code that takes work from Telegram"
    )
    sp.add_argument("--no-loop", action="store_true", help="do not start the pull loop")
    sp.add_argument("claude_args", nargs=argparse.REMAINDER, help="extra arguments for claude")
    sp.set_defaults(fn=cmd_session)
    sp = sub.add_parser("serve", help="start only the gateway")
    hp(sp)
    sp.add_argument("--reload", action="store_true")
    sp.set_defaults(fn=cmd_serve)
    sub.add_parser("opencode", help="start only `opencode serve` (from .env)").set_defaults(
        fn=cmd_opencode
    )
    sp = sub.add_parser("validate", help="validate config/")
    sp.add_argument("config_dir", nargs="?")
    sp.set_defaults(fn=cmd_validate)

    # ---- aliases of `setup` (older names) ----
    sub.add_parser("init-env").set_defaults(fn=cmd_init_env)
    sp = sub.add_parser("mcp-setup")
    hp(sp)
    sp.add_argument("--scope", choices=["user", "project", "local"], default="user")
    sp.set_defaults(fn=cmd_mcp_setup)
    sp = sub.add_parser("global-setup")
    hp(sp)
    sp.add_argument("--remove", action="store_true")
    sp.set_defaults(fn=cmd_global_setup)
    sp = sub.add_parser("repo-setup")
    hp(sp)
    repo_args(sp, required=True)
    sp.set_defaults(fn=cmd_repo_setup)

    # ---- internal (run by Claude Code, not by people) ----
    sp = sub.add_parser("channel")
    sp.add_argument("--project")
    sp.set_defaults(fn=cmd_channel)
    sp = sub.add_parser("hook-session-start")
    sp.add_argument("--project")
    sp.set_defaults(fn=cmd_hook_session_start)

    args, extra = p.parse_known_args(argv)
    if args.cmd == "session":  # everything unknown goes to claude (e.g. --continue, --resume <id>)
        args.claude_args = [*extra, *args.claude_args]
    elif extra:
        p.error(f"unrecognized arguments: {' '.join(extra)}")
    from app.config import ENV_PORTS, env_name

    if args.env:
        os.environ["AGW_ENV"] = args.env  # also for the processes started from here
    if getattr(args, "port", 0) is None:
        args.port = ENV_PORTS[env_name()]
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
