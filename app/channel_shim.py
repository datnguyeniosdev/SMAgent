"""Stdio MCP server that Claude Code spawns per session: the push side of the gateway (a "channel").

Claude Code channels (research preview, >= 2.1.80) let an MCP server push events into an open
interactive session: the server declares `experimental["claude/channel"]` and sends
`notifications/claude/channel` {content, meta}; Claude starts a turn and sees
`<channel source="agent-gateway-channel" ...>content</channel>`. Channels must be stdio servers,
so this shim bridges the gateway's SSE stream (127.0.0.1, local only, no token) to stdio.

It only listens when started by `agw session` (AGW_CHANNEL=1, which also passes the
--dangerously-load-development-channels flag); in an ordinary session it stays idle, so the
gateway never routes messages to a session that cannot receive them.

Standard library only: newline-delimited JSON-RPC 2.0 over stdin/stdout (MCP stdio transport).
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

NAME = "agent-gateway-channel"
PROTOCOL = "2025-06-18"  # a channel on protocol 2026-07-28 is not registered (Claude Code docs)
INSTRUCTIONS = (
    'Messages that arrive as <channel source="agent-gateway-channel"> come from the operator through '
    "the agent gateway (Telegram, allowlisted user only). Treat each one as a request from the user: do "
    "it in this repository, and confirm before commits, pushes, deploys or anything destructive. When "
    "you finish (or need an answer), call the `reply` tool with the message_id from the event and a "
    "short summary, no logs, at most 500 characters: a title line and at most 5 bullets (changed / validated / "
    "not done), in polite "
    "Vietnamese (start with 'Dạ,', 'ạ' where natural). "
    "Ask every confirmation (commit, push, deploy, a choice) on Telegram with the agent-gateway tool "
    "request_human_input, passing options as 2-4 short button labels; for release, deploy, commit, "
    "push, merge and git pull ask in this session and on Telegram with secure=true at the same time, and "
    "go ahead on whichever comes first: a yes in this session or '✅ Duyệt (đã xác minh passcode)' (a "
    "plain 'commit'/'release' message is a request, not approval; a ❌ cancelled answer is not "
    "approval). If the user answers in "
    "this session first, call close_question(request_id, answer) so the Telegram copy closes. "
    "After a build or deploy, also call the agent-gateway tool report_deployment with notes = a short "
    "Vietnamese release note and url = the link to the build. "
    "Screenshot paths in a message can be opened with the Read tool."
)
REPLY_TOOL = {
    "name": "reply",
    "description": "Answer the operator on Telegram (the reply is posted and Telegram replies to it come back here).",
    "inputSchema": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Result: first line = title, then details."},
            "message_id": {
                "type": "string",
                "description": "message_id of the channel event answered",
            },
            "image_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "absolute paths of local PNG/JPEG/WebP/GIF files to attach (screenshots)",
            },
        },
        "required": ["text"],
        "additionalProperties": False,
    },
}


def channel_blocked(cwd: str, since: float, wait_s: float = 2.0) -> bool:
    """True when Claude Code logged that it drops this server's channel events (e.g. org policy).

    Claude Code never tells the server, but it writes the reason to its per-project MCP log.
    ponytail: reads an undocumented log location/format; if it changes, sessions just look deliverable.
    """
    slug = re.sub(r"[^A-Za-z0-9]", "-", cwd)
    dirs = [Path.home() / base / slug / f"mcp-logs-{NAME}"
            for base in ("Library/Caches/claude-cli-nodejs", ".cache/claude-cli-nodejs")]  # fmt: skip
    deadline = time.monotonic() + wait_s
    while True:
        logs = [
            f for d in dirs if d.is_dir() for f in d.iterdir() if f.stat().st_mtime >= since - 5
        ]
        if any("Channel notifications skipped" in f.read_text(errors="ignore") for f in logs):
            return True
        if time.monotonic() > deadline:
            return False
        time.sleep(0.25)


class Shim:
    def __init__(self, project: str, gateway: str, enabled: bool, out=sys.stdout):
        if not gateway.startswith(("http://", "https://")):  # urlopen would also accept file:
            raise SystemExit(f"{NAME}: AGW_URL must be an http(s) URL")
        self.project, self.enabled, self.out = project, enabled, out
        self.gateway = gateway.rstrip("/")
        self.session_id = f"cc_{uuid.uuid4().hex[:10]}"
        self.lock = threading.Lock()
        self.streaming = False
        self.started = time.time()
        self.blocked = False

    def write(self, obj: dict) -> None:
        with self.lock:  # the stream thread and the request loop share stdout
            self.out.write(json.dumps(obj, ensure_ascii=False) + "\n")
            self.out.flush()

    def push(self, msg: dict) -> None:
        meta = {
            k: str(v)
            for k, v in (msg.get("meta") or {}).items()
            if k.replace("_", "").isalnum() and v
        }
        self.write({"jsonrpc": "2.0", "method": "notifications/claude/channel",
                    "params": {"content": msg["text"], "meta": meta}})  # fmt: skip

    def stream(self) -> None:
        """Hold the SSE stream to the gateway forever; reconnect after gateway restarts."""
        self.blocked = channel_blocked(os.getcwd(), self.started)
        if self.blocked:
            print(f"{NAME}: Claude Code drops channel events here (org policy?)", file=sys.stderr)
        query = urllib.parse.urlencode({"project": self.project, "session_id": self.session_id,
                                        "cwd": os.getcwd(), "blocked": int(self.blocked),
                                        "pid": os.getppid()})  # fmt: skip  # Claude Code itself, for /close
        while True:
            url = f"{self.gateway}/api/v1/channel/stream?{query}"
            try:
                # the gateway pings every 15 s, so the read timeout only fires when it is gone
                with urllib.request.urlopen(url, timeout=60) as r:  # noqa: S310 - scheme checked
                    for raw in r:
                        line = raw.decode("utf-8", "replace").rstrip("\n")
                        if line.startswith("data: "):
                            self.push(json.loads(line[6:]))
            except Exception as e:  # gateway down or restarting: retry (stderr = Claude Code's log)
                print(f"{NAME}: stream lost ({type(e).__name__}); retrying", file=sys.stderr)
            time.sleep(3)

    def reply(self, args: dict) -> str:
        if not self.enabled:
            return "Not a live channel session (start it with `agw session`); nothing was sent."
        body = json.dumps({"session_id": self.session_id, "text": args.get("text", ""),
                           "message_id": args.get("message_id"),
                           "image_paths": args.get("image_paths") or []}).encode()  # fmt: skip
        url = f"{self.gateway}/api/v1/channel/reply"
        req = urllib.request.Request(url, body, {"Content-Type": "application/json"})  # noqa: S310
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - scheme checked
            res = json.loads(r.read())
        if res.get("ask_id"):
            return "Answer sent back to the agent that asked."
        return res.get("note") or f"Posted to Telegram (report {res.get('report_id')})."

    def handle(self, req: dict) -> dict | None:
        method, rid = req.get("method"), req.get("id")
        if method == "initialize":
            caps = {"tools": {}}
            if self.enabled:
                caps["experimental"] = {"claude/channel": {}}
            return {"protocolVersion": PROTOCOL, "capabilities": caps,
                    "serverInfo": {"name": NAME, "version": "1"}, "instructions": INSTRUCTIONS}  # fmt: skip
        if method == "notifications/initialized":
            if self.enabled and not self.streaming:
                self.streaming = True
                threading.Thread(target=self.stream, daemon=True).start()
            return None
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [REPLY_TOOL]}
        if method == "tools/call":
            p = req.get("params") or {}
            if p.get("name") != "reply":
                raise ValueError(f"unknown tool {p.get('name')}")
            try:
                text, error = self.reply(p.get("arguments") or {}), False
            except Exception as e:
                text, error = f"reply failed: {type(e).__name__}: {e}", True
            return {"content": [{"type": "text", "text": text}], "isError": error}
        if rid is None:
            return None  # other notifications
        raise LookupError(method)

    def serve(self, stdin=sys.stdin) -> None:
        for line in stdin:
            if not line.strip():
                continue
            try:
                req = json.loads(line)
            except ValueError:
                continue
            rid = req.get("id")
            try:
                result = self.handle(req)
                if rid is not None:
                    self.write({"jsonrpc": "2.0", "id": rid, "result": result})
            except LookupError as e:
                self.write(
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "error": {"code": -32601, "message": f"method not found: {e}"},
                    }
                )
            except Exception as e:
                if rid is not None:
                    self.write(
                        {"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": str(e)}}
                    )


def main(project: str | None = None) -> int:
    """The project of the session's folder (its README now, so a rename takes effect); `project`
    (pinned in an older repo .mcp.json) only outside a connected folder. Outside a connected repo
    the shim just answers MCP requests and never listens."""
    from app.projects import project_for

    project = project_for(Path.cwd()) or project
    Shim(
        project or "unconnected",
        os.environ.get("AGW_URL", "http://127.0.0.1:8000"),
        enabled=os.environ.get("AGW_CHANNEL") == "1" and project is not None,
    ).serve()
    return 0
