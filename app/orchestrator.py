"""Generic task service: intake -> route -> plan -> validate -> workflow engine -> gates -> report.

Interfaces (Telegram, REST) only call the public methods here. Domain behaviour lives in
plugins; execution lives in the engine and the tool executor.
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Protocol

import jsonschema

from app import formatting as fmt
from app.actions import Action, ActionError, ActionRegistry
from app.builtin_tools import register_builtin_tools
from app.capabilities import CapabilityManifest, CapabilityRegistry, CapStatus
from app.config import Settings
from app.connectors.base import (
    AgentConnector,
    AgentRequest,
    AgentResult,
    Cap,
    ConnectorError,
    ImageGenerationProvider,
    UnsupportedCapability,
)
from app.engine import StepError, StepOutput, WorkflowEngine
from app.intake import TaskRequest
from app.logs import correlation_id
from app.models import (
    ART_TASK_TYPES,
    TERMINAL,
    AgentProfile,
    AgentSession,
    Task,
    TaskState,
    UsageRecord,
    Workflow,
    new_id,
    now,
)
from app.planner import PlanError, Planner
from app.plugins.base import PluginManager
from app.qa import QARunner
from app.router import Router
from app.storage import ArtifactStore, AttachmentError, AttachmentStore
from app.store import Store
from app.switches import Switches
from app.tools import ToolContext, ToolExecutionRecord, ToolExecutor, ToolRegistry, ToolStatus
from app.workflow import (
    STEP_BAD,
    GateResult,
    InvalidWorkflow,
    ResultEnvelope,
    StepDef,
    StepRecord,
    StepState,
    WorkflowRun,
)
from app.workspace import Workspace, WorkspaceError, WorkspaceManager

log = logging.getLogger(__name__)

CAP_NAMES = {  # abstract capability -> connector Cap
    "model.text": Cap.text,
    "model.vision": Cap.image_input,
    "model.files": Cap.file_input,
    "model.structured": Cap.structured_output,
    "model.tool_calling": Cap.tool_calling,
    "workspace.read": Cap.workspace_read,
    "workspace.write": Cap.workspace_write,
    "terminal": Cap.terminal,
    "session.resume": Cap.session_resume,
}


class Rejected(StepError):
    """User rejected (or let expire) an approval or clarification."""

    def __init__(self, message: str = "rejected by user", expired: bool = False):
        super().__init__(message, "expired" if expired else "approval_denied", outcome="cancelled")


class Notifier(Protocol):
    async def report(
        self,
        task: Task,
        html: str,
        *,
        actions: list[Action] = (),
        files: list[tuple[str, bytes]] = (),
        photos: list[tuple[str, bytes]] = (),
        control: bool = False,
    ) -> None: ...


class LogNotifier:
    async def report(self, task, html, *, actions=(), files=(), photos=(), control=False):
        log.info("report %s: %s", task.task_id, fmt.plain(html)[:300])


class StepContext:
    """The only handle a plugin operation/validator gets. Everything goes through policy."""

    def __init__(
        self, svc: "Orchestrator", task: Task, run: WorkflowRun | None, step: StepDef | None
    ):
        self._svc = svc
        self.task = task
        self.run = run
        self.step = step
        self.task_id = task.task_id
        self.objective = task.description
        self.request = TaskRequest.model_validate(task.request) if task.request else None

    @property
    def settings(self) -> Settings:
        return self._svc.s

    def plugin_config(self, plugin_id: str) -> dict:
        return self._svc.s.plugin_config.get(plugin_id, {})

    def workspace(self) -> Path:
        return self._svc._scratch(self.task_id)

    def read_input(self, ref: str) -> tuple[str, str, bytes]:
        return self._svc.read_input(self.task, ref)

    def add_artifact(self, kind: str, filename: str, data: bytes | str, **meta):
        art = self._svc.artifacts.add(
            self.task_id,
            kind,
            filename,
            data,
            step_id=self.step.id if self.step else None,
            producer=meta.pop("producer", self.step.ref if self.step else None),
            **meta,
        )
        return art

    def artifact(self, artifact_id: str):
        art = self._svc.artifacts.get(artifact_id)
        if art is None or not self._svc.artifact_ok(self.task, artifact_id):
            raise StepError(
                f"artifact {artifact_id} not available to this task", "missing_artifact"
            )
        return art

    def read_artifact(self, artifact_id: str) -> bytes:
        return self._svc.artifacts.read(self.artifact(artifact_id))

    async def run_tool(self, name: str, args: dict) -> ToolExecutionRecord:
        step = self.step or StepDef(id="adhoc", executor="tool", ref=name)
        return await self._svc._invoke_tool(self.task, self.run, step, name, args, approved=False)

    async def approval(self, html: str, photos=()) -> None:
        await self._svc._approval(self.task, html, photos)

    async def notify(self, html: str, kind: str = "info") -> None:
        await self._svc._notify(self.task, html, kind)

    def agents(self, extra_system: str = ""):
        from app.workspace_agents import WorkspaceAgents

        return WorkspaceAgents(self._svc, extra_system)

    @property
    def svc(self) -> "Orchestrator":
        """Escape hatch for core-shipped plugins composing workspace agents."""
        return self._svc


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        router: Router,
        workspaces: WorkspaceManager,
        attachments: AttachmentStore,
        artifacts: ArtifactStore,
        qa: QARunner,
        actions: ActionRegistry,
        image_provider: ImageGenerationProvider | None = None,
        notifier: Notifier | None = None,
        *,
        store: Store | None = None,
        tools: ToolRegistry | None = None,
        plugins: list[str] | None = None,
        gateway_cfg=None,
        image_on: bool = True,
    ):
        self.s = settings
        self.gateway_cfg = gateway_cfg  # app.gateway_config.GatewayConfig (validated) or None
        self.router = router
        self.workspaces = workspaces
        self.store = store or Store(":memory:")
        self.attachments = attachments
        self.artifacts = artifacts
        if self.attachments.store is None:
            self.attachments.store = self.store
        if self.artifacts.store is None:
            self.artifacts.store = self.store
        self.qa = qa
        self.actions = actions
        self._image_provider = image_provider
        self._model3d_provider = None  # Meshy, set by app.main.build
        self.switches = Switches(self.store, {"generate_art": image_on})
        self.router.switches = self.switches
        self.notifier: Notifier = notifier or LogNotifier()
        self.tasks: dict[str, Task] = {}
        self.sessions: dict[str, AgentSession] = {}
        self._running: dict[str, asyncio.Task] = {}
        self._approvals: dict[str, asyncio.Future] = {}  # approval_id -> future
        self._answers: dict[str, asyncio.Future] = {}  # task_id -> clarification future
        self._active_calls: dict[str, AgentConnector] = {}
        self._worktrees: dict[str, Workspace] = {}
        self._ws_locks: dict[str, asyncio.Lock] = {}
        self._sem = asyncio.Semaphore(settings.max_concurrent_tasks)
        self.caps = CapabilityRegistry()
        self.tools = tools or ToolRegistry()
        self.executor = ToolExecutor(self.tools, publisher=self, max_concurrent=4)
        if tools is None:
            register_builtin_tools(self.tools, qa)
        self.plugins = PluginManager(frozenset(settings.plugin_permissions))
        trusted = set(gateway_cfg.policies.trusted_plugin_modules) if gateway_cfg else None
        for p in plugins if plugins is not None else settings.plugins:
            module = p if "." in p else f"app.plugins.{p}"
            if trusted is not None and module not in trusted:  # trusted plugin-loading policy
                from app.plugins.base import LoadedPlugin

                self.plugins.loaded[module] = LoadedPlugin(
                    None, None, module, error="module not in policies.trusted_plugin_modules"
                )
                continue
            self.plugins.load(module, self, self.tools)
        self._register_capabilities()
        from app.agent_registry import AgentCapabilityRegistry
        from app.collaboration import CollaborationService

        self.collab = CollaborationService(self)
        from app.channel import ChannelHub

        self.channel = ChannelHub(self)  # work pushed/pulled into open Claude Code sessions
        from app.asks import AskService

        self.asks = AskService(self)  # agent-to-agent requests, operator as fallback
        self.registry = AgentCapabilityRegistry(gateway_cfg, self) if gateway_cfg else None
        self.planner = Planner(
            self.plugins,
            self.caps,
            self.tools,
            settings.plugin_config,
            gateway_cfg=gateway_cfg,
            capability_check=self._capability_check,
        )
        if self.registry:
            self.registry.publish_status()
        self.engine = WorkflowEngine(
            self,
            {
                "tool": self._exec_tool,
                "agent": self._exec_agent,
                "plugin": self._exec_plugin,
                "validator": self._exec_validator,
                "internal": self._exec_internal,
                "approval": self._exec_approval,
                "capability": self._exec_capability,
            },
        )

    # ---------- capability registry ----------

    @property
    def image_provider(self) -> ImageGenerationProvider | None:
        """None while generate_art is switched off (Telegram /mcp): every image path refuses."""
        return self._image_provider if self.switches.on("generate_art") else None

    @image_provider.setter
    def image_provider(self, provider: ImageGenerationProvider | None) -> None:
        self._image_provider = provider

    @property
    def model3d_provider(self):
        """The 3D provider (Meshy); None while generate_art is switched off (/mcp)."""
        return self._model3d_provider if self.switches.on("generate_art") else None

    @model3d_provider.setter
    def model3d_provider(self, provider) -> None:
        self._model3d_provider = provider

    def plan_for(self, task: Task, role: str | None) -> list[str]:
        """The project's executors for this workflow role (Telegram /plan), tried first."""
        from app import catalog

        return catalog.plan(self.store, self.project_of(task)).get(role, []) if role else []

    def register_connectors(self) -> None:
        for c in self.router.connectors.values():
            provides = [k for k, v in CAP_NAMES.items() if v in c.caps]
            self.caps.register(
                CapabilityManifest(
                    id=self._cap_id(c),
                    kind=getattr(c, "kind", "provider"),
                    name=c.name,
                    provides=provides,
                    operations=["execute", "cancel"],
                    backend=type(c).__name__,
                    status=CapStatus.validated if c.available else CapStatus.unavailable,
                    evidence="configured; no real execution yet"
                    if c.available
                    else "not configured",
                ),
                health=getattr(c, "health_status", None),
            )

    def _register_capabilities(self) -> None:
        self.register_connectors()
        for t in self.tools.list():
            self.register_tool_capability(t)
        for lp in self.plugins.loaded.values():
            if lp.manifest:
                self.caps.register(
                    CapabilityManifest(
                        id=f"plugin:{lp.manifest.id}",
                        kind="plugin",
                        name=lp.manifest.name,
                        description=lp.manifest.description,
                        version=lp.manifest.version,
                        category=",".join(lp.manifest.categories),
                        operations=[o.task_type for o in lp.manifest.operations],
                        artifact_types=lp.manifest.artifact_types,
                        permissions=lp.manifest.permissions,
                        status=CapStatus.validated,
                        evidence=f"manifest valid; tools: {lp.tool_names}",
                    )
                )
            else:
                self.caps.register(
                    CapabilityManifest(
                        id=f"plugin:{lp.module.rsplit('.', 1)[-1]}",
                        kind="plugin",
                        name=lp.module,
                        status=CapStatus.unavailable,
                        evidence=lp.error or "failed to load",
                    )
                )
        if self.image_provider:
            self.caps.register(
                CapabilityManifest(
                    id=f"image_provider:{self.image_provider.name}",
                    kind="image_provider",
                    name=self.image_provider.name,
                    provides=["image.generate"],
                    version=self.image_provider.model,
                    status=CapStatus.configured,
                    evidence="API key and model configured; not yet executed",
                ),
                health=getattr(self.image_provider, "health_status", None),
            )

    def register_tool_capability(self, t) -> None:
        self.caps.register(
            CapabilityManifest(
                id=f"tool:{t.name}",
                kind="tool",
                name=t.name,
                description=t.description,
                category=t.source,
                input_schema=t.input_schema,
                output_schema=t.output_schema,
                backend=t.environment,
                permissions=sorted(t.permissions),
                side_effects=t.side_effects,
                timeout_s=t.timeout_s,
                provides=[f"tool:{t.name}"],
                status=CapStatus.validated,
                evidence="registered; input schema valid",
            )
        )

    async def start(self) -> None:
        """Async startup: MCP discovery, health checks, then recovery of interrupted work."""
        await self.discover_mcp()
        await self.refresh_health()
        await self.recover()

    async def discover_mcp(self) -> list[dict]:
        from app.mcp_client import McpServerConfig, discover, register_mcp_tools

        out = []
        servers = list(self.s.mcp_servers)
        if self.gateway_cfg:
            servers += [
                m.model_dump(exclude={"enabled"})
                for m in self.gateway_cfg.mcp_servers.values()
                if m.enabled
            ]
        for raw in servers:
            cfg = McpServerConfig.model_validate(raw)
            found = await discover(cfg)
            names = register_mcp_tools(self.tools, cfg, found) if found.ok else []
            for n in names:
                self.register_tool_capability(self.tools.get(n))
            self.caps.register(
                CapabilityManifest(
                    id=f"mcp_server:{cfg.name}",
                    kind="mcp_server",
                    name=cfg.name,
                    version=f"{found.server_name} {found.server_version}" if found.ok else None,
                    operations=[t["name"] for t in found.tools][:200],
                    backend="mcp",
                    status=CapStatus.validated
                    if names
                    else (CapStatus.discovered if found.ok else CapStatus.unavailable),
                    evidence=(
                        f"tools/list returned {len(found.tools)} tools; enabled: {names}"
                        if found.ok
                        else f"connection failed: {found.error}"
                    ),
                )
            )
            out.append(
                {
                    "server": cfg.name,
                    "ok": found.ok,
                    "tools": len(found.tools),
                    "enabled": names,
                    "error": found.error,
                }
            )
        return out

    async def refresh_health(self) -> dict:
        res = await self.caps.check_health()
        self._sync_router()
        if self.registry:
            self.registry.publish_status()
        return res

    @staticmethod
    def _cap_id(conn) -> str:
        return f"{getattr(conn, 'kind', 'provider')}:{conn.name}"

    def _capability_check(self, step: StepDef) -> str | None:
        """Planner hook: None when a capability step has an eligible executor, else the reason."""
        from app.agent_registry import CapabilityError

        if self.registry is None:
            return "capability steps need a gateway configuration (CONFIG_DIR)"
        try:
            self.registry.select(step.ref, step.agent)
        except CapabilityError as e:
            return f"{e.code}: {e} ({'; '.join(e.details)})"
        return None

    def reload_config(self, cfg) -> None:
        """Swap to a validated configuration. Running workflows keep their persisted definitions."""
        from app.agent_registry import AgentCapabilityRegistry

        self.gateway_cfg = cfg
        self.registry = AgentCapabilityRegistry(cfg, self)
        self.planner.gateway_cfg = cfg
        self.registry.publish_status()
        self.store.event("config.reloaded", agents=len(cfg.agents), workflows=len(cfg.workflows))

    def _sync_router(self) -> None:
        for c in self.router.connectors.values():
            c.available = self.caps.routable(self._cap_id(c))

    # ---------- profiles ----------

    def profiles(self) -> list[AgentProfile]:
        s = self.s
        return [
            AgentProfile(
                role="designer",
                label="Designer",
                description="References -> spec, prompts, assets",
                preference=s.designer_preference.split(","),
            ),
            AgentProfile(
                role="implementer",
                label="Implementer",
                description="Changes in an isolated workspace",
                preference=s.implementer_preference.split(","),
            ),
            AgentProfile(
                role="reviewer",
                label="Reviewer",
                description="Independent review of diff, tests and artifacts",
                preference=s.reviewer_preference.split(","),
            ),
        ]

    def _pref(self, role: str) -> list[str]:
        for p in self.profiles():
            if p.role == role:
                return p.preference
        return self.s.designer_preference.split(",")

    # ---------- public API ----------

    async def submit(
        self,
        description: str,
        workflow: Workflow = Workflow.ask,
        created_by: str = "",
        attachment_ids: list[str] = (),
        agent: str | None = None,
        title: str | None = None,
        retry_of: Task | None = None,
    ) -> Task:
        """Legacy entry point: maps a Phase 1 workflow onto a generic TaskRequest."""
        if retry_of is not None:
            req = TaskRequest.model_validate(retry_of.request)
            return await self.submit_request(req, title=title, retry_of=retry_of)
        req = TaskRequest.from_legacy(description, workflow, created_by, attachment_ids, agent)
        if workflow != Workflow.ask and not self.workspaces.enabled:
            raise UnsupportedCapability(f"workflow {workflow} needs WORKSPACE_ROOT configured")
        return await self.submit_request(req, title=title, workflow=Workflow(workflow))

    async def submit_request(
        self,
        req: TaskRequest,
        title: str | None = None,
        retry_of: Task | None = None,
        workflow: Workflow | None = None,
    ) -> Task:
        if req.idempotency_key and retry_of is None:
            existing = self.store.claim_idempotency(req.idempotency_key, "")
            if existing is not None:
                prior = self._find_task_by_key(req.idempotency_key)
                if prior is not None:
                    return prior
        task = Task(
            title=title or fmt.truncate(req.objective.splitlines()[0], 60),
            description=req.objective,
            workflow=workflow or Workflow.ask,
            agent=req.agent,
            created_by=req.user_id,
            domain=req.domain,
            task_type=req.task_type,
        )
        if req.correlation_id:
            task.correlation_id = req.correlation_id
        if retry_of:
            task.attempt = retry_of.attempt + 1
            task.correlation_id = retry_of.correlation_id
            task.attachment_ids = list(retry_of.attachment_ids)
        else:
            for att_id in req.attachment_ids:
                self.attachments.bind(att_id, task.task_id)
            task.attachment_ids = list(req.attachment_ids)
        for art_id in req.artifact_ids:  # input artifacts must be readable by this user's tasks
            if self.artifacts.get(art_id) is None:
                raise AttachmentError(f"unknown input artifact {art_id}")
        task.request = req.model_dump(mode="json")
        self.tasks[task.task_id] = task
        if req.idempotency_key:
            self.store.put("idem", req.idempotency_key, {"task_id": task.task_id})
        self._set_state(task, TaskState.queued)
        self._running[task.task_id] = asyncio.create_task(self._run(task))
        return task

    def register_external(
        self, title: str, user_id: str, project: str, session_id: str | None = None
    ) -> Task:
        """Record-only task for work done outside the gateway (e.g. a Claude Code session in another
        repo), so it can publish reports and receive Telegram feedback. No workflow runs."""
        task = Task(title=fmt.truncate(title, 60), description=title, created_by=user_id)
        task.request = {
            "objective": title,
            "user_id": user_id,
            "source": "external",
            "data": {"project": project, "session_id": session_id},
        }
        self.tasks[task.task_id] = task
        for state in (TaskState.queued, TaskState.running, TaskState.succeeded):
            self._set_state(task, state)
        return task

    # ---------- generated-art review ----------

    def art_review(self, task_id: str) -> dict | None:
        """pending -> accepted | changes_requested (then next_task_id is the regenerated version)."""
        return self.store.get("art_review", task_id)

    def _set_review(self, task_id: str, **kw) -> dict:
        rec = {**(self.art_review(task_id) or {"task_id": task_id}), **kw, "at": now().isoformat()}
        self.store.put("art_review", task_id, rec, task_id=task_id, state=rec["state"])
        self.store.event("art.review", task_id=task_id, state=rec["state"])
        return rec

    async def regenerate_art(self, src: Task, feedback: str, user_id: str) -> Task:
        """The operator's Feedback on generated art: same brief plus the requested changes."""
        data = dict((src.request or {}).get("data") or {})
        base = data.setdefault("base_prompt", data.get("prompt", ""))
        revisions = [*data.get("revisions", []), feedback.strip()[:500]]
        data["revisions"] = revisions
        rev = ("\n\nRevision requests from the operator (apply all, newest last; keep everything else the "
               "same):\n- " + "\n- ".join(revisions[-5:]))  # fmt: skip
        limit = 790 if src.task_type == "design_model3d" else 3900  # Meshy takes 800 characters
        data["prompt"] = fmt.truncate(base.rstrip(), max(100, limit - len(rev))) + fmt.truncate(
            rev, limit - 100
        )
        data.update(revision_of=src.task_id, operator_requested=True)
        new = await self.submit_request(
            TaskRequest(
                objective=f"Art revision: {feedback.strip()[:120]}",
                task_type=src.task_type,
                data=data,
                attachment_ids=list(src.attachment_ids),  # e.g. the reference image of a 3D model
                user_id=user_id,
                parent_task_id=src.task_id,
                correlation_id=src.correlation_id,
            )  # fmt: skip
        )
        self._set_review(src.task_id, state="changes_requested", next_task_id=new.task_id,
                         feedback=feedback.strip()[:500])  # fmt: skip
        return new

    def _find_task_by_key(self, key: str) -> Task | None:
        rec = self.store.get("idem", key)
        return self.tasks.get(rec["task_id"]) if rec else None

    async def stop(self, ref: str) -> bool:
        task = self.tasks.get(ref) or self._task_for_session(ref)
        if task is None:
            raise KeyError(ref)
        if task.done:
            return False
        self.store.event(
            "cancellation.requested", task_id=task.task_id, correlation_id=task.correlation_id
        )
        for child in [t for t in self.tasks.values() if t.parent_id == task.task_id] + [task]:
            conn = self._active_calls.get(child.task_id)
            if conn:
                await conn.cancel(child.task_id)
        for fut in [*self._task_approvals(task.task_id), self._answers.get(task.task_id)]:
            if fut and not fut.done():
                fut.cancel()
        runner = self._running.get(task.task_id)
        if runner:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
        return True

    def _task_approvals(self, task_id: str) -> list[asyncio.Future]:
        return [
            f
            for aid, f in self._approvals.items()
            if (self.store.get("approval", aid) or {}).get("task_id") == task_id
        ]

    async def retry(self, task_id: str) -> Task:
        task = self.tasks[task_id]
        if task.state not in {
            TaskState.failed,
            TaskState.cancelled,
            TaskState.unknown,
            TaskState.expired,
        }:
            raise ValueError(f"task is {task.state}; only failed/cancelled/unknown can be retried")
        if task.parent_id:
            raise ValueError("retry the parent task instead of a workflow step")
        if task.attempt > self.s.max_retries:
            raise ValueError(f"retry limit reached ({self.s.max_retries})")
        return await self.submit(
            task.description,
            task.workflow,
            task.created_by,
            agent=task.agent,
            title=task.title,
            retry_of=task,
        )

    async def answer(self, task_id: str, text: str, actor: str) -> str:
        fut = self._answers.get(task_id)
        task = self.tasks.get(task_id)
        if task is None or fut is None or fut.done() or task.state != TaskState.waiting_for_user:
            raise ActionError("task is not waiting for an answer")
        text = text.strip()
        if not text:
            raise ActionError("empty answer")
        self.store.event("clarification.answered", task_id=task_id, actor=str(actor))
        fut.set_result(text)
        return "Answer received"

    async def apply_action(self, token: str, actor: int | str) -> str:
        a = self.actions.get(token)
        if a is None:
            raise ActionError("unknown or expired action")
        task = self.tasks.get(a.task_id)
        if task is None:
            raise ActionError("task no longer exists")
        a = self.actions.consume(token, actor, task.state)
        log.info("action %s on %s by %s", a.kind, task.task_id, actor)
        if a.kind in ("approve", "reject"):
            return self.decide_approval(
                a.payload.get("approval_id", ""), a.kind == "approve", actor
            )
        if a.kind == "answer":
            return await self.answer(task.task_id, a.payload["text"], str(actor))
        if a.kind == "stop":
            await self.stop(task.task_id)
            return "Stopped"
        if a.kind == "retry":
            new = await self.retry(task.task_id)
            return f"Retrying as {new.task_id}"
        if a.kind == "art_accept":
            self._set_review(task.task_id, state="accepted", by=str(actor))
            return "Accepted: the agent can use this image"
        if a.kind == "art_feedback":
            return (
                "Reply to the image post with what to change; the new version comes back for review"
            )
        if a.kind == "view_report":
            body, files = self._report_message(task)
            await self._notify(task, body, fmt.STATE_KIND.get(task.state, "info"), files)
            return "Report sent"
        if a.kind == "review_diff":
            diffs = [x for x in self._all_artifacts(task) if x.kind == "diff"]
            if not diffs:
                return "No diff recorded"
            files = [(d.filename, d.path.read_bytes()) for d in diffs]
            await self._notify(task, f"{len(files)} diff file(s) attached", "review", files)
            return "Diff sent"
        raise ActionError(f"action {a.kind!r} is not implemented")

    def decide_approval(self, approval_id: str, approved: bool, actor: int | str) -> str:
        rec = self.store.get("approval", approval_id)
        fut = self._approvals.get(approval_id)
        if rec is None or fut is None or fut.done() or rec["state"] != "pending":
            raise ActionError("no pending approval with that id")
        rec.update(
            state="approved" if approved else "rejected",
            decided_by=str(actor),
            decided_at=now().isoformat(),
        )
        self.store.put(
            "approval",
            approval_id,
            rec,
            task_id=rec["task_id"],
            run_id=rec.get("run_id"),
            state=rec["state"],
        )
        self.store.event(
            "approval." + ("granted" if approved else "denied"),
            task_id=rec["task_id"],
            run_id=rec.get("run_id"),
            step_id=rec.get("step_id"),
            by=str(actor),
        )
        fut.set_result((approved, str(actor)))
        return "Approved" if approved else "Rejected"

    def pending_approvals(self, task_id: str | None = None) -> list[dict]:
        return [
            a
            for a in self.store.list("approval", task_id=task_id, states=["pending"])
            if a["approval_id"] in self._approvals
        ]

    # ---------- reporting ----------

    def envelope(self, task: Task) -> ResultEnvelope:
        run = self.load_run(task.run_id) if task.run_id else None
        recs = self.load_steps(task.run_id) if task.run_id else {}
        req = task.request or {}
        validations = [
            GateResult.model_validate(r.output)
            for r in recs.values()
            if isinstance(r.output, dict) and "validator_id" in r.output
        ]
        evidence = [
            {
                "step": r.step_id,
                "state": r.state,
                "executor": r.executor_ref,
                "attempts": r.attempt,
                "duration_s": r.duration_s,
                "error": r.error,
            }
            for r in recs.values()
        ]
        limitations = list(req.get("_limitations", []))
        if run and run.compensation:
            limitations.append(f"compensation: {run.compensation}")
        return ResultEnvelope(
            task_id=task.task_id,
            domain=task.domain,
            operation=task.task_type,
            status=("partial" if run and run.state == "partial" else task.state.value),
            summary=fmt.plain(task.result or task.error or "")[:2000],
            artifacts=[
                {
                    "artifact_id": a.artifact_id,
                    "kind": a.kind,
                    "filename": a.filename,
                    "sha256": a.sha256,
                    "size_bytes": a.size_bytes,
                    "mime_type": a.mime_type,
                    "producer": a.producer,
                    "step_id": a.step_id,
                    "validation_status": a.validation_status,
                }
                for a in self._all_artifacts(task)
            ],
            validations=validations,
            evidence=evidence,
            failed_steps=[r.step_id for r in recs.values() if r.state in STEP_BAD],
            skipped_steps=[r.step_id for r in recs.values() if r.state == StepState.skipped],
            limitations=limitations,
            routing=req.get("_routing", {}),
            next_actions=(["retry"] if task.state in (TaskState.failed, TaskState.unknown) else []),
        )

    def report(self, task: Task) -> str:
        parts = [fmt.task_summary(task)]
        req = task.request or {}
        if req.get("_routing"):
            r = req["_routing"]
            parts.append(f"Routing: {r.get('mode')} → {r.get('plugin_id')}/{r.get('task_type')}")
        if task.run_id:
            recs = self.load_steps(task.run_id)
            parts.append(
                "<b>Workflow steps</b>\n" + "\n".join(_step_line(r) for r in recs.values())
            )
        children = [t for t in self.tasks.values() if t.parent_id == task.task_id]
        if children:
            parts.append("<b>Agent steps</b>\n" + "\n".join(fmt.task_line(c) for c in children))
        findings = [f for c in [task, *children] for f in c.findings]
        if findings:
            parts.append(fmt.findings_report(findings))
        arts = self._all_artifacts(task)
        if arts:
            parts.append(
                "<b>Artifacts</b>\n"
                + fmt.bullet(
                    [
                        f"{a.kind}: {fmt.escape(a.filename)} <code>{a.artifact_id}</code>"
                        for a in arts
                    ]
                )
            )
        return "\n\n".join(parts)

    # ---------- task lifecycle ----------

    def _set_state(self, task: Task, state: TaskState) -> None:
        if task.state == state:
            return
        task.transition(state)
        self.store.put(
            "task", task.task_id, task, task_id=task.task_id, run_id=task.run_id, state=state.value
        )
        self.store.event(
            "task.state",
            task_id=task.task_id,
            run_id=task.run_id,
            correlation_id=task.correlation_id,
            state=state.value,
        )

    def _finish(self, task: Task, state: TaskState) -> None:
        if not task.done:
            self._set_state(task, state)

    def _finish_children(self, task: Task) -> None:
        for c in self.tasks.values():
            if c.parent_id == task.task_id and not c.done:
                c.error = c.error or f"parent {task.state}"
                self._set_state(c, TaskState.cancelled)

    def _attachment_meta(self, task: Task) -> list[dict]:
        out = []
        for att_id in task.attachment_ids:
            a = self.attachments.get(att_id)
            out.append(
                {
                    "id": a.attachment_id,
                    "filename": a.filename,
                    "mime_type": a.mime_type,
                    "size_bytes": a.size_bytes,
                }
            )
        for art_id in (task.request or {}).get("artifact_ids", []):
            a = self.artifacts.get(art_id)
            if a:
                out.append(
                    {
                        "id": a.artifact_id,
                        "filename": a.filename,
                        "mime_type": a.mime_type,
                        "size_bytes": a.size_bytes,
                    }
                )
        return out

    async def _run(self, task: Task, resume: tuple[WorkflowRun, dict] | None = None) -> None:
        token = correlation_id.set(task.correlation_id)
        try:
            if resume is None:
                run = await self._plan(task)
                if run is None:
                    return
                self._set_state(task, TaskState.running)
                run = await self.engine.execute(task, run)
            else:
                run, recs = resume
                run = await self.engine.execute(task, run, recs)
            await self._conclude(task, run)
        except (asyncio.CancelledError, Rejected) as e:
            task.error = str(e) if isinstance(e, Rejected) else "cancelled"
            expired = isinstance(e, Rejected) and e.category == "expired"
            task.error_category = (
                "expired"
                if expired
                else ("approval_denied" if isinstance(e, Rejected) else "cancelled")
            )
            self._finish(task, TaskState.expired if expired else TaskState.cancelled)
        except (PlanError, InvalidWorkflow, UnsupportedCapability) as e:
            task.error = str(e)
            task.error_category = (
                "unsupported" if not isinstance(e, InvalidWorkflow) else "invalid_workflow"
            )
            self._finish(task, TaskState.failed)
        except ConnectorError as e:
            task.error, task.error_category = str(e), e.category
            self._finish(task, TaskState.unknown if e.outcome == "unknown" else TaskState.failed)
        except (WorkspaceError, AttachmentError, ValueError) as e:
            task.error, task.error_category = str(e), type(e).__name__
            self._finish(task, TaskState.failed)
        except Exception as e:
            log.exception("task %s crashed", task.task_id)
            task.error, task.error_category = f"internal error: {type(e).__name__}", "internal"
            self._finish(task, TaskState.failed)
        finally:
            self._running.pop(task.task_id, None)
            self._finish_children(task)
            self.store.put(
                "task",
                task.task_id,
                task,
                task_id=task.task_id,
                run_id=task.run_id,
                state=task.state.value,
            )
            log.info("task finished", extra={"task_id": task.task_id, "state": task.state})
            if task.done:
                await self._final_report(task)
            correlation_id.reset(token)

    async def _final_report(self, task: Task) -> None:
        group = new_id("grp")
        acts = [self.actions.create("view_report", task.task_id, "View report", TERMINAL)]
        if task.state != TaskState.succeeded and not task.parent_id:
            acts.append(self.actions.create("retry", task.task_id, "Retry", TERMINAL, group))
        if any(a.kind == "diff" for a in self._all_artifacts(task)):
            acts.append(self.actions.create("review_diff", task.task_id, "Review diff", TERMINAL))
        env = self.envelope(task)
        self.artifacts.add(
            task.task_id, "envelope", "result.json", env.model_dump_json(indent=2), producer="core"
        )
        photos = self._final_photos(task)
        reply = "Reply to this post to send feedback"
        if task.task_type in ART_TASK_TYPES and task.state == TaskState.succeeded and photos:
            acts += [  # the operator reviews generated art before an agent may use it
                self.actions.create("art_accept", task.task_id, "✅ Accept", TERMINAL),
                self.actions.create("art_feedback", task.task_id, "✏️ Feedback", TERMINAL),
            ]
            self._set_review(task.task_id, state="pending")
            reply = (
                "✅ Accept, or reply to this post with what to change (a new version is generated)"
            )
        run = self.load_run(task.run_id) if task.run_id else None
        if (
            run is not None
            and not run.workflow.final_report
            and task.state == TaskState.succeeded
            and not photos
        ):  # the workflow's own message is the report; a second "done" post is noise
            self.store.event(
                "task.final_report", task_id=task.task_id, delivered=False, skipped="opt_out"
            )
            return
        body = self.msg(
            fmt.STATE_KIND.get(task.state, "info"),
            task,
            self._final_body(task, len(photos)),
            title=task.title,
            reply=reply,
        )
        html, overflow = fmt.fit(body)
        files = [(f"{task.task_id}_report.txt", overflow.encode())] if overflow else []
        rep = await self.collab.publish_final(task, html, photos, files, acts)
        self.store.event(
            "task.final_report",
            task_id=task.task_id,
            correlation_id=task.correlation_id,
            report_id=rep.report_id,
            delivered=rep.telegram.channel_message_id is not None,
            photos=len(photos),
            state=task.state.value,
        )

    def _final_photos(self, task: Task) -> list[tuple[str, bytes, str]]:
        """Images produced by the task (and its steps), newest first; failed validations excluded."""
        limit = self.gateway_cfg.telegram.max_report_photos if self.gateway_cfg else 4
        out = []
        arts = sorted(self._all_artifacts(task), key=lambda a: a.created_at, reverse=True)
        for a in arts:
            if len(out) >= limit:
                break
            if not (a.mime_type or "").startswith("image/") or a.validation_status == "failed":
                continue
            try:
                out.append((a.filename, self.artifacts.read(a), a.artifact_id))
            except (AttachmentError, OSError):
                continue  # unreadable or changed on disk: never send it
        return out

    def _final_body(self, task: Task, n_photos: int) -> str:
        """Only what the reader needs: outcome, what failed, evidence counts. Full detail: View report."""
        lines = []
        if task.error:
            lines.append(
                f"<b>Error</b> [{fmt.escape(task.error_category or 'error')}]: "
                f"{fmt.escape(fmt.truncate(task.error, 600))}"
            )
        if task.result:
            lines.append(fmt.escape(fmt.truncate(task.result.strip(), 1500)))
        if task.run_id:
            bad = [r for r in self.load_steps(task.run_id).values() if r.state in STEP_BAD]
            lines += [_step_line(r) for r in bad]
        wf = (task.request or {}).get("_routing", {}).get("workflow_id")
        secs = int((task.updated_at - task.created_at).total_seconds())
        meta = [
            f"⏱ {secs // 60}m {secs % 60}s",
            f"verified: {self.collab.verified_status(task.task_id)}",
        ]
        if wf:
            meta.insert(0, f"workflow <code>{fmt.escape(wf)}</code>")
        if n_photos:
            meta.append(f"🖼 {n_photos}")
        return "\n".join([*lines, "", " · ".join(meta)]).strip()

    async def _plan(self, task: Task) -> WorkflowRun | None:
        self._set_state(task, TaskState.planning)
        req = TaskRequest.model_validate(task.request)
        atts = self._attachment_meta(task)
        env_args = {
            "qa_checks": list(self.qa.commands),
            "workspace_configured": self.workspaces.enabled,
            "attachments": atts,
        }
        decision = self.planner.route(req, atts)
        for _ in range(3):  # bounded clarification rounds
            task.request["_routing"] = {**decision.__dict__}
            self.store.event("task.routed", task_id=task.task_id, **decision.__dict__)
            result = self.planner.plan(req, decision, env_args)
            if result.question is None:
                break
            answer = await self._clarify(task, result.question, result.options)
            decision = self._apply_answer(req, decision, answer)
        else:
            raise PlanError("could not determine the task after 3 clarification rounds")
        if result.limitations:
            task.request["_limitations"] = result.limitations
        wf = self.planner.validate(result.workflow, req, self._executor_known)
        task.domain, task.task_type = wf.domain, decision.task_type or wf.id
        if decision.plugin_id and decision.plugin_id != "general":
            task.domain = decision.plugin_id
        run = WorkflowRun(
            task_id=task.task_id,
            workflow=wf,
            inputs={
                "objective": req.objective,
                "attachment_ids": task.attachment_ids,
                "artifact_ids": req.artifact_ids,
                "constraints": req.constraints,
                "output_format": req.output_format,
                **req.data,
            },
        )
        task.run_id = run.run_id
        self.store.put(
            "run", run.run_id, run, task_id=task.task_id, run_id=run.run_id, state="created"
        )
        self.store.event(
            "workflow.created",
            task_id=task.task_id,
            run_id=run.run_id,
            workflow=wf.id,
            version=wf.version,
            steps=[s.id for s in wf.steps],
        )
        return run

    def _apply_answer(self, req: TaskRequest, decision, answer: str):
        from app.planner import RoutingDecision

        text = answer.strip()
        if ":" in text and " " not in text:
            pid, _, ttype = text.partition(":")
            if pid == "general" or self.plugins.get(pid):
                req.domain, req.task_type = pid, ttype or None
                return RoutingDecision("explicit", pid, ttype or None, reasons=["user clarified"])
        if decision.plugin_id and self.plugins.get(decision.plugin_id) and " " not in text:
            req.task_type = text
            return RoutingDecision(
                "explicit", decision.plugin_id, text, reasons=["user chose operation"]
            )
        if self.plugins.get(text):
            req.domain = text
            return self.planner.route(req, [])
        req.objective = f"{req.objective}\n\nClarification: {text}"
        return self.planner.route(req, [])

    async def _clarify(self, task: Task, question: str, options: list[str]) -> str:
        fut = asyncio.get_running_loop().create_future()
        self._answers[task.task_id] = fut
        task.question = question
        self._set_state(task, TaskState.waiting_for_user)
        group = new_id("grp")
        acts = [
            self.actions.create(
                "answer",
                task.task_id,
                o[:30],
                {TaskState.waiting_for_user},
                group,
                payload={"text": o},
            )
            for o in options[:6]
        ]
        await self._safe_report(
            task,
            self.msg(
                "question",
                task,
                fmt.escape(question),
                title=task.title,
                reply=f"<code>/answer {task.task_id} </code>" + (" or a button" if acts else ""),
            ),  # fmt: skip
            actions=acts,
            control=True,
        )
        try:
            return await asyncio.wait_for(fut, self.s.action_ttl_s)
        except TimeoutError as e:
            raise Rejected("clarification expired", expired=True) from e
        finally:
            self._answers.pop(task.task_id, None)
            task.question = None
            self._set_state(task, TaskState.planning)

    async def _conclude(self, task: Task, run: WorkflowRun) -> None:
        self._set_state(task, TaskState.validating)
        recs = self.load_steps(run.run_id)
        for r in recs.values():  # aggregate provider/usage of agent steps onto the task
            if r.executor_ref and not task.provider:
                task.provider = r.executor_ref
        if task.result is None:
            summary = {k: v for k, v in run.outputs.items() if v is not None}
            task.result = json.dumps(summary, indent=2, default=str)[:3000] if summary else ""
        state = {
            "succeeded": TaskState.succeeded,
            "cancelled": TaskState.cancelled,
            "unknown": TaskState.unknown,
        }.get(run.state, TaskState.failed)
        if state == TaskState.cancelled and any(
            r.error_category == "expired" for r in recs.values()
        ):
            state = TaskState.expired
        if state != TaskState.succeeded and not task.error:
            bad = [r for r in recs.values() if r.state in STEP_BAD]
            task.error = run.error or (bad[0].error if bad else "workflow did not succeed")
            task.error_category = (
                "partial"
                if run.state == "partial"
                else (bad[0].error_category if bad else "criteria_failed")
            )
        if task.domain and task.domain != "general":
            self.caps.record_execution(
                f"plugin:{task.domain}",
                state == TaskState.succeeded,
                f"task {task.task_id} {state.value}",
            )
        self._finish(task, state)

    # ---------- engine host ----------

    def is_idempotent(self, step: StepDef) -> bool:
        if step.idempotent is False:
            return False
        if step.executor == "tool":
            t = self.tools.get(step.ref)
            return bool(t) and not t.side_effects
        if step.executor == "agent":
            return "workspace.write" not in step.required_capabilities
        if step.executor == "plugin":
            pid, _, name = step.ref.partition(".")
            lp = self.plugins.get(pid)
            return bool(lp) and name in lp.plugin.idempotent_operations()
        if step.executor == "capability":
            cap = self.registry.capabilities.get(step.ref) if self.registry else None
            if cap is None or cap.side_effects:
                return False
            for e in cap.executors:
                t = self.tools.get(e.ref) if e.type == "tool" else None
                if e.type == "runtime" or (t is not None and t.side_effects):
                    return False
            return True
        return True

    def needs_approval(self, step: StepDef) -> bool:
        from app.collaboration import policy_requires_approval

        if step.requires_approval or step.executor == "approval":
            return True
        rules = self.gateway_cfg.policies.approvals if self.gateway_cfg else []
        if step.executor == "tool":
            t = self.tools.get(step.ref)
            return bool(t and t.requires_approval) or policy_requires_approval(
                rules, [f"tool:{step.ref}"]
            )
        if step.executor == "capability" and self.registry:
            from app.agent_registry import CapabilityError, executor_id

            cap = self.registry.capabilities.get(step.ref)
            if cap is None:
                return False
            if cap.requires_approval or policy_requires_approval(rules, [f"capability:{cap.id}"]):
                return True
            try:
                spec = self.registry.select(step.ref, step.agent).spec
            except CapabilityError:
                return False
            t = self.tools.get(spec.ref) if spec.type == "tool" else None
            ids = [executor_id(spec), f"{spec.type}:{spec.ref}"]
            return bool(t and t.requires_approval) or policy_requires_approval(rules, ids)
        return step.executor == "agent" and "workspace.write" in step.required_capabilities

    def context_for(self, task: Task) -> dict:
        req = task.request or {}
        return {
            "objective": task.description,
            "constraints": req.get("constraints", []),
            "parent_task_id": req.get("parent_task_id"),
            **(req.get("data") or {}),
        }

    def artifact_ok(self, task: Task, artifact_id: str) -> bool:
        art = self.artifacts.get(artifact_id)
        if art is None or not art.path.exists():
            return False
        family = {task.task_id} | {
            t.task_id for t in self.tasks.values() if t.parent_id == task.task_id
        }
        allowed_inputs = set((task.request or {}).get("artifact_ids", []))
        return art.task_id in family or artifact_id in allowed_inputs

    async def publish(
        self, type_: str, task: Task, run: WorkflowRun, step_id: str | None = None, **data: Any
    ) -> None:
        self.store.event(
            type_,
            task_id=task.task_id,
            run_id=run.run_id,
            step_id=step_id,
            correlation_id=task.correlation_id,
            **data,
        )
        if type_ == "step.failed":
            await self._notify(
                task,
                f"❌ <code>{step_id}</code> {data.get('state')}: "
                f"{fmt.escape(fmt.truncate(str(data.get('error')), 300))}",
                "step",
            )
        elif type_ == "step.retry_scheduled":
            await self._notify(
                task,
                f"🔁 <code>{step_id}</code> attempt {data['attempt']} failed, retrying",
                "step",
            )
        elif type_ == "step.completed" and self.s.telegram_progress_updates:
            await self._notify(
                task, f"✅ <code>{step_id}</code> done ({data.get('duration_s')}s)", "step"
            )

    async def tool_event(self, rec: ToolExecutionRecord, event: str) -> None:
        """ToolEventPublisher: concise channel updates for real tool executions."""
        self.store.put(
            "tool_record",
            rec.record_id,
            rec,
            task_id=rec.task_id,
            state=rec.status.value if rec.status else None,
        )
        self.store.event(
            f"tool.{event}",
            task_id=rec.task_id,
            tool=rec.tool,
            status=rec.status,
            record_id=rec.record_id,
        )
        task = self.tasks.get(rec.task_id)
        if task is None or event != "finished":
            return
        icon = "✅" if rec.status == ToolStatus.succeeded else "❌"
        detail = fmt.escape(
            fmt.truncate(
                rec.error or rec.output.splitlines()[0] if (rec.error or rec.output) else "", 160
            )
        )
        await self._notify(task, f"{icon} <code>{rec.tool}</code> {rec.status}: {detail}", "tool")

    async def request_approval(self, task, run, step: StepDef, rec: StepRecord) -> bool:
        data = (task.request or {}).get("data") or {}
        if task.task_type in ART_TASK_TYPES and data.get("operator_requested"):
            rec.approved_by = "operator (asked for this revision on Telegram)"
            return True
        desc = {
            "tool": f"run tool <code>{step.ref}</code>",
            "agent": "let an agent modify a workspace",
            "approval": "continue past this checkpoint",
            "plugin": f"run <code>{step.ref}</code>",
        }
        msg = step.inputs.get("message") if step.executor == "approval" else None
        html = (
            f"Approve to {desc.get(step.executor, step.executor)} (step <code>{step.id}</code>)"
            + (f"\n{fmt.escape(str(msg))}" if msg else "")
        )
        brief = ((task.request or {}).get("data") or {}).get("prompt")
        if brief:  # paid generation: show exactly what will be generated before approval
            html += "\n\n<b>Brief</b>\n" + fmt.escape(fmt.truncate(str(brief), 900))
        try:
            approved, actor = await self._approval_wait(
                task, html, run_id=run.run_id, step_id=step.id
            )
        except Rejected as e:
            rec.error, rec.error_category = str(e), e.category  # "expired" or "approval_denied"
            return False
        rec.approved_by = actor
        return approved

    # ---------- executors ----------

    def _executor_known(self, kind: str, ref: str) -> bool:
        if kind == "internal":
            from app.gateway_config import INTERNAL_OPS

            return ref in INTERNAL_OPS
        if kind == "validator":
            return (
                ref.startswith("core.")
                and ref in CORE_VALIDATORS
                or bool(self.plugins.validator(ref))
            )
        return True

    async def _invoke_tool(
        self, task: Task, run, step: StepDef, name: str, args: dict, approved: bool
    ) -> ToolExecutionRecord:
        tool = self.tools.get(name)
        granted = frozenset(self.s.tool_permissions)
        if step.permissions:
            granted &= frozenset(step.permissions)
        ws = await self._tool_workspace(task, tool)
        ctx = ToolContext(
            task.task_id,
            ws,
            granted,
            actor=task.created_by,
            approved=frozenset({name}) if approved else frozenset(),
            artifacts=self.artifacts,
            attachments=self.attachments,
            read_input=lambda ref: self.read_input(task, ref),
            step_id=step.id,
        )
        # Single writer per worktree: concurrent file-writing steps conflict explicitly.
        writes = tool is not None and "workspace:write" in tool.permissions
        lock_ws = self._worktrees.get(task.task_id) if writes else None
        call_id = None
        if run:  # dedupe key per attempt: engine retries are deliberate new calls
            attempt = (self.store.get("step", f"{run.run_id}:{step.id}") or {}).get("attempt", 0)
            call_id = f"{run.run_id}:{step.id}:{attempt}:{name}:{json.dumps(args, sort_keys=True, default=str)}"
        if lock_ws:
            self.workspaces.acquire_write(lock_ws, f"{task.task_id}:{step.id}")
        try:
            rec = await self.executor.execute(name, args, ctx, call_id=call_id)
        finally:
            if lock_ws:
                self.workspaces.release_write(lock_ws, f"{task.task_id}:{step.id}")
        self.caps.record_execution(
            f"tool:{name}", rec.status == ToolStatus.succeeded, f"{rec.record_id} {rec.status}"
        )
        return rec

    async def _tool_workspace(self, task: Task, tool) -> Path:
        """Writes go to a per-task git worktree when WORKSPACE_ROOT is set, else task scratch."""
        if tool is None or not (
            {"workspace:read", "workspace:write", "process:execute"} & tool.permissions
        ):
            return self._scratch(task.task_id)
        if self.workspaces.enabled and tool.environment != "artifact":
            # Parallel steps of one task share one lazily created worktree; create it once.
            async with self._ws_locks.setdefault(task.task_id, asyncio.Lock()):
                ws = self._worktrees.get(task.task_id)
                if ws is None and (tool.side_effects or "process:execute" in tool.permissions):
                    ws = await self.workspaces.create(task.task_id)
                    self._worktrees[task.task_id] = ws
            return ws.path if ws else self.workspaces.project_root
        return self._scratch(task.task_id)

    async def _exec_tool(self, task, run, step: StepDef, inputs: dict) -> StepOutput:
        rec = await self._invoke_tool(
            task, run, step, step.ref, inputs, approved=self.needs_approval(step)
        )
        if rec.status == ToolStatus.succeeded:
            for a in rec.artifact_ids:
                if self.artifacts.get(a) and self.artifacts.get(a).step_id is None:
                    self.artifacts.get(a).step_id = step.id
            data = rec.data if rec.data is not None else {"text": rec.output}
            if isinstance(data, dict):
                data = {**data, "_record_id": rec.record_id}
            return StepOutput(data, rec.artifact_ids, f"tool:{step.ref}")
        outcome = {"timed_out": "timed_out", "unknown": "unknown"}.get(rec.status.value, "failed")
        retryable = rec.error_category in ("timeout", "rate_limit", "mcp_connection")
        raise StepError(
            rec.error or f"tool {rec.status}",
            rec.error_category or "tool_error",
            retryable,
            outcome,
            data={"_record_id": rec.record_id},
        )

    async def _exec_agent(self, task, run, step: StepDef, inputs: dict) -> StepOutput:
        need = {CAP_NAMES[c] for c in step.required_capabilities if c in CAP_NAMES} or {Cap.text}
        try:
            conn = self.router.route(need, self._pref(step.ref), task.agent)
        except UnsupportedCapability as e:
            raise StepError(str(e), "unsupported") from e
        single = len(run.workflow.steps) == 1
        holder = task if single else self._child(task, step.ref, step.title or step.id)
        atts = self._task_attachments(task)
        prompt = str(inputs.get("prompt") or task.description)
        req = AgentRequest(
            task_id=holder.task_id,
            prompt=prompt,
            system=inputs.get("system"),
            attachments=atts,
            json_schema=inputs.get("json_schema"),
            workspace=self._scratch(holder.task_id) if Cap.workspace_read in conn.caps else None,
        )
        try:
            result = await self._call(holder, conn, req, step.ref)
        except ConnectorError as e:
            self.caps.record_execution(self._cap_id(conn), False, f"{e.category}: {str(e)[:120]}")
            raise StepError(str(e), e.category, e.retryable, e.outcome) from e
        self.caps.record_execution(self._cap_id(conn), True, f"task {task.task_id}")
        if not single:
            holder.result = result.text
            self._set_state(holder, TaskState.succeeded)
        art = self.artifacts.add(
            task.task_id,
            "report",
            f"{step.id}.md",
            result.text,
            step_id=step.id,
            producer=conn.name,
        )
        if single:
            task.result = result.text
        return StepOutput(
            {
                "text": result.text,
                "structured": result.structured,
                "provider": result.provider,
                "model": result.model,
            },
            [art.artifact_id],
            f"agent:{conn.name}",
        )

    async def _exec_plugin(self, task, run, step: StepDef, inputs: dict) -> StepOutput:
        lp, fn = self.plugins.operation(step.ref)
        try:
            out = await fn(StepContext(self, task, run, step), inputs)
        except ConnectorError as e:  # runtime/provider errors keep their real outcome
            if isinstance(e, StepError):
                raise
            raise StepError(str(e), e.category, e.retryable, e.outcome) from e
        if not isinstance(out, StepOutput):
            raise StepError(f"plugin op {step.ref} returned {type(out).__name__}", "invalid_output")
        out.executor_ref = out.executor_ref or f"plugin:{step.ref}"
        return out

    async def _exec_validator(self, task, run, step: StepDef, inputs: dict) -> StepOutput:
        fn = CORE_VALIDATORS.get(step.ref) or self.plugins.validator(step.ref)
        if fn is None:
            raise StepError(f"unknown validator {step.ref}", "unsupported")
        try:
            gate: GateResult = await fn(StepContext(self, task, run, step), inputs)
        except StepError:
            raise
        except Exception as e:  # a validator that could not run is never a pass
            gate = GateResult(
                validator_id=step.ref, status="unknown", error=f"{type(e).__name__}: {e}"[:300]
            )
        for a in gate.evidence:
            if a.startswith("art_") and self.artifacts.get(a):
                self.artifacts.set_validation(a, "passed" if gate.status == "passed" else "failed")
        data = gate.model_dump(mode="json")
        if gate.status != "passed":
            raise StepError(
                f"gate {step.ref} {gate.status}: "
                + "; ".join(f.get("message", "") for f in gate.findings)[:400],
                f"gate_{gate.status}",
                data=data,
            )
        return StepOutput(data, [], f"validator:{step.ref}")

    async def _exec_capability(self, task, run, step: StepDef, inputs: dict) -> StepOutput:
        from app.capability_exec import exec_capability

        return await exec_capability(self, task, run, step, inputs)

    async def task_workspace(self, task: Task) -> Path:
        """The task's write workspace: a git worktree if WORKSPACE_ROOT is set, else scratch."""
        if not self.workspaces.enabled:
            return self._scratch(task.task_id)
        async with self._ws_locks.setdefault(task.task_id, asyncio.Lock()):
            ws = self._worktrees.get(task.task_id)
            if ws is None:
                ws = await self.workspaces.create(task.task_id)
                self._worktrees[task.task_id] = ws
        return ws.path

    async def _exec_internal(self, task, run, step: StepDef, inputs: dict) -> StepOutput:
        from app.capability_exec import internal_op

        if (out := await internal_op(self, task, run, step, inputs)) is not None:
            return out
        if step.ref == "aggregate":
            return StepOutput(
                inputs,
                [
                    a
                    for v in inputs.values()
                    if isinstance(v, list)
                    for a in v
                    if isinstance(a, str) and a.startswith("art_")
                ],
            )
        if step.ref == "select":
            return StepOutput(inputs.get("value"))
        if step.ref == "assert":
            if inputs.get("value") != inputs.get("equals"):
                raise StepError(
                    f"assertion failed: {inputs.get('value')!r} != {inputs.get('equals')!r}",
                    "assertion",
                )
            return StepOutput({"ok": True})
        raise StepError(f"unknown internal op {step.ref}", "unsupported")

    async def _exec_approval(self, task, run, step: StepDef, inputs: dict) -> StepOutput:
        rec = self.load_steps(run.run_id).get(step.id)
        return StepOutput({"approved": True, "by": rec.approved_by if rec else None})

    # ---------- agents ----------

    def _child(self, parent: Task, role: str, title: str, depends_on: list[Task] = ()) -> Task:
        for dep in depends_on:
            if dep.state != TaskState.succeeded:
                raise ValueError(f"dependency {dep.task_id} is {dep.state}")
        child = Task(
            title=title,
            description=parent.description,
            workflow=parent.workflow,
            role=role,
            parent_id=parent.task_id,
            depends_on=[d.task_id for d in depends_on],
            correlation_id=parent.correlation_id,
            created_by=parent.created_by,
            domain=parent.domain,
        )
        self.tasks[child.task_id] = child
        self._set_state(child, TaskState.queued)
        self._set_state(child, TaskState.running)
        return child

    async def _call(
        self, step: Task, connector: AgentConnector, req: AgentRequest, role: str, ws=None
    ) -> AgentResult:
        step.provider, step.model = connector.name, getattr(connector, "model", None)
        started = now()
        self._active_calls[step.task_id] = connector
        if self.s.telegram_progress_updates:
            await self._notify(
                step, f"▶️ {role}: {fmt.escape(step.title)} via {connector.name}", "step"
            )
        try:
            async with self._sem:
                result = await connector.execute(req)
        except ConnectorError as e:
            step.usage.append(
                UsageRecord(
                    task_id=step.task_id,
                    provider=connector.name,
                    model=step.model,
                    started_at=started,
                    ended_at=now(),
                    status="error",
                    error_category=e.category,
                )
            )
            step.error, step.error_category = str(e), e.category
            self.caps.record_execution(self._cap_id(connector), False, f"{e.category}")
            if step.parent_id:
                self._finish(
                    step, TaskState.unknown if e.outcome == "unknown" else TaskState.failed
                )
            raise
        finally:
            self._active_calls.pop(step.task_id, None)
        self.caps.record_execution(self._cap_id(connector), True, f"task {step.task_id}")
        step.model = result.model or step.model
        step.usage.append(
            UsageRecord(
                task_id=step.task_id,
                provider=result.provider,
                model=result.model,
                started_at=started,
                ended_at=now(),
                status="ok",
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cost_usd=result.cost_usd,
            )
        )
        sess = AgentSession(
            connector=connector.name,
            native_session_id=result.session_id,
            task_id=step.task_id,
            role=role,
            workspace=ws.path if ws else None,
        )
        self.sessions[sess.session_id] = sess
        self.store.put("session", sess.session_id, sess, task_id=step.task_id)
        step.session_ids.append(sess.session_id)
        return result

    def _task_attachments(self, task: Task):
        out = []
        for att_id in task.attachment_ids:
            att = self.attachments.get(att_id)
            out.append((att, self.attachments.read(att)))
        return out

    def read_input(self, task: Task, ref: str) -> tuple[str, str, bytes]:
        """Resolve an attachment or artifact reference with task isolation."""
        root = self.tasks.get(task.parent_id) if task.parent_id else task
        if ref.startswith("att_"):
            if ref not in root.attachment_ids:
                raise StepError(f"attachment {ref} is not part of this task", "forbidden")
            a = self.attachments.get(ref)
            return a.filename, a.mime_type, self.attachments.read(a)
        if ref.startswith("art_"):
            if not self.artifact_ok(root, ref):
                raise StepError(f"artifact {ref} is not available to this task", "forbidden")
            a = self.artifacts.get(ref)
            return a.filename, a.mime_type or "application/octet-stream", self.artifacts.read(a)
        raise StepError(f"invalid input reference {ref!r}", "invalid_input")

    def _scratch(self, task_id: str) -> Path:
        if not task_id.replace("_", "").isalnum():
            raise ValueError("invalid task id")
        p = self.s.data_dir / "scratch" / task_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    # ---------- approvals ----------

    async def _approval_wait(self, task: Task, html: str, photos=(), run_id=None, step_id=None):
        approval_id = new_id("apr")
        fut = asyncio.get_running_loop().create_future()
        self._approvals[approval_id] = fut
        rec = {
            "approval_id": approval_id,
            "task_id": task.task_id,
            "run_id": run_id,
            "step_id": step_id,
            "state": "pending",
            "requested_at": now().isoformat(),
            "ttl_s": self.s.action_ttl_s,
            "text": fmt.plain(html)[:500],
        }
        self.store.put(
            "approval", approval_id, rec, task_id=task.task_id, run_id=run_id, state="pending"
        )
        prev = task.state
        self._set_state(task, TaskState.waiting_for_approval)
        group = new_id("grp")
        states = {TaskState.waiting_for_approval}
        acts = [
            self.actions.create(
                "approve",
                task.task_id,
                "Approve",
                states,
                group,
                payload={"approval_id": approval_id},
            ),
            self.actions.create(
                "reject",
                task.task_id,
                "Reject",
                states,
                group,
                payload={"approval_id": approval_id},
            ),
            self.actions.create("stop", task.task_id, "Stop", states),
        ]
        await self._safe_report(
            task,
            self.msg(
                "approval",
                task,
                html,
                title=task.title,
                ref=approval_id,
                reply="Use the buttons below",
            ),  # fmt: skip
            actions=acts,
            photos=photos,
            control=True,
        )
        try:
            approved, actor = await asyncio.wait_for(fut, self.s.action_ttl_s)
        except TimeoutError as e:
            rec.update(state="expired")
            self.store.put(
                "approval", approval_id, rec, task_id=task.task_id, run_id=run_id, state="expired"
            )
            raise Rejected("approval expired", expired=True) from e
        finally:
            self._approvals.pop(approval_id, None)
            if (
                not self._approvals_pending(task.task_id)
                and task.state == TaskState.waiting_for_approval
            ):
                self._set_state(
                    task, prev if prev != TaskState.waiting_for_approval else TaskState.running
                )
        if not approved:
            raise Rejected()
        return approved, actor

    def cleanup_artifacts(self, retention_days: int | None = None) -> dict:
        """Delete artifacts of finished tasks older than the retention period.

        Never touches artifacts of non-terminal tasks (or their steps) or artifacts any
        non-terminal task references as an input."""
        from datetime import timedelta

        days = retention_days or (
            self.gateway_cfg.policies.artifact_retention_days if self.gateway_cfg else 30
        )
        cutoff = now() - timedelta(days=days)
        active = {t.task_id for t in self.tasks.values() if not t.done}
        active |= {t.task_id for t in self.tasks.values() if t.parent_id in active}
        referenced = {
            a
            for t in self.tasks.values()
            if not t.done
            for a in (t.request or {}).get("artifact_ids", [])
        }
        deleted, kept = [], 0
        for art in self.artifacts.all():
            owner = self.tasks.get(art.task_id)
            if (
                art.created_at > cutoff
                or art.task_id in active
                or art.artifact_id in referenced
                or owner is None
            ):
                kept += 1
                continue
            self.artifacts.delete(art.artifact_id)
            deleted.append(art.artifact_id)
        self.store.event("artifacts.cleanup", deleted=len(deleted), kept=kept, retention_days=days)
        return {"deleted": deleted, "kept": kept, "retention_days": days}

    def _approvals_pending(self, task_id: str) -> bool:
        return bool(self._task_approvals(task_id))

    async def _approval(self, task: Task, html: str, photos=()) -> None:
        """Approval checkpoint used inside plugin operations."""
        await self._approval_wait(task, html, photos)

    async def _safe_report(self, task: Task, html: str, **kw) -> None:
        try:
            await self.notifier.report(task, html, **kw)
        except Exception:
            task.delivery_failures += 1
            log.exception("report delivery failed for %s", task.task_id)

    async def _notify(self, task: Task, html: str, kind="info", files=None, actions=()) -> None:
        await self._safe_report(
            task, self.msg(kind, task, html), actions=list(actions), files=files or []
        )

    # ---------- message identity (project / session) ----------

    def _lineage(self, task: Task):
        """task, its parent step owner, then feedback sources (request.parent_task_id), cycle-safe."""
        seen, t = set(), task
        while t is not None and t.task_id not in seen:
            seen.add(t.task_id)
            yield t
            t = self.tasks.get(t.parent_id or (t.request or {}).get("parent_task_id") or "")

    def project_of(self, task: Task) -> str:
        for t in self._lineage(task):
            if p := ((t.request or {}).get("data") or {}).get("project"):
                return str(p)
        return self.default_project

    @property
    def default_project(self) -> str:
        return self.s.workspace_root.name if self.s.workspace_root else "gateway"

    def session_of(self, task: Task) -> str:
        """Caller-supplied session, else the latest agent session, else the correlation id
        (shared by a task and its feedback follow-ups)."""
        root = self.tasks.get(task.parent_id or "") or task
        if s := ((root.request or {}).get("data") or {}).get("session_id"):
            return str(s)
        steps = [t for t in self.tasks.values() if t.parent_id == root.task_id]
        sessions = [s for t in (root, *steps) for s in t.session_ids]
        return sessions[-1] if sessions else root.correlation_id

    def msg(self, kind: str, task: Task, body: str = "", **kw) -> str:
        root = self.tasks.get(task.parent_id or "") or task
        return fmt.envelope(
            kind,
            body,
            project=self.project_of(root),
            task_id=root.task_id,
            session=self.session_of(root),
            **kw,
        )

    def _report_message(self, task: Task):
        html = self.report(task)
        msg, overflow = fmt.fit(html)
        files = [(f"{task.task_id}_report.txt", overflow.encode())] if overflow else []
        return msg, files

    def _all_artifacts(self, task: Task):
        ids = {task.task_id} | {
            t.task_id for t in self.tasks.values() if t.parent_id == task.task_id
        }
        return [a for i in ids for a in self.artifacts.for_task(i)]

    def _task_for_session(self, session_id: str) -> Task | None:
        sess = self.sessions.get(session_id)
        if not sess:
            return None
        t = self.tasks.get(sess.task_id)
        return self.tasks.get(t.parent_id) if t and t.parent_id else t

    # ---------- persistence / recovery ----------

    def load_run(self, run_id: str) -> WorkflowRun | None:
        body = self.store.get("run", run_id)
        return WorkflowRun.model_validate(body) if body else None

    def load_steps(self, run_id: str) -> dict[str, StepRecord]:
        recs = [StepRecord.model_validate(b) for b in self.store.list("step", run_id=run_id)]
        run = self.load_run(run_id)
        order = {s.id: i for i, s in enumerate(run.workflow.steps)} if run else {}
        return {r.step_id: r for r in sorted(recs, key=lambda r: order.get(r.step_id, 99))}

    async def recover(self) -> list[dict]:
        """Resume interrupted tasks. Non-idempotent in-flight steps become unknown, never replayed."""
        report = []
        for body in self.store.list("task"):
            task = Task.model_validate(body)
            self.tasks.setdefault(task.task_id, task)
        for task in list(self.tasks.values()):
            if task.done or task.task_id in self._running or task.parent_id:
                continue
            entry = {"task_id": task.task_id, "state": task.state.value}
            run = self.load_run(task.run_id) if task.run_id else None
            if run is None or task.state in (
                TaskState.queued,
                TaskState.planning,
                TaskState.waiting_for_user,
            ):
                # Planning has no side effects: start over from the stored request.
                task.state = TaskState.queued
                entry["action"] = "re-planned from request"
                self._running[task.task_id] = asyncio.create_task(self._run(task))
            else:
                recs = self.load_steps(run.run_id)
                entry["steps"] = self.engine.prepare_recovery(run, recs)
                task.state = TaskState.running
                entry["action"] = "resumed workflow"
                self._running[task.task_id] = asyncio.create_task(
                    self._run(task, resume=(run, recs))
                )
            self.store.event(
                "task.recovered",
                task_id=task.task_id,
                **{k: v for k, v in entry.items() if k != "task_id"},
            )
            report.append(entry)
        return report


def _step_line(r: StepRecord) -> str:
    icon = {StepState.succeeded: "✅", StepState.skipped: "⏭"}.get(
        r.state, "❌" if r.state in STEP_BAD else "⏳"
    )
    line = f"{icon} {r.step_id}: {r.state}"
    return line + (f" — {fmt.escape(fmt.truncate(r.error, 120))}" if r.error else "")


# ---------- core validators (domain-neutral quality gates) ----------


async def _gate_artifacts_readable(ctx: StepContext, inputs: dict) -> GateResult:
    ids = inputs.get("artifact_ids") or []
    findings, ok = [], []
    for aid in ids:
        try:
            ctx.read_artifact(aid)  # existence + ownership + hash check
            ok.append(aid)
        except Exception as e:
            findings.append({"severity": "high", "message": f"{aid}: {e}"})
    status = "passed" if ids and not findings else ("skipped" if not ids else "failed")
    return GateResult(
        validator_id="core.artifacts_readable",
        status=status,
        evidence=ok,
        findings=findings,
        metrics={"checked": len(ids)},
    )


async def _gate_json_schema(ctx: StepContext, inputs: dict) -> GateResult:
    try:
        jsonschema.validate(inputs.get("value"), inputs.get("schema") or {})
    except jsonschema.ValidationError as e:
        return GateResult(
            validator_id="core.json_schema",
            status="failed",
            findings=[{"severity": "high", "message": e.message}],
        )
    return GateResult(validator_id="core.json_schema", status="passed")


async def _gate_non_empty(ctx: StepContext, inputs: dict) -> GateResult:
    text = str(inputs.get("text") or "").strip()
    return GateResult(
        validator_id="core.non_empty",
        status="passed" if text else "failed",
        metrics={"chars": len(text)},
        findings=[] if text else [{"severity": "high", "message": "output is empty"}],
    )


CORE_VALIDATORS = {
    "core.artifacts_readable": _gate_artifacts_readable,
    "core.json_schema": _gate_json_schema,
    "core.non_empty": _gate_non_empty,
}
