"""Declarative gateway configuration (YAML) with schemas, reference validation and diagnostics.

Layout (all optional except app.yaml):
    config/app.yaml                     schema_version, app, telegram
    config/agents/*.yaml                agents: [...]
    config/capabilities/*.yaml          capabilities: [...]
    config/providers/*.yaml             providers: [...]
    config/mcp_servers/*.yaml           mcp_servers: [...]
    config/routing/*.yaml               routing: {...}            (exactly one file)
    config/workflows/*.yaml             workflows: [...]
    config/policies/*.yaml              policies: {...}           (exactly one file)

Precedence: built-in defaults < YAML < environment (Settings). Secrets are never read
from YAML: providers name an environment variable (`api_key_env`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import jsonschema
import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

from app.workflow import RetryPolicy, WorkflowDef, validate_structure

SCHEMA_VERSION = 1
_ID = r"^[a-z][a-z0-9_.-]{0,63}$"
_SECRET_KEY = re.compile(r"(api[_-]?key|token|secret|password)$", re.I)
_SECRET_VALUE = re.compile(
    r"(sk-[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{20,}|\b\d{6,}:[A-Za-z0-9_-]{30,})"
)
INTERNAL_OPS = {"aggregate", "select", "assert", "context.load", "prompt.build", "report.publish"}
RUNTIMES = {"claude_code", "opencode"}


class ExecutorSpec(BaseModel):
    type: Literal["tool", "provider", "runtime", "plugin", "internal"]
    ref: str  # tool name | provider id | runtime id | plugin operation | internal op
    operation: str | None = (
        None  # provider: generate | generate_structured; runtime: new_execution | return_to_caller
    )
    priority: int = 100  # lower is preferred
    requires_env: list[str] = []
    permissions: list[str] = []
    options: dict[str, Any] = {}
    model: str | None = None  # provider: run on this model instead of the provider's default


class CapabilityDef(BaseModel):
    id: str = Field(pattern=_ID)
    description: str = ""
    enabled: bool = True
    input_schema: dict = {"type": "object"}
    output_schema: dict | None = None
    executors: list[ExecutorSpec] = Field(min_length=1)
    timeout_s: float = Field(300, gt=0, le=7200)
    retry: RetryPolicy = RetryPolicy()
    side_effects: bool = False
    requires_approval: bool = False


class AgentDef(BaseModel):
    id: str = Field(pattern=_ID)
    name: str = ""
    description: str = ""
    enabled: bool = True
    capabilities: list[str] = Field(min_length=1)
    executor_preference: list[str] = []  # e.g. ["provider:gemini", "runtime:claude_code"]


class ProviderDef(BaseModel):
    id: str = Field(pattern=_ID)
    type: Literal["openai_compatible", "gemini", "fal", "meshy"]
    enabled: bool = True
    model: str | None = None
    model_env: str | None = None  # take the model id from this environment variable instead
    base_url: str | None = None
    api_key_env: str  # name of the environment variable holding the key
    capabilities: list[
        Literal[
            "model.text",
            "model.vision",
            "model.files",
            "model.structured",
            "model.tool_calling",
            "image.generate",
            "model3d.generate",
        ]
    ]
    json_mode: Literal["json_schema", "json_object", "prompt"] = "json_schema"
    timeout_s: float = 120
    options: dict[str, Any] = {}  # provider-specific defaults (e.g. fal model input fields)


class McpServerDef(BaseModel):
    name: str = Field(pattern=_ID)
    enabled: bool = True
    command: str | None = None
    args: list[str] = []
    url: str | None = None
    allowed_tools: list[str] = []
    read_only_tools: list[str] = []
    path_args: list[str] = ["path", "file_path", "notebook_path"]
    timeout_s: float = 30
    env_passthrough: list[str] = []


class RouteMatch(BaseModel):
    keywords: list[str] = []  # any keyword (lower-case word) in the objective
    domains: list[str] = []
    attachment_mime_prefixes: list[str] = []
    sources: list[str] = []  # telegram | api | mcp


class RouteDef(BaseModel):
    id: str = Field(pattern=_ID)
    enabled: bool = True
    task_type: str
    workflow: str
    priority: int = 100
    match: RouteMatch = RouteMatch()


class FeedbackRouteDef(BaseModel):
    id: str = Field(pattern=_ID)
    enabled: bool = True
    workflow: str
    priority: int = 100
    keywords: list[str] = []
    requires_report_images: bool = False


class ChannelRule(BaseModel):
    """Central routing of live-session work: a request of a project in routing_mode "central" whose
    text matches goes to `lane` (its child session), with no LLM involved."""

    project: str = "*"  # glob over the project's normalized name
    match: str  # regex over the request text
    lane: str

    @field_validator("match")
    @classmethod
    def _regex(cls, v):
        re.compile(v)
        return v


class RoutingConfig(BaseModel):
    channel_rules: list[ChannelRule] = []  # first match wins; no match: the Gateway Agent decides
    default_workflow: str | None = None
    plugin_fallback: bool = True  # Phase 3-5 plugin classifier when no route matches
    dynamic_planning: dict = {"enabled": False}  # model-generated plans: not implemented
    routes: list[RouteDef] = []
    feedback_routes: list[FeedbackRouteDef] = []

    @field_validator("dynamic_planning")
    @classmethod
    def _no_dynamic(cls, v):
        if v.get("enabled"):
            raise ValueError(
                "dynamic_planning.enabled=true is not supported (no verified planner model)"
            )
        return v


class WorkflowConfig(WorkflowDef):
    enabled: bool = True
    context_writes: list[str] = []  # task-context keys this workflow may update


class ApprovalRule(BaseModel):
    match: str  # fnmatch on executor ids: "runtime:*", "tool:workspace.create_file", "capability:x"
    reason: str = ""


class PolicyConfig(BaseModel):
    approvals: list[ApprovalRule] = []
    provider_allowlist: list[str] | None = None
    tool_allowlist: list[str] | None = None  # fnmatch patterns; None = all registered tools
    mcp_url_hosts: list[str] = ["127.0.0.1", "localhost", "::1"]
    trusted_plugin_modules: list[str] = [
        "app.plugins.software",
        "app.plugins.documents",
        "app.plugins.data",
        "app.plugins.media",
        "app.plugins.unity",
        "app.plugins.game_research",
    ]
    artifact_retention_days: int = Field(30, ge=1)


class TelegramConfig(BaseModel):
    discussion_chat_id: int | None = None  # linked Discussion Group (non-secret)
    feedback_requires_reply: bool = True
    max_report_photos: int = Field(4, ge=0, le=10)


class AppSection(BaseModel):
    name: str = "agent-gateway"
    environment: str = "local"


@dataclass
class Diagnostic:
    level: Literal["error", "warning"]
    source: str
    message: str

    def __str__(self) -> str:
        return f"{self.level.upper()} {self.source}: {self.message}"


@dataclass
class GatewayConfig:
    schema_version: int = SCHEMA_VERSION
    app: AppSection = field(default_factory=AppSection)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    agents: dict[str, AgentDef] = field(default_factory=dict)
    capabilities: dict[str, CapabilityDef] = field(default_factory=dict)
    providers: dict[str, ProviderDef] = field(default_factory=dict)
    mcp_servers: dict[str, McpServerDef] = field(default_factory=dict)
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    workflows: dict[str, WorkflowConfig] = field(default_factory=dict)
    policies: PolicyConfig = field(default_factory=PolicyConfig)
    sources: dict[str, str] = field(default_factory=dict)  # "<kind>:<id>" -> file
    diagnostics: list[Diagnostic] = field(default_factory=list)
    root: Path | None = None

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.level == "error"]


class ConfigError(Exception):
    def __init__(self, diagnostics: list[Diagnostic]):
        self.diagnostics = diagnostics
        super().__init__("invalid configuration:\n" + "\n".join(f"  {d}" for d in diagnostics))


_LISTS = {
    "agents": ("agents", AgentDef, "id"),
    "capabilities": ("capabilities", CapabilityDef, "id"),
    "providers": ("providers", ProviderDef, "id"),
    "mcp_servers": ("mcp_servers", McpServerDef, "name"),
    "workflows": ("workflows", WorkflowConfig, "id"),
}


def _scan_secrets(obj: Any, path: str, out: list[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if (
                isinstance(v, str)
                and v
                and _SECRET_KEY.search(str(k))
                and not str(k).endswith("_env")
            ):
                out.append(f"{path}.{k}: secrets must come from environment variables (use *_env)")
            _scan_secrets(v, f"{path}.{k}", out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _scan_secrets(v, f"{path}[{i}]", out)
    elif isinstance(obj, str) and _SECRET_VALUE.search(obj):
        out.append(f"{path}: value looks like a secret; move it to an environment variable")


def _read(path: Path, cfg: GatewayConfig) -> dict | None:
    try:
        data = yaml.safe_load(path.read_text("utf-8")) or {}
    except yaml.YAMLError as e:
        cfg.diagnostics.append(Diagnostic("error", str(path), f"YAML parse error: {e}"))
        return None
    if not isinstance(data, dict):
        cfg.diagnostics.append(Diagnostic("error", str(path), "top level must be a mapping"))
        return None
    if data.get("schema_version") != SCHEMA_VERSION:
        cfg.diagnostics.append(
            Diagnostic(
                "error",
                str(path),
                f"schema_version must be {SCHEMA_VERSION} (got {data.get('schema_version')!r})",
            )
        )
        return None
    hits: list[str] = []
    _scan_secrets(data, path.name, hits)
    for h in hits:
        cfg.diagnostics.append(Diagnostic("error", str(path), h))
    return data


def _err(cfg: GatewayConfig, source: str, e: ValidationError) -> None:
    for x in e.errors():
        loc = ".".join(str(p) for p in x["loc"])
        cfg.diagnostics.append(Diagnostic("error", source, f"{loc}: {x['msg']}"))


def load_config(root: Path) -> GatewayConfig:
    """Parse and validate. Never raises for content errors: inspect `cfg.errors`."""
    cfg = GatewayConfig(root=root)
    app_file = root / "app.yaml"
    if not app_file.is_file():
        cfg.diagnostics.append(Diagnostic("error", str(app_file), "missing app.yaml"))
        return cfg
    if (data := _read(app_file, cfg)) is not None:
        try:
            cfg.app = AppSection.model_validate(data.get("app", {}))
            cfg.telegram = TelegramConfig.model_validate(data.get("telegram", {}))
        except ValidationError as e:
            _err(cfg, str(app_file), e)
    for kind, (key, model, id_field) in _LISTS.items():
        for path in sorted((root / kind).glob("*.y*ml")):
            data = _read(path, cfg)
            if data is None:
                continue
            for i, raw in enumerate(data.get(key) or []):
                try:
                    item = model.model_validate(raw)
                except ValidationError as e:
                    _err(cfg, f"{path}[{i}]", e)
                    continue
                ident = getattr(item, id_field)
                bucket = getattr(cfg, kind)
                if ident in bucket:
                    cfg.diagnostics.append(
                        Diagnostic(
                            "error",
                            str(path),
                            f"duplicate {kind[:-1]} id {ident!r} (also in {cfg.sources[f'{kind}:{ident}']})",
                        )
                    )
                    continue
                bucket[ident] = item
                cfg.sources[f"{kind}:{ident}"] = str(path)
    for kind, key, model in (
        ("routing", "routing", RoutingConfig),
        ("policies", "policies", PolicyConfig),
    ):
        files = sorted((root / kind).glob("*.y*ml"))
        if len(files) > 1:
            cfg.diagnostics.append(
                Diagnostic("error", str(root / kind), f"exactly one {kind} file allowed")
            )
        for path in files[:1]:
            if (data := _read(path, cfg)) is not None:
                try:
                    setattr(cfg, kind, model.model_validate(data.get(key) or {}))
                except ValidationError as e:
                    _err(cfg, str(path), e)
    _validate_references(cfg)
    return cfg


def _validate_references(cfg: GatewayConfig) -> None:
    def add(level, kind, ident, msg):
        cfg.diagnostics.append(Diagnostic(level, cfg.sources.get(f"{kind}:{ident}", kind), msg))

    for a in cfg.agents.values():
        for c in a.capabilities:
            if c not in cfg.capabilities:
                add("error", "agents", a.id, f"agent {a.id}: unknown capability {c!r}")
            elif a.enabled and not cfg.capabilities[c].enabled:
                add("warning", "agents", a.id, f"agent {a.id}: capability {c!r} is disabled")
    for c in cfg.capabilities.values():
        for e in c.executors:
            if e.type == "provider" and e.ref not in cfg.providers:
                add("error", "capabilities", c.id, f"capability {c.id}: unknown provider {e.ref!r}")
            if e.type == "provider" and e.operation not in (
                None,
                "generate",
                "generate_structured",
            ):
                add(
                    "error",
                    "capabilities",
                    c.id,
                    f"capability {c.id}: unknown provider operation {e.operation!r}",
                )
            if e.type == "runtime" and e.ref not in RUNTIMES:
                add("error", "capabilities", c.id, f"capability {c.id}: unknown runtime {e.ref!r}")
            if e.type == "runtime" and e.operation not in ("new_execution", "return_to_caller"):
                add(
                    "error",
                    "capabilities",
                    c.id,
                    f"capability {c.id}: runtime operation must be new_execution|return_to_caller",
                )
            if e.type == "internal" and e.ref not in INTERNAL_OPS:
                add(
                    "error",
                    "capabilities",
                    c.id,
                    f"capability {c.id}: unknown internal op {e.ref!r}",
                )
            if e.type == "plugin" and "." not in e.ref:
                add(
                    "error",
                    "capabilities",
                    c.id,
                    f"capability {c.id}: plugin executor must be '<plugin>.<op>'",
                )
        try:
            jsonschema.Draft202012Validator.check_schema(c.input_schema)
            if c.output_schema:
                jsonschema.Draft202012Validator.check_schema(c.output_schema)
        except jsonschema.SchemaError as e:
            add("error", "capabilities", c.id, f"capability {c.id}: invalid schema: {e.message}")
    for s in cfg.mcp_servers.values():
        if bool(s.command) == bool(s.url):
            add(
                "error",
                "mcp_servers",
                s.name,
                f"mcp server {s.name}: set exactly one of command/url",
            )
        if s.url:
            u = urlparse(s.url)
            if (
                u.scheme not in ("http", "https")
                or (u.hostname or "") not in cfg.policies.mcp_url_hosts
            ):
                add(
                    "error",
                    "mcp_servers",
                    s.name,
                    f"mcp server {s.name}: url host {u.hostname!r} not in policies.mcp_url_hosts (SSRF guard)",
                )
    for w in cfg.workflows.values():
        for err in validate_structure(w):
            add("error", "workflows", w.id, f"workflow {w.id}: {err}")
        for s in [*w.steps, *w.compensations]:
            if s.executor == "capability":
                if s.ref not in cfg.capabilities:
                    add(
                        "error",
                        "workflows",
                        w.id,
                        f"workflow {w.id} step {s.id}: unknown capability {s.ref!r}",
                    )
                if s.agent and (
                    s.agent not in cfg.agents or s.ref not in cfg.agents[s.agent].capabilities
                ):
                    add(
                        "error",
                        "workflows",
                        w.id,
                        f"workflow {w.id} step {s.id}: agent {s.agent!r} does not declare capability {s.ref!r}",
                    )
            if s.executor == "internal" and s.ref not in INTERNAL_OPS:
                add(
                    "error",
                    "workflows",
                    w.id,
                    f"workflow {w.id} step {s.id}: unknown internal op {s.ref!r}",
                )
    r = cfg.routing
    seen = set()
    for wf_ref, where in (
        [(r.default_workflow, "default_workflow")]
        + [(x.workflow, f"route {x.id}") for x in r.routes]
        + [(x.workflow, f"feedback route {x.id}") for x in r.feedback_routes]
    ):
        if wf_ref is None:
            continue
        if wf_ref not in cfg.workflows:
            cfg.diagnostics.append(
                Diagnostic("error", "routing", f"{where}: unknown workflow {wf_ref!r}")
            )
        elif not cfg.workflows[wf_ref].enabled:
            cfg.diagnostics.append(
                Diagnostic("error", "routing", f"{where}: workflow {wf_ref!r} is disabled")
            )
    for x in r.routes:
        if x.id in seen:
            cfg.diagnostics.append(Diagnostic("error", "routing", f"duplicate route id {x.id!r}"))
        seen.add(x.id)


def load_or_raise(root: Path) -> GatewayConfig:
    cfg = load_config(root)
    if cfg.errors:
        raise ConfigError(cfg.errors)
    return cfg


def main(argv: list[str] | None = None) -> int:
    """`uv run python -m app.gateway_config [config_dir]`: validate and print diagnostics."""
    import sys

    args = argv if argv is not None else sys.argv[1:]
    root = Path(args[0] if args else "config")
    cfg = load_config(root)
    for d in cfg.diagnostics:
        print(d)
    print(
        f"{len(cfg.agents)} agents, {len(cfg.capabilities)} capabilities, {len(cfg.providers)} providers, "
        f"{len(cfg.mcp_servers)} mcp servers, {len(cfg.workflows)} workflows, {len(cfg.routing.routes)} routes, "
        f"{len(cfg.routing.feedback_routes)} feedback routes"
    )
    print("INVALID" if cfg.errors else "OK")
    return 1 if cfg.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
