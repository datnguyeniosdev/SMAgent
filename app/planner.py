"""Task classification, routing and plan validation (deterministic; observable decisions)."""

import logging
from dataclasses import dataclass, field
from typing import Any

import jsonschema

from app.capabilities import CapabilityRegistry
from app.intake import TaskRequest
from app.plugins.base import Classification, PlanResult, PluginManager
from app.tools import ToolRegistry
from app.workflow import InvalidWorkflow, StepDef, WorkflowDef, validate_structure

log = logging.getLogger(__name__)
CAP_HINTS = {
    "model.vision": "image_input",
    "model.files": "file_input",
    "model.text": "text",
    "workspace.write": "workspace_write",
}
CONFIDENT = 0.5
MARGIN = 0.15


@dataclass
class PlanningEnv:
    """What a plugin may know while planning. No execution handles."""

    has: Any  # callable(capability: str) -> bool
    tools: list[str]
    qa_checks: list[str]
    workspace_configured: bool
    attachments: list[dict]  # id, filename, mime_type, size_bytes
    config: dict = field(default_factory=dict)  # this plugin's PLUGIN_CONFIG entry


@dataclass
class RoutingDecision:
    mode: str  # direct_tool | explicit | classified | fallback | clarify | rejected
    plugin_id: str | None
    task_type: str | None
    score: float | None = None
    reasons: list[str] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)
    workflow_id: str | None = None
    workflow_version: str | None = None


class PlanError(ValueError):
    pass


class Planner:
    def __init__(
        self,
        plugins: PluginManager,
        caps: CapabilityRegistry,
        tools: ToolRegistry,
        plugin_config: dict | None = None,
        gateway_cfg=None,
        capability_check=None,
    ):
        self.plugins = plugins
        self.caps = caps
        self.tools = tools
        self.plugin_config = plugin_config or {}
        self.gateway_cfg = (
            gateway_cfg  # config-driven routes/workflows take precedence when present
        )
        self.capability_check = capability_check or (lambda step: "no capability registry")

    def has(self, capability: str) -> bool:
        if capability.startswith("tool:"):
            return self.tools.get(capability[5:]) is not None and self.caps.routable(capability)
        return bool(self.caps.providers_of(capability))

    def env(self, plugin_id: str, qa_checks, workspace_configured, attachments) -> PlanningEnv:
        return PlanningEnv(
            self.has,
            [t.name for t in self.tools.list()],
            list(qa_checks),
            workspace_configured,
            attachments,
            self.plugin_config.get(plugin_id, {}),
        )

    # ---------- routing ----------

    def route(self, req: TaskRequest, attachments: list[dict]) -> RoutingDecision:
        if req.workflow is not None:
            return RoutingDecision(
                "explicit_workflow", None, None, reasons=["workflow definition supplied"]
            )
        if req.workflow_id is not None:
            return self._config_decision(
                "config_workflow",
                req.workflow_id,
                req.task_type,
                [f"workflow {req.workflow_id!r} requested explicitly"],
            )
        if req.tool or req.mode == "direct_tool":
            return RoutingDecision("direct_tool", None, None, reasons=["explicit tool requested"])
        if (hit := self._config_route(req, attachments)) is not None:
            return hit
        if req.domain == "general":
            return RoutingDecision(
                "explicit",
                "general",
                req.task_type or "answer",
                reasons=["explicit general domain"],
            )
        if req.domain:
            lp = self.plugins.get(req.domain)
            if lp is None:
                return RoutingDecision(
                    "rejected",
                    req.domain,
                    req.task_type,
                    reasons=[f"plugin {req.domain!r} is not active"],
                )
            task_type = req.task_type
            if task_type is None:
                c = self._safe_classify(lp, req, attachments)
                task_type = c.task_type if c else None
            return RoutingDecision("explicit", req.domain, task_type, reasons=["explicit domain"])
        cands = [
            c for lp in self.plugins.active() if (c := self._safe_classify(lp, req, attachments))
        ]
        cands.sort(key=lambda c: c.score, reverse=True)
        listed = [
            {
                "plugin": c.plugin_id,
                "task_type": c.task_type,
                "score": round(c.score, 2),
                "reasons": c.reasons,
            }
            for c in cands
        ]
        if (
            cands
            and cands[0].score >= CONFIDENT
            and (len(cands) == 1 or cands[0].score - cands[1].score >= MARGIN)
        ):
            best = cands[0]
            return RoutingDecision(
                "classified", best.plugin_id, best.task_type, best.score, best.reasons, listed
            )
        if cands and cands[0].score >= CONFIDENT:
            return RoutingDecision(
                "clarify", None, None, cands[0].score, ["ambiguous: several domains match"], listed
            )
        cfg = self.gateway_cfg
        if cfg is not None and cfg.routing.default_workflow:
            return self._config_decision(
                "config_default",
                cfg.routing.default_workflow,
                None,
                ["no route or plugin matched; configured default workflow"],
                listed,
            )
        if self.has("model.text"):
            return RoutingDecision(
                "fallback",
                "general",
                "answer",
                None,
                ["no domain matched; answering with a model (read-only)"],
                listed,
            )
        return RoutingDecision(
            "clarify", None, None, None, ["no domain matched and no model is available"], listed
        )

    # ---------- configuration routes ----------

    def _config_decision(
        self, mode, workflow_id, task_type, reasons, candidates=()
    ) -> RoutingDecision:
        cfg = self.gateway_cfg
        wf = cfg.workflows.get(workflow_id) if cfg else None
        if wf is None or not wf.enabled:
            return RoutingDecision(
                "rejected",
                None,
                task_type,
                reasons=[f"unknown or disabled workflow {workflow_id!r}"],
            )
        d = RoutingDecision(
            mode, wf.domain, task_type or wf.id, reasons=reasons, candidates=list(candidates)
        )
        d.workflow_id, d.workflow_version = wf.id, wf.version
        return d

    def _config_route(self, req: TaskRequest, attachments: list[dict]) -> RoutingDecision | None:
        """Known task types use their configured workflow; then keyword/domain/mime/source matches."""
        cfg = self.gateway_cfg
        if cfg is None:
            return None
        routes = sorted((r for r in cfg.routing.routes if r.enabled), key=lambda r: r.priority)
        if req.task_type:
            for r in routes:
                if r.task_type == req.task_type and (
                    not req.domain or not r.match.domains or req.domain in r.match.domains
                ):
                    return self._config_decision(
                        "config_route",
                        r.workflow,
                        r.task_type,
                        [f"route {r.id}: task_type {r.task_type!r}"],
                    )
        words = set(req.objective.lower().replace(",", " ").replace(".", " ").split())
        mimes = [a["mime_type"] for a in attachments]
        for r in routes:
            m = r.match
            if not (m.keywords or m.domains or m.attachment_mime_prefixes or m.sources):
                continue
            if m.keywords and not words & {k.lower() for k in m.keywords}:
                continue
            if m.domains and req.domain not in m.domains:
                continue
            if m.attachment_mime_prefixes and not any(
                x.startswith(p) for x in mimes for p in m.attachment_mime_prefixes
            ):
                continue
            if m.sources and req.source not in m.sources:
                continue
            return self._config_decision(
                "config_route",
                r.workflow,
                r.task_type,
                [
                    f"route {r.id} matched ({', '.join(sorted(words & {k.lower() for k in m.keywords})) or 'filters'})"
                ],
            )
        return None

    def _safe_classify(self, lp, req, attachments) -> Classification | None:
        try:
            return lp.plugin.classify(req, attachments)
        except Exception:  # a broken plugin must not break routing
            log.exception("plugin %s classify failed", lp.manifest.id)
            return None

    # ---------- planning ----------

    def plan(self, req: TaskRequest, decision: RoutingDecision, env_args: dict) -> PlanResult:
        if decision.mode == "rejected":
            raise PlanError("; ".join(decision.reasons))
        if decision.mode == "clarify":
            opts = [f"{c['plugin']}:{c['task_type']}" for c in decision.candidates[:4]]
            if not opts:
                opts = [f"{lp.manifest.id}" for lp in self.plugins.active()]
            return PlanResult(question="Which kind of task is this?", options=opts)
        if decision.mode == "direct_tool":
            return PlanResult(self._direct_tool(req))
        if decision.mode in ("config_workflow", "config_route", "config_default"):
            wf = self.gateway_cfg.workflows[decision.workflow_id]
            if wf.input_schema:
                try:
                    jsonschema.validate(req.data, wf.input_schema)
                except jsonschema.ValidationError as e:
                    raise PlanError(f"invalid input for workflow {wf.id}: {e.message}") from e
            return PlanResult(
                WorkflowDef.model_validate(
                    wf.model_dump(exclude={"enabled", "context_writes"}, exclude_unset=True)
                )
            )
        if decision.mode == "explicit_workflow":
            try:
                return PlanResult(WorkflowDef.model_validate(req.workflow))
            except ValueError as e:
                raise InvalidWorkflow([f"invalid workflow definition: {e}"]) from e
        if decision.plugin_id == "general":
            return PlanResult(self._general(req, env_args))
        lp = self.plugins.get(decision.plugin_id)
        if decision.task_type is None:
            ops = [o.task_type for o in lp.manifest.operations]
            return PlanResult(
                question=f"What should the {lp.manifest.name} plugin do?", options=ops
            )
        op = next((o for o in lp.manifest.operations if o.task_type == decision.task_type), None)
        if op is None:
            raise PlanError(f"plugin {lp.manifest.id} has no operation {decision.task_type!r}")
        missing = [c for c in op.required_capabilities if not self.has(c)]
        if missing:
            raise PlanError(
                f"{lp.manifest.id}.{op.task_type} unavailable: missing capabilities {missing}"
            )
        if op.input_schema is not None:
            try:
                jsonschema.validate(req.data, op.input_schema)
            except jsonschema.ValidationError as e:
                return PlanResult(
                    question=f"Input for {op.task_type} is invalid: {e.message}. "
                    "Please resend with the required fields."
                )
        env = self.env(lp.manifest.id, **env_args)
        try:
            result = lp.plugin.plan(req, op.task_type, env)
        except PlanError:
            raise
        except Exception as e:
            raise PlanError(
                f"plugin {lp.manifest.id} failed to plan: {type(e).__name__}: {e}"
            ) from e
        return result

    def _direct_tool(self, req: TaskRequest) -> WorkflowDef:
        tool = self.tools.get(req.tool or "")
        if tool is None:
            raise PlanError(f"unknown tool {req.tool!r}")
        return WorkflowDef(
            id="core.direct_tool",
            domain="general",
            description=f"Run {tool.name}",
            steps=[
                StepDef(
                    id="run",
                    executor="tool",
                    ref=tool.name,
                    inputs=req.tool_args,
                    timeout_s=tool.timeout_s,
                )
            ],
            outputs={"result": "$steps.run.output"},
        )

    def _general(self, req: TaskRequest, env_args: dict) -> WorkflowDef:
        caps = ["model.text"]
        mimes = [a["mime_type"] for a in env_args.get("attachments", [])]
        if any(m.startswith("image/") for m in mimes):
            caps.append("model.vision")
        if any(not m.startswith("image/") for m in mimes):
            caps.append("model.files")
        return WorkflowDef(
            id="core.answer",
            domain="general",
            description="Answer with a model",
            steps=[
                StepDef(
                    id="answer",
                    executor="agent",
                    ref="assistant",
                    required_capabilities=caps,
                    inputs={"prompt": "$task.objective"},
                )
            ],
            outputs={"answer": "$steps.answer.output.text"},
        )

    # ---------- validation ----------

    def validate(self, wf: WorkflowDef, req: TaskRequest, executors_ok) -> WorkflowDef:
        """Reject before execution: structure, executors, capabilities, policy, constraints."""
        errors = validate_structure(wf)
        cap_errors: list[str] = []
        for s in [*wf.steps, *wf.compensations]:
            if s.executor == "tool":
                t = self.tools.get(s.ref)
                if t is None:
                    errors.append(f"step {s.id}: unknown tool {s.ref!r}")
                elif not self.caps.routable(f"tool:{s.ref}"):
                    errors.append(f"step {s.id}: tool {s.ref} is not available")
                elif req.require_approval and t.side_effects:
                    s.requires_approval = True
            elif s.executor == "agent":
                for c in s.required_capabilities:
                    if not self.has(c):
                        cap_errors.append(
                            f"step {s.id}: no available provider for {c}"
                            + (f" ({CAP_HINTS[c]})" if c in CAP_HINTS else "")
                        )
                if req.require_approval and "workspace.write" in s.required_capabilities:
                    s.requires_approval = True
            elif s.executor == "plugin":
                try:
                    self.plugins.operation(s.ref)
                except Exception as e:
                    errors.append(f"step {s.id}: {e}")
            elif s.executor == "capability":
                cap = self.gateway_cfg.capabilities.get(s.ref) if self.gateway_cfg else None
                if (
                    cap is not None
                ):  # capability-level policies apply unless the step overrides them
                    if "retry" not in s.model_fields_set:
                        s.retry = cap.retry
                    if "timeout_s" not in s.model_fields_set:
                        s.timeout_s = cap.timeout_s
                if (why := self.capability_check(s)) is not None:
                    cap_errors.append(f"step {s.id}: capability {s.ref} unavailable: {why}")
            elif s.executor == "validator" and not executors_ok("validator", s.ref):
                errors.append(f"step {s.id}: unknown validator {s.ref!r}")
            elif s.executor == "internal" and not executors_ok("internal", s.ref):
                errors.append(f"step {s.id}: unknown internal op {s.ref!r}")
            for c in s.required_capabilities:
                if c.startswith("tool:") and not self.has(c):
                    cap_errors.append(f"step {s.id}: missing {c}")
        missing = [c for c in req.constraints if c not in wf.constraints]
        wf.constraints = list(dict.fromkeys([*wf.constraints, *missing]))  # never drop constraints
        if errors:
            raise InvalidWorkflow(errors + cap_errors)
        if cap_errors:  # well-formed plan, but the environment cannot run it
            raise PlanError("unsupported: " + "; ".join(cap_errors))
        return wf
