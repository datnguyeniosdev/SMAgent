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
    from app.keys import new_secret

    if not ENV_FILE.exists() and Path(".env.example").exists():
        shutil.copy(".env.example", ENV_FILE)
        print("created .env from .env.example")
    changed = set_env_values(
        {
            "API_TOKEN": secrets.token_urlsafe(32),
            "OPENCODE_PASSWORD": secrets.token_urlsafe(24),
            "TELEGRAM_WEBHOOK_SECRET": secrets.token_urlsafe(32),  # only used in webhook mode
            "AGW_SECRET_KEY": new_secret(),  # encrypts the keys saved with Telegram /keys
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


def stop_mark() -> Path:
    """Written by `agw stop`: the gateway is down on purpose, so background starts (the sessions'
    10-minute health check) leave it down. A start from a terminal, or --force, removes it."""
    return Path(_settings().data_dir) / "agw.stopped"


def _listener(port: int) -> int | None:
    r = subprocess.run(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"], capture_output=True, text=True
    )
    pids = [int(x) for x in r.stdout.split()]
    return pids[0] if pids else None


def _cwd(pid: int) -> str:
    r = subprocess.run(
        ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], capture_output=True, text=True
    )
    return next((ln[1:] for ln in r.stdout.splitlines() if ln.startswith("n")), "")


def is_agw_dev(command: str) -> bool:
    """`.../agw dev ...`, not any command that merely contains "agw" (e.g. a path under .agw-prod)."""
    return re.search(r"(^|[/\s])agw dev\b", command) is not None


def cmd_stop(args) -> int:
    """Stop this folder's gateway on this environment's port and keep background starts off."""
    mark = stop_mark()
    mark.parent.mkdir(parents=True, exist_ok=True)
    mark.write_text(f"stopped with `agw stop` at {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    pid = _listener(args.port)
    if pid is None:
        print(f"nothing listens on port {args.port}; background starts stay off until `agw dev`")
        return 0
    if Path(_cwd(pid)).resolve() != Path.cwd().resolve():
        print(
            f"port {args.port} belongs to another folder ({_cwd(pid) or 'unknown'}); not stopping it"
        )
        return 1
    parent = int(
        subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True).stdout
        or 0
    )
    os.kill(pid, signal.SIGTERM)  # uvicorn shuts down; its `agw dev` parent then stops OpenCode
    for _ in range(20):
        time.sleep(0.5)
        if _listener(args.port) is None:
            break
    else:
        os.kill(pid, signal.SIGKILL)
        time.sleep(1)
    try:
        cmd = subprocess.run(
            ["ps", "-o", "command=", "-p", str(parent)], capture_output=True, text=True
        ).stdout
        if parent > 1 and is_agw_dev(cmd):  # only the `agw dev` that started it
            os.kill(parent, signal.SIGTERM)
    except ProcessLookupError:
        pass
    print(
        f"gateway on port {args.port} stopped; background starts stay off until you run `agw dev` in a terminal"
    )
    return 0


def cmd_dev(args) -> int:
    s = _settings()
    mark = stop_mark()
    if mark.exists():
        if not (args.force or sys.stdin.isatty()):  # a health check starting it in the background
            print(
                f"not starting: stopped on purpose ({mark}); run `agw dev` in a terminal to start it"
            )
            return 0
        mark.unlink()
    if _port_open(args.host, args.port):
        print(
            f"gateway already running on {_gateway_url(args.host, args.port)} (port in use); not starting another. "
            "To run it here instead: `agw stop`, then `agw dev`"
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
    from app.projects import is_project
    from app.repo_setup import setup_repo

    url = f"{_gateway_url(args.host, args.port)}/mcp"
    for raw in args.repos:
        repo = Path(raw).expanduser().resolve()
        if not is_project(repo):
            print(f"-- {repo}: not a project (no .git, AGENTS.md or CLAUDE.md), skipped")
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
        kind = _project_kind(r["project"], getattr(args, "kind", None))
        print(f"   {r['project']!r} is a {kind} project")
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


def _project_kind(project: str, kind: str | None) -> str:
    """The project's type in the gateway registry: set when given, else kept (new projects are
    products). Written to the gateway's own store, which it reads per request."""
    from app.project_registry import ProjectRegistry
    from app.store import Store

    s = _settings()
    store = Store(s.state_db if s.state_db == ":memory:" else s.data_dir / s.state_db)
    try:
        reg = ProjectRegistry(store)
        p = reg.ensure(project)
        return (reg.set_kind(p.project_id, kind) if kind else p).kind
    finally:
        store.close()


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


# One-shot runs of other agents' CLIs (prompt last). ponytail: codex/gemini flags unverified on this Mac.
AGENT_RUN = {
    "opencode": ["opencode", "run"],
    # workspace-write with approvals reviewed automatically (--full-auto is gone in newer codex)
    "codex": ["codex", "exec", "--approve-for-me"],
    "antigravity": ["gemini", "-p"],
}


def cmd_agent_loop(args) -> int:
    """A terminal that runs another agent once a minute to take work from Telegram (it has no loop of its own)."""
    from app.codex_relay import codex_cli
    from app.projects import project_for
    from app.repo_setup import loop_prompt

    base = AGENT_RUN[args.agent]
    exe = codex_cli() if base[0] == "codex" else shutil.which(base[0])
    if exe is None:
        raise SystemExit(f"{base[0]} not found on PATH")
    base = [exe, *base[1:]]
    import urllib.parse
    import urllib.request

    from app.config import ENV_PORTS, env_name

    mcp = json.loads(Path(".mcp.json").read_text()) if Path(".mcp.json").exists() else {}
    # a child worktree (dispatch) is outside the projects root: its .mcp.json names the project
    local = mcp.get("mcpServers", {}).get(CHANNEL_NAME) or {}
    project = project_for(Path.cwd()) or local.get("args", [None])[-1]
    prompt = loop_prompt(project, "cli", args.agent)
    query = urllib.parse.urlencode(
        {"project": project, "agent": args.agent, "cwd": str(Path.cwd())}
    )
    url = f"http://127.0.0.1:{ENV_PORTS[env_name()]}/api/v1/channel/peek?{query}"
    print(f"{args.agent} takes work for {project!r}: checked every minute, run only when there is work. "
          "Ctrl-C stops it.")  # fmt: skip
    try:
        while True:
            try:
                with urllib.request.urlopen(url, timeout=10) as r:  # noqa: S310 - fixed local URL
                    state = json.loads(r.read())
            except OSError:
                state = {"work": False}  # gateway down: try again next minute
            if state.get("closing"):
                print("closed by the operator (/close)")
                return 0
            if state.get("work"):
                subprocess.run([*base, *args.claude_args, prompt], env=_clean_env(), check=False)  # noqa: S603
            time.sleep(60)
    except KeyboardInterrupt:
        return 0


def cmd_session(args) -> int:
    """Open an interactive Claude Code session in this repo that receives pushed work (channel)."""
    if args.agent != "claude_code":
        return cmd_agent_loop(args)
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

    mcp = json.loads(Path(".mcp.json").read_text()) if Path(".mcp.json").exists() else {}
    local = mcp.get("mcpServers", {}).get(CHANNEL_NAME)
    if local is None and not global_installed():
        raise SystemExit(
            "no channel server here: run `uv run agw setup` (all repos) or `agw setup --repo <repo>`"
        )
    # no /loop: push delivers here, and the SessionStart hook starts the waiter and its safety cron
    env = {**_clean_env(), "AGW_CHANNEL": "1"}  # the shim only listens in sessions started here
    # A first prompt: Claude Code may block channel pushes (then nothing would start this session),
    # so the session fetches its work itself and starts its waiter (SessionStart hook) on its first turn.
    from app.projects import project_for
    from app.repo_setup import loop_prompt

    project = project_for(Path.cwd())
    first = []
    if project and not args.claude_args:  # (resuming with --continue/--resume: it already knows)
        pull = loop_prompt(project, "cli").replace("this folder", repr(str(Path.cwd())))
        first = [f"agent-gateway opened this session for work. {pull}"]
    # the channel option takes several values, so it goes last: a prompt after it would be read as a channel
    argv = [
        claude,
        *(["--model", args.model] if args.model else []),
        *args.claude_args,
        *first,
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
    from app import global_setup
    from app.projects import project_for
    from app.repo_setup import session_start_hint

    if args.project and global_setup.installed():
        return 0  # an older repo hook: the global hook says it once, with the current name
    # the folder's README now (a renamed project takes its new name); a pinned name only outside
    project = project_for(Path.cwd()) or args.project
    if project is None:  # global hook in a folder that is not a connected repo: stay silent
        return 0
    print(session_start_hint(project))
    print(
        f"Use project '{project}' for every agent-gateway call; the agent-gateway skill has the details "
        "(short reports, Telegram requests, reviewed art, refreshing the session after setup)."
    )
    return 0


def cmd_hook_prompt(args) -> int:
    """UserPromptSubmit hook (global): if this session's `agw wait` is gone (the app restarted and
    stopped background tasks), its stdout tells Claude to start it again before the request."""
    from app.config import ENV_PORTS
    from app.projects import client_kind, project_for  # light imports: this runs on every prompt
    from app.waiter import instance, missing, reminder, tell

    cwd = str(Path.cwd())
    project = project_for(Path(cwd))
    if project:  # the model runs again: work it left stalled (rate limit) is put to the operator
        tell(
            "/api/v1/channel/active",
            ENV_PORTS["prod"],
            project=project,
            agent="claude_code",
            cwd=cwd,
            client=client_kind(),
            instance=instance(),
        )
    if project and missing(project, cwd):
        print(reminder(project, cwd, client_kind()))
    if project and (own := _own_session(project, cwd, client_kind(), instance())):
        print(f"agent-gateway: this conversation's gateway session_id is '{own}' (its own: it is the "
              "project's 👑 main). Pass exactly that session_id to get_messages, reply_message and every "
              "gateway call, not the one the folder's other conversations share.")  # fmt: skip
    return 0


def _own_session(project: str, cwd: str, client: str, inst: str) -> str | None:
    """This conversation's own gateway session, when it has one (#Main), else None (the folder's)."""
    import urllib.parse
    import urllib.request

    from app.channel import ChannelHub
    from app.config import ENV_PORTS

    if not inst:
        return None
    query = urllib.parse.urlencode({"project": project, "agent": "claude_code", "cwd": cwd,
                                    "client": client, "instance": inst})  # fmt: skip
    try:
        url = f"http://127.0.0.1:{ENV_PORTS['prod']}/api/v1/channel/role?{query}"
        with urllib.request.urlopen(url, timeout=3) as r:  # noqa: S310 - fixed local URL
            role = json.loads(r.read())
    except (OSError, ValueError):
        return None
    sid = role.get("session_id")
    if (
        not role.get("main")
        or not sid
        or sid == ChannelHub.pull_id(project, "claude_code", cwd, client)
    ):
        return None  # not the main (any more): nothing to say
    return sid


ROUTER_ONLY = (
    "Blocked: this conversation is its project's 👑 main session, which only routes. Do not do the "
    "work here: hand it to a child session with the agent-gateway tool dispatch(session_id=<yours>, "
    "lane=<task type>, text=<what the user asked>, brief=<what to do and report>), or to a subagent "
    "for a quick read-only question. Bash here runs only `agw` (waiter, main)."
)


def cmd_hook_tool(args) -> int:
    """PreToolUse hook (global): the project's main conversation only routes, so its edits and
    shell commands (other than agw itself) are denied. Silent and allowed outside a main, or when
    the gateway does not answer: a hook never blocks an agent by failing."""
    import urllib.parse
    import urllib.request

    from app.config import ENV_PORTS
    from app.projects import client_kind, project_for
    from app.waiter import instance

    try:
        event = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    command = (
        (event.get("tool_input") or {}).get("command", "")
        if event.get("tool_name") == "Bash"
        else ""
    )
    if re.search(r"(^|/|\s)agw\s", command):
        return 0
    cwd = event.get("cwd") or str(Path.cwd())
    project = project_for(Path(cwd))
    if not project:
        return 0
    query = urllib.parse.urlencode({"project": project, "agent": "claude_code", "cwd": cwd,
                                    "client": client_kind(), "instance": instance()})  # fmt: skip
    try:
        url = f"http://127.0.0.1:{ENV_PORTS['prod']}/api/v1/channel/role?{query}"
        with urllib.request.urlopen(url, timeout=3) as r:  # noqa: S310 - fixed local URL
            role = json.loads(r.read())
    except (OSError, ValueError):
        return 0
    why = ROUTER_ONLY if role.get("main") else None
    if why is None and role.get("required"):
        required = role["required"]
        if command and any(re.match(rf"\s*(\S*/)?{re.escape(c)}(\s|$)", command)
                           for r in required for c in r.get("commands") or []):  # fmt: skip
            return 0  # the required command itself
        missing = skills_not_used(event.get("transcript_path") or "", required)
        if missing:
            why = (f"Blocked: this request requires {', '.join(missing)}, not used yet. Skills: load each "
                   "with the Skill tool (not listed there: Read .claude/skills/<name>/SKILL.md); commands: "
                   "run them first (e.g. `rea capabilities`, `rea analyze <target>`). Then do the work by "
                   "their workflows. Mandatory: the gateway checks it.")  # fmt: skip
    if why:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                          "permissionDecision": "deny", "permissionDecisionReason": why}}))  # fmt: skip
    return 0


def skills_not_used(transcript: str, required: list[dict]) -> list[str]:
    """Required skills not used since their request reached this conversation: used = a Skill tool
    call for it, or a Read of its SKILL.md, after the last mention of the request's message id in the
    transcript. No transcript (another runtime, unreadable): nothing can be checked, nothing blocked."""
    try:
        lines = Path(transcript).read_text(errors="ignore").splitlines() if transcript else []
    except OSError:
        return []
    if not lines:
        return []
    missing = []
    for req in required:
        start = max((i for i, ln in enumerate(lines) if req["message_id"] in ln), default=-1)
        used, ran = set(), set()
        for ln in lines[start + 1 :]:
            if '"tool_use"' not in ln:
                continue
            try:
                content = (json.loads(ln).get("message") or {}).get("content") or []
            except ValueError:
                continue
            for c in content if isinstance(content, list) else []:
                if not isinstance(c, dict) or c.get("type") != "tool_use":
                    continue
                inp = c.get("input") or {}
                if c.get("name") == "Skill":
                    used.add(str(inp.get("skill", "")).rsplit(":", 1)[-1])
                elif c.get("name") == "Read" and str(inp.get("file_path", "")).endswith(
                    "/SKILL.md"
                ):
                    used.add(Path(str(inp["file_path"])).parent.name)
                elif c.get("name") == "Bash":
                    first = (str(inp.get("command", "")).split() or [""])[0]
                    ran.add(Path(first).name)
        missing += [s for s in req.get("skills") or [] if s not in used and s not in missing]
        missing += [f"the `{c}` command" for c in req.get("commands") or []
                    if c not in ran and f"the `{c}` command" not in missing]  # fmt: skip
    return missing


def cmd_hook_failure(args) -> int:
    """StopFailure hook (global): the turn ended on an API error (rate limit, billing, overload...).
    The session's unanswered Telegram work goes to the operator to move, wait for, or cancel."""
    from app.config import ENV_PORTS
    from app.projects import client_kind, project_for
    from app.waiter import instance, tell

    try:
        event = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        event = {}
    cwd = event.get("cwd") or str(Path.cwd())
    project = project_for(Path(cwd))
    if project:
        reason = " · ".join(str(event[k])[:120] for k in ("error", "error_details") if event.get(k))
        tell("/api/v1/channel/stalled", ENV_PORTS["prod"], project=project, agent="claude_code",
             cwd=cwd, reason=reason or "API error", client=client_kind(), instance=instance())  # fmt: skip
    return 0


def cmd_cursor_hook(args) -> int:
    """Cursor hooks (sessionStart, stop): event JSON on stdin, reply JSON on stdout (app/cursor_hooks.py)."""
    from app.config import ENV_PORTS
    from app.cursor_hooks import hook

    try:
        event = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        print("{}")
        return 0
    print(json.dumps(hook(event, ENV_PORTS["prod"])))
    return 0


def cmd_codex_hook(args) -> int:
    """Codex SessionStart/SessionEnd hook: record this folder's desktop thread for the gateway's push."""
    from app.codex_relay import hook

    try:
        event = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    if (
        event.get("hook_event_name") == "Stop"
    ):  # a question asked in the thread goes to Telegram too
        from app.config import ENV_PORTS
        from app.projects import project_for
        from app.waiter import tell

        cwd = event.get("cwd") or os.getcwd()
        if (project := project_for(Path(cwd))) and event.get("last_assistant_message"):
            tell("/api/v1/channel/turn", ENV_PORTS["prod"], {"project": project, "agent": "codex",
                 "cwd": cwd, "text": event["last_assistant_message"]})  # fmt: skip
        print("{}")  # Codex wants JSON from Stop
        return 0
    out = hook(event)
    if out:
        print(json.dumps(out))
    return 0


def cmd_main(args) -> int:
    """#Main typed in a Claude conversation: it becomes its project's pinned 👑 main (its own gateway
    session, split from the folder's shared one) until it disconnects. Its waiter follows by itself."""
    import urllib.error
    import urllib.parse
    import urllib.request

    from app.config import ENV_PORTS, env_name
    from app.projects import client_kind, project_for
    from app.waiter import instance

    cwd = str(Path(args.cwd).resolve())
    project = args.project or project_for(Path(cwd))
    if not project:
        print(f"{cwd} is not a project folder under the projects root.")
        return 2
    query = urllib.parse.urlencode({"project": project, "agent": "claude_code", "cwd": cwd,
                                    "client": client_kind(), "instance": instance()})  # fmt: skip
    url = f"http://127.0.0.1:{ENV_PORTS[env_name()]}/api/v1/channel/main?{query}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="POST"), timeout=30) as r:  # noqa: S310
            out = json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f"Not the main: {json.loads(e.read() or b'{}').get('detail') or e}")
        return 1
    except OSError as e:
        print(f"The gateway is not reachable ({e}).")
        return 1
    print(f"👑 This session ({out['main']}) is now the pinned main of {out['project']} until it "
          "disconnects. Use that session_id with get_messages; your waiter switches to it by itself.")  # fmt: skip
    return 0


def cmd_mcp_stdio(args) -> int:
    """The gateway's MCP tools over stdio for a Claude Code session; reconnects after a restart."""
    import asyncio

    from app.config import ENV_PORTS, env_name
    from app.mcp_proxy import serve

    asyncio.run(serve(args.url or f"http://127.0.0.1:{ENV_PORTS[env_name()]}/mcp"))
    return 0


def cmd_wait(args) -> int:
    from app.config import ENV_PORTS, env_name
    from app.projects import project_for
    from app.waiter import antigravity_notify, wait

    cwd = str(Path(args.cwd).resolve())
    project = args.project or project_for(Path(cwd))
    if not project:
        print(f"{cwd} is not a project folder under the projects root; nothing to wait for.")
        return 2
    notify = None
    if args.agent == "antigravity":
        notify = antigravity_notify()
        if notify is None:
            print(
                "Run this from an Antigravity agent terminal (it needs ANTIGRAVITY_CONVERSATION_ID)."
            )
            return 2
        if not os.environ.get("AGW_WAIT_DETACHED"):  # Antigravity stops its terminal commands
            from app.channel import ChannelHub
            from app.waiter import WAITERS

            WAITERS.mkdir(parents=True, exist_ok=True)
            log = WAITERS / f"{ChannelHub.pull_id(project, 'antigravity', cwd)}.log"
            agw = Path(sys.executable).parent / "agw"
            with open(log, "a") as out:
                proc = subprocess.Popen(  # noqa: S603 - our own CLI, same arguments
                    [str(agw), "wait", "--agent", "antigravity", "--cwd", cwd, "--project", project],
                    stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True,
                    env={**os.environ, "AGW_WAIT_DETACHED": "1"},
                )  # fmt: skip
            print(f"Gateway waiter running in the background (pid {proc.pid}); it wakes this "
                  f"conversation when Telegram work arrives. Log: {log}")  # fmt: skip
            return 0
    return wait(project, cwd, args.client or "app", ENV_PORTS[env_name()], agent=args.agent,
                notify=notify)  # fmt: skip


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
    from app.keys import new_secret

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
                    "TELEGRAM_WEBHOOK_SECRET": secrets.token_urlsafe(32),
                    "AGW_SECRET_KEY": new_secret()}, path)  # fmt: skip
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
        sp.add_argument("--kind", choices=["product", "tool"], default=None,
                        help="product (default for a new project): one worker and one deploy session; "
                        "tool: requests run in parallel sessions, one per topic")  # fmt: skip
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
        "--print-rules", action="store_true", help="print the gateway rules text (no install)"
    )
    sp.set_defaults(fn=cmd_setup)
    sp = sub.add_parser("dev", help="start OpenCode (if enabled) + the gateway")
    hp(sp)
    sp.add_argument(
        "--force", action="store_true", help="start even after `agw stop` without a terminal"
    )
    sp.set_defaults(fn=cmd_dev)
    sp = sub.add_parser("stop", help="stop the gateway; background health checks leave it stopped")
    hp(sp)
    sp.set_defaults(fn=cmd_stop)
    sp = sub.add_parser("doctor", help="show what is configured and reachable")
    hp(sp)
    sp.set_defaults(fn=cmd_doctor)
    sp = sub.add_parser("open", help="open a project folder (fuzzy name) in a Claude session")
    sp.add_argument("query")
    sp.set_defaults(fn=cmd_open)
    sp = sub.add_parser(
        "session", help="(in a repo) open Claude Code that takes work from Telegram"
    )
    sp.add_argument("--no-loop", action="store_true", help=argparse.SUPPRESS)  # (no loop any more)
    sp.add_argument(
        "--model", default="", help="Claude model for the session (opus, sonnet, haiku or an id)"
    )
    sp.add_argument("--agent", default="claude_code", choices=["claude_code", *AGENT_RUN],
                    help="another agent CLI, run once a minute to take work")  # fmt: skip
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
    sub.add_parser("hook-prompt").set_defaults(fn=cmd_hook_prompt)  # UserPromptSubmit
    sub.add_parser("hook-failure").set_defaults(fn=cmd_hook_failure)  # StopFailure
    sub.add_parser("hook-tool").set_defaults(fn=cmd_hook_tool)  # PreToolUse: the main only routes
    sub.add_parser("codex-hook").set_defaults(fn=cmd_codex_hook)  # Codex SessionStart/End
    sub.add_parser("cursor-hook").set_defaults(fn=cmd_cursor_hook)  # Cursor sessionStart/stop
    sp = sub.add_parser("wait")  # pull loop run in the background (app/waiter.py)
    sp.add_argument("--project", default="")  # default: the folder's project (README)
    sp.add_argument("--cwd", default=".")
    sp.add_argument("--client", default="")
    sp.add_argument("--agent", default="claude_code", choices=["claude_code", "antigravity"])
    sp.set_defaults(fn=cmd_wait)
    sp = sub.add_parser("mcp-stdio")  # Claude Code's agent-gateway MCP: a proxy that reconnects
    sp.add_argument("--url", default="")
    sp.set_defaults(fn=cmd_mcp_stdio)
    sp = sub.add_parser("main")  # #Main: this conversation becomes its project's pinned main
    sp.add_argument("--project", default="")
    sp.add_argument("--cwd", default=".")
    sp.set_defaults(fn=cmd_main)

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
