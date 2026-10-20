"""FastAPI gateway: REST control API + Telegram polling in the same process."""

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from app.actions import ActionError, ActionRegistry
from app.config import Settings, get_settings
from app.connectors.base import Cap, ConnectorError
from app.connectors.claude_code import ClaudeCodeConnector
from app.connectors.http_providers import ChatCompletionsProvider, OpenAIImageProvider
from app.logs import setup_logging
from app.models import Message, Workflow
from app.orchestrator import Orchestrator
from app.qa import QARunner
from app.router import Router
from app.storage import ArtifactStore, AttachmentError, AttachmentStore
from app.workspace import WorkspaceManager

log = logging.getLogger(__name__)


def build(settings: Settings) -> Orchestrator:
    s = settings
    client = httpx.AsyncClient(timeout=s.http_timeout_s)
    key = lambda v: v.get_secret_value() if v else None  # noqa: E731
    openai = ChatCompletionsProvider(
        "openai",
        s.openai_base_url,
        key(s.openai_api_key),
        s.openai_model,
        frozenset(
            {Cap.text, Cap.image_input, Cap.file_input, Cap.structured_output, Cap.tool_calling}
        ),
        json_schema_mode=True,
        client=client,
    )
    deepseek_caps = {Cap.text, Cap.file_input, Cap.structured_output, Cap.tool_calling}
    if s.deepseek_image_input:
        deepseek_caps.add(Cap.image_input)
    deepseek = ChatCompletionsProvider(
        "deepseek",
        s.deepseek_base_url,
        key(s.deepseek_api_key),
        s.deepseek_model,
        frozenset(deepseek_caps),
        json_schema_mode=False,  # DeepSeek documents json_object only
        client=client,
    )
    claude = ClaudeCodeConnector(
        s.claude_code_bin,
        s.claude_code_model,
        s.claude_code_allowed_tools,
        str(s.claude_code_mcp_config) if s.claude_code_mcp_config else None,
        s.claude_code_max_budget_usd,
        s.task_timeout_s,
        enabled=s.claude_code_enabled,
    )
    image = None
    if s.openai_api_key and s.openai_image_model:
        image = OpenAIImageProvider(
            key(s.openai_api_key), s.openai_image_model, s.openai_base_url, client
        )
    return Orchestrator(
        s,
        Router([claude, openai, deepseek]),
        WorkspaceManager(s.workspace_root, s.data_dir / "worktrees"),
        AttachmentStore(s.data_dir / "media", s.max_attachment_bytes),
        ArtifactStore(s.data_dir / "artifacts"),
        QARunner(
            s.qa_commands, s.qa_timeout_s, s.qa_max_output_bytes, s.qa_sandbox, s.qa_docker_image
        ),
        ActionRegistry(s.action_ttl_s),
        image_provider=image,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_level, settings.secret_values())
    orch = build(settings)
    app.state.orch = orch
    app.state.settings = settings
    poller = None
    if settings.telegram_bot_token:
        from app.telegram_bot import TelegramGateway

        gw = TelegramGateway(settings, orch, orch.attachments, orch.actions)
        orch.notifier = gw
        poller = asyncio.create_task(gw.run_polling())
    else:
        log.warning("TELEGRAM_BOT_TOKEN not set: Telegram disabled, reports go to the log")
    yield
    if poller:
        poller.cancel()
        await asyncio.gather(poller, return_exceptions=True)


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
    description: str = Field(min_length=1, max_length=20_000)
    workflow: Workflow = Workflow.ask
    agent: str | None = None
    attachment_ids: list[str] = Field(default=[], max_length=20)


@app.post("/api/v1/tasks", status_code=201)
async def create_task(body: TaskIn, actor: Auth, o: Orch):
    try:
        task = await o.submit(
            body.description, body.workflow, actor, body.attachment_ids, body.agent
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return task


@app.get("/api/v1/tasks/{task_id}")
async def get_task(task_id: str, _: Auth, o: Orch):
    if task_id not in o.tasks:
        raise HTTPException(404, "unknown task")
    t = o.tasks[task_id]
    children = [c for c in o.tasks.values() if c.parent_id == task_id]
    return {"task": t, "steps": children}


@app.get("/api/v1/tasks/{task_id}/artifacts")
async def task_artifacts(task_id: str, _: Auth, o: Orch):
    if task_id not in o.tasks:
        raise HTTPException(404, "unknown task")
    # Metadata only; files are not served over HTTP.
    return [a.model_dump(exclude={"path"}) for a in o._all_artifacts(o.tasks[task_id])]


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
async def execute_action(action_id: str, actor: Auth, o: Orch):
    return {"result": await o.apply_action(action_id, actor)}
