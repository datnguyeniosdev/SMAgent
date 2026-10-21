"""OpenCode agent runtime over its HTTP server API (`opencode serve`, v2 `/api/*`).

Verified against OpenCode v2.1.0 on this machine (schema from GET /openapi.json):
  POST /api/session {location:{directory}, title, permissions[], model?}  -> {data: Session.Info}
  POST /api/session/{id}/prompt {text}                                     -> {data: Inbox.User}
  POST /api/experimental/session/{id}/wait                                 -> 204 when idle
  GET  /api/session/{id}/permission ; POST .../permission/{rid}/reply {decision: once|always|reject}
  GET  /api/session/{id}  (outcome, tokens, cost) ; GET .../message ; GET .../diff
  POST /api/session/{id}/interrupt                                         -> {interrupted}
The server must be started separately (`OPENCODE_SERVER_PASSWORD=... opencode serve`).

Safety: OpenCode's default `build` agent allows every action (observed: it ran `pip3 install`
on the host). Every session created here carries an explicit deny-by-default ruleset:
read/search allowed, file edits allowed (writes outside the directory are a separate
`external_directory` action and stay denied), shell = ask, answered by the gateway's
allowlist; everything else is rejected.
"""

from __future__ import annotations

import asyncio
import logging
import shlex

import httpx

from app.connectors.base import (
    AgentRequest,
    AgentResult,
    Cap,
    ConnectorError,
    UnsupportedCapability,
)

log = logging.getLogger(__name__)
READ_ACTIONS = ("read", "grep", "glob", "list")


def ruleset(read_only: bool) -> list[dict]:
    rules = [{"action": "*", "resource": "*", "effect": "deny"}]
    rules += [{"action": a, "resource": "*", "effect": "allow"} for a in READ_ACTIONS]
    if not read_only:
        rules += [
            {"action": "edit", "resource": "*", "effect": "allow"},
            {"action": "shell", "resource": "*", "effect": "ask"},
        ]
    rules += [
        {"action": "read", "resource": "*.env", "effect": "deny"},
        {"action": "read", "resource": "*.env.*", "effect": "deny"},
    ]
    return rules


def command_allowed(command: str, allowlist: list[str]) -> bool:
    """Exact-prefix match on parsed argv; shell operators make a command ineligible."""
    if any(tok in command for tok in (";", "&&", "||", "|", ">", "<", "`", "$(", "\n")):
        return False
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    for allowed in allowlist:
        want = shlex.split(allowed)
        if argv[: len(want)] == want:
            return True
    return False


class OpenCodeConnector:
    name = "opencode"
    kind = "runtime"
    caps = frozenset(
        {
            Cap.text,
            Cap.workspace_read,
            Cap.workspace_write,
            Cap.terminal,
            Cap.session_resume,
            Cap.cancel,
        }
    )
    verification = "verified"

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:4096",
        password: str | None = None,
        username: str = "opencode",
        model: str | None = None,  # "providerID/modelID"; None = the server's default model
        shell_allowlist: list[str] = (),
        timeout_s: float = 1800,
        enabled: bool = True,
        client: httpx.AsyncClient | None = None,
    ):
        self.base = base_url.rstrip("/") + "/api"
        self.model = model
        self.shell_allowlist = list(shell_allowlist)
        self.timeout_s = timeout_s
        self.available = enabled
        auth = (username, password) if password else None
        self.client = client or httpx.AsyncClient(auth=auth, timeout=60)
        self._sessions: dict[str, str] = {}  # gateway task id -> opencode session id

    async def _req(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            r = await self.client.request(method, f"{self.base}{path}", **kw)
        except httpx.TimeoutException as e:
            raise ConnectorError(f"opencode {path}: timeout", "timeout", outcome="unknown") from e
        except httpx.HTTPError as e:
            raise ConnectorError(
                f"opencode server unreachable: {type(e).__name__}", "unavailable"
            ) from e
        if r.status_code == 401:
            raise ConnectorError("opencode server rejected credentials (OPENCODE_PASSWORD)", "auth")
        if r.status_code >= 400:
            raise ConnectorError(
                f"opencode {path}: HTTP {r.status_code}: {r.text[:200]}", "provider"
            )
        return r

    async def health_status(self) -> tuple[str, str]:
        if not self.available:
            return "unavailable", "OPENCODE_ENABLED is false"
        try:
            info = (await self._req("GET", "/info")).json()
        except ConnectorError as e:
            return "unavailable", str(e)
        version = (info.get("data") or info).get("version", "?") if isinstance(info, dict) else "?"
        return (
            "validated",
            f"opencode server reachable (version {version}); execution not yet proven",
        )

    async def execute(self, req: AgentRequest) -> AgentResult:
        if req.workspace is None:
            raise ConnectorError("opencode requires a workspace directory", "unsupported")
        if req.attachments:
            raise UnsupportedCapability(
                "opencode connector does not forward attachments (files outside the workspace are denied)"
            )
        directory = str(req.workspace.resolve())
        if req.resume_session:
            sid = req.resume_session  # prompting an existing server session (supported by the API)
        else:
            body = {
                "location": {"directory": directory},
                "title": f"gateway {req.task_id}",
                "permissions": ruleset(req.read_only),
            }
            if self.model and "/" in self.model:
                provider, model_id = self.model.split("/", 1)
                body["model"] = {"providerID": provider, "id": model_id}
            sid = (await self._req("POST", "/session", json=body)).json()["data"]["id"]
        self._sessions[req.task_id] = sid
        text = (req.system + "\n\n" + req.prompt) if req.system else req.prompt
        decisions: list[dict] = []
        try:
            await self._req("POST", f"/session/{sid}/prompt", json={"text": text})
            waiter = asyncio.create_task(
                self._req("POST", f"/experimental/session/{sid}/wait", timeout=self.timeout_s + 30)
            )
            try:
                async with asyncio.timeout(self.timeout_s):
                    while not waiter.done():
                        await self._answer_permissions(sid, decisions)
                        await asyncio.wait({waiter}, timeout=0.5)
                    waiter.result()
            except TimeoutError as e:
                waiter.cancel()
                await self.cancel(req.task_id)
                raise ConnectorError(
                    "opencode timed out",
                    "timeout",
                    outcome="failed" if req.read_only else "unknown",
                ) from e
            except asyncio.CancelledError:
                waiter.cancel()
                await self.cancel(req.task_id)
                raise
        finally:
            self._sessions.pop(req.task_id, None)
        return await self._result(sid, decisions)

    async def _answer_permissions(self, sid: str, decisions: list[dict]) -> None:
        pending = (await self._req("GET", f"/session/{sid}/permission")).json().get("data", [])
        done = {d["id"] for d in decisions}
        for p in pending:
            if p["id"] in done:
                continue
            action, resources = p.get("action"), p.get("resources") or []
            ok = (
                action == "shell"
                and resources
                and all(command_allowed(r, self.shell_allowlist) for r in resources)
            )
            decision = "once" if ok else "reject"
            body = {"decision": decision}
            if not ok:
                body["message"] = "not allowed by gateway policy"
            try:
                await self._req("POST", f"/session/{sid}/permission/{p['id']}/reply", json=body)
            except ConnectorError as e:  # request may have been resolved meanwhile
                log.info("permission reply failed: %s", e)
            decisions.append(
                {"id": p["id"], "action": action, "resources": resources[:3], "decision": decision}
            )

    async def _result(self, sid: str, decisions: list[dict]) -> AgentResult:
        info = (await self._req("GET", f"/session/{sid}")).json()["data"]
        msgs = (
            (await self._req("GET", f"/session/{sid}/message", params={"limit": 20}))
            .json()
            .get("data", [])
        )
        assistant = next((m for m in msgs if m.get("type") == "assistant"), {})
        text = "\n".join(
            x.get("text", "") for x in assistant.get("content") or [] if x.get("type") == "text"
        ).strip()
        model = assistant.get("model") or {}
        diff = (await self._req("GET", f"/session/{sid}/diff")).json().get("data", [])
        outcome = info.get("outcome")
        if outcome == "interrupted":
            raise ConnectorError("opencode session was interrupted", "cancelled", outcome="unknown")
        if outcome != "succeeded":
            raise ConnectorError(f"opencode session outcome {outcome!r}: {text[:300]}", "provider")
        tokens = info.get("tokens") or {}
        return AgentResult(
            text=text,
            provider=self.name,
            model=f"{model.get('providerID')}/{model.get('id')}" if model else self.model,
            session_id=sid,
            input_tokens=tokens.get("input"),
            output_tokens=tokens.get("output"),
            cost_usd=info.get("cost"),
            metadata={
                "runtime": "opencode",
                "outcome": outcome,
                "permission_decisions": decisions,
                "files_changed": [{"file": d["file"], "status": d["status"]} for d in diff],
            },
        )

    async def cancel(self, task_id: str) -> bool:
        sid = self._sessions.get(task_id)
        if sid is None:
            return False
        try:
            r = await self._req("POST", f"/session/{sid}/interrupt")
        except ConnectorError:
            return False
        return bool(r.json().get("interrupted"))
