"""FastAPI gateway: REST control API + Telegram polling in the same process."""

import asyncio
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from app import catalog, keys
from app.actions import ActionError, ActionRegistry
from app.config import Settings, env_file, get_settings
from app.connectors.base import Cap, ConnectorError
from app.connectors.claude_code import ClaudeCodeConnector
from app.connectors.http_providers import ChatCompletionsProvider, OpenAIImageProvider
from app.intake import TaskRequest
from app.logs import setup_logging
from app.models import Message, Workflow
from app.orchestrator import Orchestrator
from app.qa import QARunner
from app.router import Router
from app.storage import ArtifactStore, AttachmentError, AttachmentStore
from app.store import Store
from app.telegram_bot import WEBHOOK_PATH
from app.workspace import WorkspaceManager

log = logging.getLogger(__name__)


def build_providers(cfg, client: httpx.AsyncClient) -> list:
    """Model providers declared in config/providers. Keys and models come from the environment."""
    from app.connectors.http_providers import GeminiProvider

    out = []
    names = {
        "model.text": Cap.text,
        "model.vision": Cap.image_input,
        "model.files": Cap.file_input,
        "model.structured": Cap.structured_output,
        "model.tool_calling": Cap.tool_calling,
    }
    for p in cfg.providers.values():
        if not p.enabled or p.type in ("fal", "meshy"):  # image / 3D providers are built separately
            continue
        key = os.environ.get(p.api_key_env)
        model = p.model or (os.environ.get(p.model_env) if p.model_env else None)
        caps = frozenset(names[c] for c in p.capabilities)
        if p.type == "gemini":
            out.append(
                GeminiProvider(
                    p.id,
                    key,
                    model,
                    caps,
                    p.base_url or "https://generativelanguage.googleapis.com/v1beta",
                    p.timeout_s,
                    client,
                )
            )
        else:
            out.append(
                ChatCompletionsProvider(
                    p.id,
                    p.base_url or "",
                    key,
                    model,
                    caps,
                    json_schema_mode=p.json_mode == "json_schema",
                    client=client,
                )
            )
    return out


def load_gateway_config(s: Settings):
    from app.gateway_config import load_or_raise

    if s.config_dir is None or not (s.config_dir / "app.yaml").exists():
        return None
    cfg = load_or_raise(s.config_dir)  # invalid configuration aborts startup with diagnostics
    for d in cfg.diagnostics:
        log.warning("config: %s", d)
    if s.telegram_discussion_chat_id is not None:  # environment overrides YAML
        cfg.telegram.discussion_chat_id = s.telegram_discussion_chat_id
    return cfg


def build_model3d(gateway_cfg, client):
    """The 3D provider (Meshy) when configured with a key, else None."""
    p = next(
        (p for p in (gateway_cfg.providers.values() if gateway_cfg else []) if p.type == "meshy"),
        None,
    )
    if p is None or not p.enabled or not os.environ.get(p.api_key_env):
        return None
    from app.connectors.meshy import MeshyProvider

    model = p.model or (os.environ.get(p.model_env) if p.model_env else None)
    return MeshyProvider(os.environ[p.api_key_env], model, client, timeout_s=p.timeout_s)


def build_image(gateway_cfg, s: Settings, client) -> tuple:
    """-> (image provider or None, starting state of the generate_art switch)."""
    key = lambda v: v.get_secret_value() if v else None  # noqa: E731
    image, image_on = None, True
    fal = next(
        (p for p in (gateway_cfg.providers.values() if gateway_cfg else []) if p.type == "fal"),
        None,
    )
    if fal is not None and os.environ.get(fal.api_key_env):
        from app.connectors.fal import FalImageProvider

        image_on = fal.enabled  # only the starting state: Telegram /mcp switches it live
        image = FalImageProvider(
            os.environ[fal.api_key_env],
            fal.model
            or (os.environ.get(fal.model_env) if fal.model_env else None)
            or "fal-ai/flux/schnell",
            timeout_s=fal.timeout_s,
            extra_input=fal.options,
        )
    elif s.openai_api_key and s.openai_image_model:
        image = OpenAIImageProvider(
            key(s.openai_api_key), s.openai_image_model, s.openai_base_url, client
        )
    return image, image_on


def build(settings: Settings) -> Orchestrator:
    s = settings
    # Providers name their key by env var (api_key_env: FAL_KEY, GEMINI_API_KEY, ...). Settings reads
    # .env for its own fields only, so export .env too; variables already set in the shell win.
    load_dotenv(env_file(), override=False)
    store = Store(s.state_db if s.state_db == ":memory:" else s.data_dir / s.state_db)
    if bad := keys.load(store):  # /keys values (encrypted in the database) win over .env
        log.warning(
            "saved settings not loaded, AGW_SECRET_KEY missing or changed: %s", ", ".join(bad)
        )
    client = httpx.AsyncClient(timeout=s.http_timeout_s)
    key = lambda v: v.get_secret_value() if v else None  # noqa: E731
    gateway_cfg = load_gateway_config(s)
    if gateway_cfg is not None:
        catalog.merge(store, gateway_cfg)  # providers added from Telegram (/providers)
    claude = ClaudeCodeConnector(
        s.claude_code_bin,
        s.claude_code_model,
        s.claude_code_allowed_tools,
        str(s.claude_code_mcp_config) if s.claude_code_mcp_config else None,
        s.claude_code_max_budget_usd,
        s.task_timeout_s,
        enabled=s.claude_code_enabled,
    )
    from app.connectors.opencode import OpenCodeConnector

    opencode = OpenCodeConnector(
        s.opencode_url,
        key(s.opencode_password),
        s.opencode_username,
        s.opencode_model,
        s.opencode_shell_allowlist,
        s.task_timeout_s,
        enabled=s.opencode_enabled,
    )
    if gateway_cfg is not None:
        connectors = [claude, opencode, *build_providers(gateway_cfg, client)]
    else:  # legacy env-only provider configuration
        deepseek_caps = {Cap.text, Cap.file_input, Cap.structured_output, Cap.tool_calling}
        if s.deepseek_image_input:
            deepseek_caps.add(Cap.image_input)
        connectors = [
            claude,
            ChatCompletionsProvider(
                "openai",
                s.openai_base_url,
                key(s.openai_api_key),
                s.openai_model,
                frozenset(
                    {
                        Cap.text,
                        Cap.image_input,
                        Cap.file_input,
                        Cap.structured_output,
                        Cap.tool_calling,
                    }
                ),
                json_schema_mode=True,
                client=client,
            ),
            ChatCompletionsProvider(
                "deepseek",
                s.deepseek_base_url,
                key(s.deepseek_api_key),
                s.deepseek_model,
                frozenset(deepseek_caps),
                json_schema_mode=False,
                client=client,
            ),
        ]
    image, image_on = build_image(gateway_cfg, s, client)
    orch = Orchestrator(
        s,
        Router(connectors),
        WorkspaceManager(s.workspace_root, s.data_dir / "worktrees"),
        AttachmentStore(s.data_dir / "media", s.max_attachment_bytes, store),
        ArtifactStore(s.data_dir / "artifacts", store),
        QARunner(
            s.qa_commands,
            s.qa_timeout_s,
            s.qa_max_output_bytes,
            s.qa_sandbox,
            s.qa_docker_image,
            tuple(s.qa_env_passthrough),
        ),
        ActionRegistry(s.action_ttl_s),
        image_provider=image,
        image_on=image_on,
        store=store,
        gateway_cfg=gateway_cfg,
    )
    orch.http_client = client  # reused by reload_providers
    orch.model3d_provider = build_model3d(gateway_cfg, client)
    refresh_tools(orch)
    return orch


def refresh_tools(orch) -> None:
    """Plugin tools whose provider appeared after the plugins loaded (e.g. media.generate_3d)."""
    for name in orch.plugins.refresh_tools(orch, orch.tools):
        orch.register_tool_capability(orch.tools.get(name))


def reload_providers(orch) -> None:
    """After /keys changed a key or model: rebuild the model and image providers from the environment,
    no restart. The Claude Code / OpenCode runtimes are kept as they are."""
    cfg = orch.gateway_cfg
    if cfg is None:
        return
    client = getattr(orch, "http_client", None) or httpx.AsyncClient(timeout=orch.s.http_timeout_s)
    catalog.merge(orch.store, cfg)
    keep = {n: c for n, c in orch.router.connectors.items() if n in ("claude_code", "opencode")}
    orch.router.connectors = {**keep, **{c.name: c for c in build_providers(cfg, client)}}
    orch.register_connectors()
    # ponytail: OpenAI images read Settings (loaded at start); only fal picks up a new key live
    orch.image_provider, _ = build_image(cfg, orch.s, client)
    orch.model3d_provider = build_model3d(cfg, client)
    refresh_tools(orch)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_level, settings.secret_values(), settings.data_dir / "agw.log")
    logging.getLogger("uvicorn.access").addFilter(_QuietProbes())
    orch = build(settings)
    app.state.orch = orch
    app.state.settings = settings
    await orch.start()  # MCP discovery, health checks, recovery
    from app.mcp_server import build_mcp_server

    mcp = build_mcp_server(orch)
    # Stateless: no MCP session lives in this process, so a gateway restart does not break connected
    # agents (their next call just works; no tool here needs server->client session state).
    app.state.mcp_asgi = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True)
    # The session manager's task group must be entered and exited by the same task.
    mcp_ready, mcp_stop = asyncio.Event(), asyncio.Event()

    async def run_mcp():
        async with mcp.session_manager.run():
            mcp_ready.set()
            await mcp_stop.wait()

    mcp_task = asyncio.create_task(run_mcp())
    await mcp_ready.wait()
    poller, webhook = None, False
    app.state.telegram, app.state.webhook_tasks = None, set()
    if settings.telegram_bot_token:
        from app.telegram_bot import TelegramGateway

        gw = TelegramGateway(settings, orch, orch.attachments, orch.actions)
        orch.notifier = gw
        if settings.telegram_mode == "polling":
            poller = asyncio.create_task(gw.run_polling())
        elif not settings.telegram_webhook_url:
            log.error(
                "TELEGRAM_MODE=webhook but TELEGRAM_WEBHOOK_URL is empty: not receiving updates"
            )
        else:
            try:
                await gw.start_webhook(
                    settings.telegram_webhook_url,
                    settings.telegram_webhook_secret.get_secret_value(),
                )
                app.state.telegram, webhook = gw, True
            except Exception as e:  # sending still works; receiving is reported as down
                log.error("telegram webhook registration failed: %s", type(e).__name__)
    else:
        log.warning("TELEGRAM_BOT_TOKEN not set: Telegram disabled, reports go to the log")

    async def ask_watchdog():  # agents waiting on agents: escalate when stuck; expire lost leases
        while True:
            await asyncio.sleep(30)
            try:
                await orch.asks.check()
                await orch.collab.remind_questions()  # unanswered operator questions: every 30 min
                orch.channel._expire()  # even when no agent polls (waiters only peek)
                orch.channel._prune()  # a session whose waiter died is reported within a minute
            except Exception:
                log.exception("ask watchdog round failed")

    watchdog = asyncio.create_task(ask_watchdog())
    background = [watchdog]
    from app.config import env_name

    if env_name() == "prod":  # Codex desktop hooks record their threads for the prod gateway
        from app.codex_relay import push_loop

        background.append(
            asyncio.create_task(push_loop(orch.channel, settings.data_dir / "codex-relays"))
        )
    yield
    for t in background:
        t.cancel()
    await asyncio.gather(*background, return_exceptions=True)
    if poller:
        poller.cancel()
        await asyncio.gather(poller, return_exceptions=True)
    if webhook:
        try:
            await app.state.telegram.stop_webhook()
        except Exception as e:
            log.warning("could not remove the telegram webhook: %s", type(e).__name__)
        await asyncio.gather(*app.state.webhook_tasks, return_exceptions=True)
    # deploys and reviews: stop their processes and report "interrupted" while Telegram still works
    jobs = [*getattr(orch.notifier, "_deploy_tasks", ()), *orch.reviews._jobs.values()]
    for t in jobs:
        t.cancel()
    if jobs:
        try:
            await asyncio.wait_for(asyncio.gather(*jobs, return_exceptions=True), 15)
        except TimeoutError:
            log.warning("deploy/review jobs did not stop within 15 s")
    if hasattr(orch.notifier, "bot"):
        await orch.notifier.bot.session.close()  # aiohttp session of the Telegram bot
    mcp_stop.set()
    try:
        await asyncio.wait_for(mcp_task, 5)
    except (TimeoutError, Exception):  # open client streams must not block shutdown
        mcp_task.cancel()
        await asyncio.gather(mcp_task, return_exceptions=True)
    for t in list(orch._running.values()):
        t.cancel()
    await asyncio.gather(*orch._running.values(), return_exceptions=True)
    orch.store.close()


app = FastAPI(title="Agent Gateway", lifespan=lifespan)
bearer = HTTPBearer(auto_error=False)


def orch(request: Request) -> Orchestrator:
    return request.app.state.orch


def require_token(
    request: Request, creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
) -> str:
    token = request.app.state.settings.api_token
    if token is None:
        raise HTTPException(503, "control API disabled: API_TOKEN not set")
    if creds is None or not secrets.compare_digest(
        creds.credentials.encode(), token.get_secret_value().encode()
    ):
        raise HTTPException(401, "invalid token")
    return "api"


Auth = Annotated[str, Depends(require_token)]


def require_operator(
    request: Request, creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
) -> str:
    """Approve / execute / answer: decisions the operator makes, never the agents (they hold API_TOKEN)."""
    token = request.app.state.settings.operator_token
    if token is None:
        raise HTTPException(
            403, "operator decisions are made on Telegram (set OPERATOR_TOKEN to allow them here)"
        )
    if creds is None or not secrets.compare_digest(
        creds.credentials.encode(), token.get_secret_value().encode()
    ):
        raise HTTPException(401, "invalid operator token")
    return "operator"


Operator = Annotated[str, Depends(require_operator)]
Orch = Annotated[Orchestrator, Depends(orch)]


@app.exception_handler(ConnectorError)
async def _connector_error(request, e: ConnectorError):
    return JSONResponse({"error": e.category, "detail": str(e)}, status_code=422)


@app.exception_handler(AttachmentError)
async def _attachment_error(request, e: AttachmentError):
    return JSONResponse({"error": "attachment", "detail": str(e)}, status_code=400)


@app.exception_handler(ActionError)
async def _action_error(request, e: ActionError):
    return JSONResponse({"error": "action", "detail": str(e)}, status_code=409)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/v1/agents")
async def agents(_: Auth, o: Orch):
    return {"profiles": o.profiles(), "connectors": o.router.describe()}


@app.get("/api/v1/sessions")
async def sessions(_: Auth, o: Orch):
    return list(o.sessions.values())


@app.get("/api/v1/sessions/{session_id}")
async def session(session_id: str, _: Auth, o: Orch):
    if session_id not in o.sessions:
        raise HTTPException(404, "unknown session")
    return o.sessions[session_id]


class TaskIn(BaseModel):
    """Generic task request. `description`/`workflow` remain as legacy aliases."""

    objective: str | None = Field(None, max_length=20_000)
    description: str | None = Field(None, max_length=20_000)
    workflow: Workflow | None = None
    domain: str | None = None
    task_type: str | None = None
    agent: str | None = None
    attachment_ids: list[str] = Field(default=[], max_length=20)
    artifact_ids: list[str] = Field(default=[], max_length=20)
    data: dict = {}
    constraints: list[str] = Field(default=[], max_length=50)
    output_format: str | None = None
    mode: str = "auto"
    tool: str | None = None
    tool_args: dict = {}
    workflow_id: str | None = None
    require_approval: bool = False
    idempotency_key: str | None = None
    timeout_s: int | None = None


@app.post("/api/v1/tasks", status_code=201)
async def create_task(body: TaskIn, actor: Auth, o: Orch):
    text = body.objective or body.description
    if not text:
        raise HTTPException(422, "objective is required")
    try:
        legacy = body.workflow or (
            Workflow.ask if body.description and not body.objective else None
        )
        if legacy is not None and not body.domain and not body.tool:
            task = await o.submit(text, legacy, actor, body.attachment_ids, body.agent)
        else:
            req = TaskRequest(
                objective=text,
                user_id=actor,
                **body.model_dump(
                    exclude={"objective", "description", "workflow"}, exclude_none=True
                ),
            )
            task = await o.submit_request(req)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return task


@app.post("/api/v1/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, _: Auth, o: Orch):
    try:
        stopped = await o.stop(task_id)
    except KeyError as e:
        raise HTTPException(404, "unknown task") from e
    t = o.tasks[task_id]
    return {
        "stopped": stopped,
        "state": t.state,
        "note": "unknown means an in-flight side effect may have completed",
    }


class AnswerIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@app.post("/api/v1/tasks/{task_id}/answer")
async def answer_task(task_id: str, body: AnswerIn, actor: Auth, o: Orch):
    return {"result": await o.answer(task_id, body.text, actor)}


@app.get("/api/v1/tasks/{task_id}/result")
async def task_result(task_id: str, _: Auth, o: Orch):
    if task_id not in o.tasks:
        raise HTTPException(404, "unknown task")
    return o.envelope(o.tasks[task_id])


@app.get("/api/v1/tasks/{task_id}/workflow")
async def task_workflow(task_id: str, _: Auth, o: Orch):
    t = o.tasks.get(task_id)
    if t is None or not t.run_id:
        raise HTTPException(404, "no workflow for task")
    run = o.load_run(t.run_id)
    return {
        "run": run.model_dump(exclude={"workflow"}),
        "definition": run.workflow,
        "steps": list(o.load_steps(t.run_id).values()),
    }


@app.get("/api/v1/tasks/{task_id}/events")
async def task_events(task_id: str, _: Auth, o: Orch):
    return o.store.events(task_id)


@app.get("/api/v1/approvals")
async def approvals(_: Auth, o: Orch, task_id: str | None = None):
    return o.pending_approvals(task_id)


class DecisionIn(BaseModel):
    approve: bool


@app.post("/api/v1/approvals/{approval_id}")
async def decide(approval_id: str, body: DecisionIn, actor: Operator, o: Orch):
    return {"result": o.decide_approval(approval_id, body.approve, actor)}


@app.get("/api/v1/tools")
async def tools(_: Auth, o: Orch):
    return {
        "tools": [t.public() for t in o.tools.list()],
        "discovery_notes": o.tools.discovery_notes,
    }


@app.get("/api/v1/capabilities")
async def capabilities(_: Auth, o: Orch, refresh: bool = False):
    if refresh:
        await o.refresh_health()
    return o.caps.summary()


@app.get("/api/v1/plugins")
async def plugins(_: Auth, o: Orch):
    return [
        {
            "id": lp.manifest.id if lp.manifest else None,
            "module": lp.module,
            "active": lp.active,
            "error": lp.error,
            "tools": lp.tool_names,
            "manifest": lp.manifest.model_dump() if lp.manifest else None,
        }
        for lp in o.plugins.loaded.values()
    ]


@app.post("/api/v1/plugins/{plugin_id}/{action}")
async def plugin_toggle(plugin_id: str, action: str, _: Auth, o: Orch):
    if action not in ("activate", "deactivate"):
        raise HTTPException(404, "unknown action")
    try:
        o.plugins.set_active(plugin_id, action == "activate")
    except KeyError as e:
        raise HTTPException(404, "unknown plugin") from e
    return {"plugin": plugin_id, "active": action == "activate"}


@app.get("/api/v1/artifacts/{artifact_id}/content")
async def artifact_content(artifact_id: str, _: Auth, o: Orch):
    art = o.artifacts.get(artifact_id)
    if art is None:
        raise HTTPException(404, "unknown artifact")
    return Response(
        o.artifacts.read(art),
        media_type=art.mime_type or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{art.filename}"'},
    )


@app.get("/api/v1/tasks/{task_id}")
async def get_task(task_id: str, _: Auth, o: Orch):
    if task_id not in o.tasks:
        raise HTTPException(404, "unknown task")
    t = o.tasks[task_id]
    children = [c for c in o.tasks.values() if c.parent_id == task_id]
    steps = list(o.load_steps(t.run_id).values()) if t.run_id else []
    return {"task": t, "agent_steps": children, "workflow_steps": steps, "steps": children}


@app.get("/api/v1/tasks/{task_id}/artifacts")
async def task_artifacts(task_id: str, _: Auth, o: Orch):
    if task_id not in o.tasks:
        raise HTTPException(404, "unknown task")
    # Metadata only; files are not served over HTTP.
    return [
        a.model_dump(exclude={"path"}) for a in o._all_artifacts(o.tasks[task_id])
    ]  # content: /artifacts/{id}/content


@app.post("/api/v1/attachments", status_code=201)
async def upload(file: UploadFile, _: Auth, o: Orch):
    limit = o.attachments.max_bytes
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"attachment exceeds {limit} bytes")
    att = o.attachments.save(data, file.filename or "file", file.content_type or "", "api")
    return att.model_dump(exclude={"storage_path"})


class MessageIn(BaseModel):
    content: str = Field(default="", max_length=20_000)
    attachment_ids: list[str] = Field(default=[], max_length=20)
    workflow: Workflow | None = None  # set to create a task from this message


@app.post("/api/v1/messages", status_code=201)
async def post_message(body: MessageIn, actor: Auth, o: Orch):
    for att_id in body.attachment_ids:
        o.attachments.get(att_id)
    msg = Message(source=actor, content=body.content, attachments=body.attachment_ids)
    if body.workflow:
        try:
            task = await o.submit(body.content, body.workflow, actor, body.attachment_ids)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        msg.task_id = task.task_id
    return msg


@app.post("/api/v1/actions/{action_id}/execute")
async def execute_action(action_id: str, actor: Operator, o: Orch):
    return {"result": await o.apply_action(action_id, actor)}


# ---------- config, reports, feedback, human input, handoffs ----------


@app.get("/api/v1/config/validate")
async def config_validate(_: Auth, request: Request):
    from app.gateway_config import load_config

    s = request.app.state.settings
    if s.config_dir is None:
        return {"valid": False, "diagnostics": ["CONFIG_DIR not set"]}
    cfg = load_config(s.config_dir)
    return {
        "valid": not cfg.errors,
        "diagnostics": [str(d) for d in cfg.diagnostics],
        "counts": {
            "agents": len(cfg.agents),
            "capabilities": len(cfg.capabilities),
            "workflows": len(cfg.workflows),
            "routes": len(cfg.routing.routes),
        },
    }


@app.post("/api/v1/config/reload")
async def config_reload(_: Auth, request: Request, o: Orch):
    """Validate first; swap only if valid. Running workflows keep their persisted definitions."""
    from app.gateway_config import load_config

    s = request.app.state.settings
    cfg = load_config(s.config_dir) if s.config_dir else None
    if cfg is None or cfg.errors:
        raise HTTPException(
            422,
            {
                "error": "invalid_config",
                "diagnostics": [str(d) for d in (cfg.errors if cfg else [])],
            },
        )
    if s.telegram_discussion_chat_id is not None:
        cfg.telegram.discussion_chat_id = s.telegram_discussion_chat_id
    o.reload_config(cfg)
    return {
        "reloaded": True,
        "agents": sorted(cfg.agents),
        "workflows": {w.id: w.version for w in cfg.workflows.values()},
    }


@app.get("/api/v1/agents/registry")
async def agent_registry(_: Auth, o: Orch):
    if o.registry is None:
        return {"agents": [], "capabilities": []}
    return {
        "agents": [
            {**a.model_dump(), "status": o.caps.get(f"agent:{a.id}").status}
            for a in o.registry.agents.values()
        ],
        "capabilities": [
            {
                "id": c.id,
                "status": o.caps.get(f"capability:{c.id}").status,
                "agents": o.registry.agents_for(c.id),
                "executors": [
                    {"executor": x.executor_id, "eligible": x.ok, "reason": x.reason}
                    for x in o.registry.candidates(c.id)
                ],
            }
            for c in o.registry.capabilities.values()
        ],
    }


class ReportIn(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)
    agent_status: str | None = None
    images: list[dict] = Field(default=[], max_length=10)  # [{"name": ..., "data_base64": ...}]
    artifact_ids: list[str] = Field(default=[], max_length=20)
    handoff_id: str | None = None
    source: str = "api"


@app.post("/api/v1/tasks/{task_id}/reports", status_code=201)
async def submit_report(task_id: str, body: ReportIn, _: Auth, o: Orch):
    import base64
    import binascii

    from app.collaboration import CollaborationError

    try:
        imgs = [
            (
                str(i.get("name") or "screenshot.png")[:100],
                base64.b64decode(i["data_base64"], validate=True),
            )
            for i in body.images
        ]
        rep = await o.collab.submit_report(
            task_id,
            body.text,
            body.source,
            body.agent_status,
            imgs,
            body.artifact_ids,
            body.handoff_id,
        )
    except (KeyError, binascii.Error) as e:
        raise HTTPException(400, "invalid image payload") from e
    except CollaborationError as e:
        raise HTTPException(
            404 if e.code == "unknown_task" else 400, {"error": e.code, "detail": str(e)}
        ) from e
    return rep.model_dump(mode="json")


@app.get("/api/v1/tasks/{task_id}/reports")
async def list_reports(task_id: str, _: Auth, o: Orch):
    return [r.model_dump(mode="json") for r in o.collab.reports_for(task_id)]


class FeedbackIn(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    task_id: str | None = None


@app.post("/api/v1/reports/{report_id}/feedback", status_code=201)
async def report_feedback(report_id: str, body: FeedbackIn, actor: Auth, o: Orch):
    from app.collaboration import CollaborationError

    try:
        fb = await o.collab.submit_feedback(
            text=body.text, user_id=actor, source="api", report_id=report_id
        )
    except CollaborationError as e:
        raise HTTPException(
            404 if e.code.startswith("unknown") else 400, {"error": e.code, "detail": str(e)}
        ) from e
    return fb.model_dump(mode="json")


@app.get("/api/v1/tasks/{task_id}/handoffs")
async def handoffs(task_id: str, _: Auth, o: Orch):
    return [h.model_dump(mode="json") for h in o.collab.handoffs_for(task_id)]


class HumanInputIn(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    task_id: str | None = None
    options: list[str] = []


@app.post("/api/v1/human-input", status_code=201)
async def human_input(body: HumanInputIn, _: Auth, o: Orch):
    hi = await o.collab.request_human_input(body.question, body.task_id, body.options)
    return hi.model_dump(mode="json")


@app.get("/api/v1/human-input/{request_id}")
async def human_input_get(request_id: str, _: Auth, o: Orch):
    from app.collaboration import CollaborationError

    try:
        return o.collab.get_human_input(request_id).model_dump(mode="json")
    except CollaborationError as e:
        raise HTTPException(404, str(e)) from e


@app.post("/api/v1/human-input/{request_id}/answer")
async def human_input_answer(request_id: str, body: AnswerIn, actor: Operator, o: Orch):
    from app.collaboration import CollaborationError

    try:
        return o.collab.answer_human_input(request_id, body.text, actor).model_dump(mode="json")
    except CollaborationError as e:
        raise HTTPException(409, {"error": e.code, "detail": str(e)}) from e


@app.post("/api/v1/maintenance/cleanup-artifacts")
async def cleanup_artifacts(_: Auth, o: Orch, retention_days: int | None = None):
    return o.cleanup_artifacts(retention_days)


@app.post("/api/v1/deployments", status_code=201)
async def report_deployment(body: dict, actor: Auth, o: Orch):
    from pydantic import ValidationError

    from app.collaboration import Deployment

    try:
        d = Deployment.model_validate({**body, "source": body.get("source") or actor})
    except ValidationError as e:
        raise HTTPException(422, e.errors()[:3]) from e
    return (await o.collab.report_deployment(d)).model_dump(mode="json")


@app.get("/api/v1/deployments")
async def list_deployments(_: Auth, o: Orch, limit: int = 50):
    return [d.model_dump(mode="json") for d in o.collab.deployments(limit)]


# ---------- live Claude Code sessions (channel shim): local requests only, no token ----------


def _local_only(request: Request) -> None:
    headers = {k.lower().encode(): v.encode() for k, v in request.headers.items()}
    if not _local_request(request.scope, headers):
        raise HTTPException(403, "local requests only")


@app.get("/api/v1/channel/peek", include_in_schema=False)
async def channel_peek(
    request: Request, project: str, agent: str, cwd: str = "", client: str = "cli"
):
    """`agw agent-loop`: keep the pull session listed and say whether there is anything to do (work:
    new or unfinished; queued: new only), so a paid model run starts only when there is work."""
    _local_only(request)
    return request.app.state.orch.channel.peek(project, agent, cwd, client)


@app.get("/api/v1/channel/wait", include_in_schema=False)
async def channel_wait(
    request: Request, project: str, agent: str, cwd: str = "", client: str = "cli", after: str = "",
    hold: float = 25,
):  # fmt: skip
    """`agw wait`: peek held open until a message other than `after` is queued (or the session
    closes), at most `hold` seconds: idle waiters cost one held request per 25 s and wake at once."""
    _local_only(request)
    hub = request.app.state.orch.channel
    return await hub.wait(project, agent, cwd, client, after, max(0.0, min(hold, 55.0)))


@app.post("/api/v1/channel/stalled", include_in_schema=False)
async def channel_stalled(
    request: Request, project: str, agent: str, cwd: str = "", reason: str = "", client: str = ""
):
    """An agent's failure hook (Claude StopFailure, Cursor stop with an error): its work is held for
    the operator on Telegram instead of waiting out the lease."""
    _local_only(request)
    hub = request.app.state.orch.channel
    held = hub.stall(hub.pull_id(project, agent, cwd, client), reason or "error")
    return {"stalled": [m["message_id"] for m in held]}


class TurnIn(BaseModel):
    project: str = Field(max_length=200)
    agent: str = Field(max_length=40)
    cwd: str = Field("", max_length=1000)
    text: str = Field("", max_length=20_000)


@app.post("/api/v1/channel/turn", include_in_schema=False)
async def channel_turn(request: Request, body: TurnIn):
    """An agent tool's turn ended (Codex Stop, Cursor afterAgentResponse): a question it asked only in
    its own chat is forwarded to Telegram; the answer wakes that session."""
    _local_only(request)
    orch = request.app.state.orch
    hub = orch.channel
    sid = hub.pull_id(body.project, body.agent, body.cwd)
    s = hub.sessions.get(sid)
    label = s.title if s else body.agent
    hi = await orch.collab.turn_ended(body.project, sid, label, body.text)
    return {"forwarded": hi.request_id if hi else None}


@app.post("/api/v1/channel/active", include_in_schema=False)
async def channel_active(
    request: Request, project: str, agent: str, cwd: str = "", client: str = ""
):
    """The agent's model runs again (a prompt): work it left stalled is put to the operator."""
    _local_only(request)
    hub = request.app.state.orch.channel
    return {
        "asked": [m["message_id"] for m in hub.active(hub.pull_id(project, agent, cwd, client))]
    }


@app.get("/api/v1/channel/stream", include_in_schema=False)
async def channel_stream(
    request: Request,
    project: str,
    session_id: str,
    cwd: str = "",
    blocked: bool = False,
    pid: int = 0,
):
    """SSE stream held by the shim inside a Claude Code session; each event is one message."""
    _local_only(request)
    if not (1 <= len(project) <= 120 and 1 <= len(session_id) <= 64):
        raise HTTPException(422, "project and session_id are required")
    hub = request.app.state.orch.channel
    sess = hub.connect(session_id, project, cwd[:500], blocked, pid=pid)

    async def events():
        try:
            yield ": connected\n\n"
            while not await request.is_disconnected():
                try:
                    msg = await asyncio.wait_for(sess.queue.get(), 15)
                except TimeoutError:
                    yield ": ping\n\n"  # keeps the shim's read timeout from firing
                    continue
                pushed = {**msg, "meta": {**(msg.get("meta") or {}), "session_id": sess.session_id}}
                yield f"data: {json.dumps(pushed, ensure_ascii=False)}\n\n"
                # ponytail: at-most-once; a message read off the queue just before the shim drops is lost
                hub.delivered(msg, sess.session_id)
        finally:
            hub.disconnect(sess)

    return StreamingResponse(events(), media_type="text/event-stream")


class ChannelReplyIn(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=8000)
    message_id: str | None = Field(None, max_length=40)
    image_paths: list[str] = Field(default_factory=list, max_length=10)


@app.post("/api/v1/channel/reply", include_in_schema=False)
async def channel_reply(request: Request, body: ChannelReplyIn):
    _local_only(request)
    from app.collaboration import CollaborationError
    from app.storage import AttachmentError, load_image_paths

    try:
        rep = await request.app.state.orch.channel.reply(
            body.session_id, body.text, body.message_id, load_image_paths(body.image_paths)
        )
    except KeyError as e:
        raise HTTPException(404, "unknown session") from e
    except (AttachmentError, CollaborationError) as e:
        raise HTTPException(422, str(e)) from e
    from app.mcp_server import reply_result

    return reply_result(rep)


@app.get("/api/v1/channel/sessions", include_in_schema=False)
async def channel_sessions(request: Request):
    _local_only(request)
    return [
        {"session_id": s.session_id, "project": s.project, "cwd": s.cwd, "task_id": s.task_id,
         "blocked": s.blocked, "connected_at": s.connected_at.isoformat()}
        for s in request.app.state.orch.channel.live()
    ]  # fmt: skip


@app.post(WEBHOOK_PATH, include_in_schema=False)
async def telegram_webhook(request: Request):
    """Telegram pushes updates here in webhook mode. Only requests carrying the secret token
    registered with setWebhook are accepted; the update is processed after the 200 reply."""
    s, gw = request.app.state.settings, request.app.state.telegram
    if gw is None or s.telegram_webhook_secret is None:
        raise HTTPException(404, "Not Found")
    got = request.headers.get("x-telegram-bot-api-secret-token", "")
    if not secrets.compare_digest(
        got.encode(), s.telegram_webhook_secret.get_secret_value().encode()
    ):
        raise HTTPException(401, "invalid secret token")
    body = await request.body()
    if len(body) > 1_000_000:
        raise HTTPException(413, "update too large")
    try:
        data = json.loads(body)
    except ValueError as e:
        raise HTTPException(400, "invalid JSON") from e
    if not isinstance(data, dict) or not isinstance(data.get("update_id"), int):
        raise HTTPException(400, "not a Telegram update")
    tasks = request.app.state.webhook_tasks
    task = asyncio.create_task(gw.feed(data))  # reply fast; Telegram retries slow webhooks
    tasks.add(task)

    def done(t: asyncio.Task) -> None:
        tasks.discard(t)
        if not t.cancelled() and t.exception():
            log.error("webhook update failed: %s", type(t.exception()).__name__)

    task.add_done_callback(done)
    return {"ok": True}


def _is_oauth_probe(path: str) -> bool:
    """OAuth discovery / dynamic registration that MCP clients try after a 401 (RFC 8414/9728/7591)."""
    return "/.well-known/" in path or path in ("/register", "/authorize", "/token")


class _QuietProbes(logging.Filter):
    """Keep OAuth discovery probes out of the access log; one warning about the cause is enough."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args if isinstance(record.args, tuple) else ()
        return not (len(args) >= 3 and isinstance(args[2], str) and _is_oauth_probe(args[2]))


_LOOPBACK = {"127.0.0.1", "::1"}
_LOCAL_NAMES = {"127.0.0.1", "localhost", "::1"}


def _local_request(scope, headers: dict) -> bool:
    """Same-machine request that is not a browser page on another site (DNS rebinding / CSRF):
    loopback client, Host naming this machine, and no Origin other than a local one."""
    from urllib.parse import urlsplit

    client = (scope.get("client") or ("",))[0]
    host = urlsplit("//" + headers.get(b"host", b"").decode()).hostname
    origin = headers.get(b"origin", b"").decode()
    if client not in _LOOPBACK or host not in _LOCAL_NAMES:
        return False
    return not origin or urlsplit(origin).hostname in _LOCAL_NAMES


class _McpEndpoint:
    """ASGI wrapper: bearer auth, then the MCP Streamable HTTP app built at startup."""

    _last_warning = 0.0

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return
        path = scope.get("path", "")
        if scope["type"] == "http" and _is_oauth_probe(path):
            # No OAuth here: the gateway uses a static bearer token (API_TOKEN / AGW_API_TOKEN).
            await JSONResponse({"detail": "no OAuth: send Authorization: Bearer <API_TOKEN>"}, 404)(
                scope, receive, send
            )
            return
        if scope["type"] != "http" or not path.startswith("/mcp"):
            await JSONResponse({"detail": "Not Found"}, 404)(scope, receive, send)
            return
        app_ = scope["app"]
        token = app_.state.settings.api_token
        headers = dict(scope.get("headers") or [])
        got = headers.get(b"authorization", b"").decode()
        if not app_.state.settings.mcp_require_token:
            if _local_request(scope, headers):
                await app_.state.mcp_asgi(scope, receive, send)
            else:
                log.warning("MCP request refused: token-less mode serves only local requests")
                await JSONResponse({"detail": "local requests only"}, 403)(scope, receive, send)
            return
        if token is None:
            await JSONResponse({"detail": "control API disabled: API_TOKEN not set"}, 503)(
                scope, receive, send
            )
            return
        if not secrets.compare_digest(got.encode(), f"Bearer {token.get_secret_value()}".encode()):
            now_ = time.monotonic()
            if now_ - _McpEndpoint._last_warning > 60:  # once a minute, not once per retry
                _McpEndpoint._last_warning = now_
                log.warning(
                    "MCP request rejected: %s bearer token. Start that Claude Code session from a shell "
                    "with AGW_API_TOKEN set (see ~/.zshrc), or re-run `uv run agw mcp-setup`",
                    "missing" if not got else "wrong",
                )
            await JSONResponse(
                {"detail": "invalid token"},
                401,
                headers={"WWW-Authenticate": 'Bearer realm="agent-gateway", error="invalid_token"'},
            )(scope, receive, send)
            return
        await app_.state.mcp_asgi(scope, receive, send)


app.mount("/", _McpEndpoint())  # registered last: only paths no API route matched reach it
