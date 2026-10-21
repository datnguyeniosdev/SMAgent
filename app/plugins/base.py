"""Domain plugin contract and plugin manager.

A plugin contributes classification, workflow templates (WorkflowDef), tools,
operations ("plugin" steps) and validators. It only receives a PluginServices
facade: every tool call still goes through ToolExecutor -> ToolPolicyEngine, and
plugin-registered tools must use the plugin's id as prefix and may only request
permissions listed in its manifest AND granted by configuration.
"""

import importlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, Field

from app.intake import TaskRequest
from app.tools import ToolDefinition
from app.workflow import GateResult, WorkflowDef

log = logging.getLogger(__name__)


class OperationSpec(BaseModel):
    task_type: str
    description: str
    required_capabilities: list[str] = []  # e.g. "tool:data.transform", "provides:model.text"
    optional_capabilities: list[str] = []
    input_schema: dict | None = None  # validates TaskRequest.data
    side_effects: bool = False
    example: dict = {}


class PluginManifest(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,30}$")
    name: str
    version: str
    description: str
    categories: list[str] = []
    operations: list[OperationSpec]
    permissions: list[str] = []  # the most this plugin's tools may ever require
    artifact_types: list[str] = []
    keywords: list[str] = []  # classification hints


@dataclass
class Classification:
    plugin_id: str
    task_type: str | None
    score: float  # 0..1
    reasons: list[str] = field(default_factory=list)


@dataclass
class PlanResult:
    workflow: WorkflowDef | None = None
    question: str | None = None  # clarification needed instead of a plan
    options: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)


OperationFn = Callable[[Any, dict], Awaitable[Any]]  # (StepContext, inputs) -> StepOutput
ValidatorFn = Callable[[Any, dict], Awaitable[GateResult]]


class DomainPlugin(Protocol):
    manifest: PluginManifest

    def classify(self, req: TaskRequest, attachments: list) -> Classification | None: ...
    def plan(self, req: TaskRequest, task_type: str, env: Any) -> PlanResult: ...
    def tools(self, services: Any) -> list[ToolDefinition]: ...
    def operations(self) -> dict[str, OperationFn]: ...  # name -> fn, called as "<id>.<name>"
    def validators(self) -> dict[str, ValidatorFn]: ...
    def idempotent_operations(self) -> set[str]: ...


class PluginError(Exception):
    pass


@dataclass
class LoadedPlugin:
    plugin: DomainPlugin | None
    manifest: PluginManifest | None
    module: str
    active: bool = False
    error: str | None = None
    tool_names: list[str] = field(default_factory=list)


class PluginManager:
    def __init__(self, granted_permissions: frozenset[str]):
        self.granted = granted_permissions
        self.loaded: dict[str, LoadedPlugin] = {}

    def load(self, module_path: str, services: Any, registry) -> LoadedPlugin:
        """Import, validate and register one plugin. Errors are isolated to that plugin."""
        try:
            mod = importlib.import_module(module_path)
            plugin = mod.create_plugin()
            manifest = PluginManifest.model_validate(plugin.manifest.model_dump())
            if manifest.id in self.loaded and self.loaded[manifest.id].active:
                raise PluginError(f"duplicate plugin id {manifest.id}")
            allowed = frozenset(manifest.permissions) & self.granted
            names = []
            for t in plugin.tools(services):
                if not t.name.startswith(f"{manifest.id}."):
                    raise PluginError(f"tool {t.name} must be prefixed with '{manifest.id}.'")
                if not t.permissions <= allowed:
                    raise PluginError(
                        f"tool {t.name} needs {sorted(t.permissions - allowed)} not granted to plugin"
                    )
                registry.register(t)
                names.append(t.name)
            for op in plugin.operations():
                if "." in op:
                    raise PluginError(f"operation name {op!r} must not contain '.'")
            lp = LoadedPlugin(plugin, manifest, module_path, active=True, tool_names=names)
        except Exception as e:
            log.exception("plugin %s failed to load", module_path)
            lp = LoadedPlugin(None, None, module_path, error=f"{type(e).__name__}: {e}"[:300])
            self.loaded[module_path] = lp
            return lp
        self.loaded[manifest.id] = lp
        return lp

    def active(self) -> list[LoadedPlugin]:
        return [lp for lp in self.loaded.values() if lp.active and lp.plugin]

    def get(self, plugin_id: str) -> LoadedPlugin | None:
        lp = self.loaded.get(plugin_id)
        return lp if lp and lp.active and lp.plugin else None

    def set_active(self, plugin_id: str, active: bool) -> None:
        lp = self.loaded.get(plugin_id)
        if lp is None or lp.plugin is None:
            raise KeyError(plugin_id)
        lp.active = active

    def operation(self, ref: str) -> tuple[LoadedPlugin, OperationFn]:
        pid, _, name = ref.partition(".")
        lp = self.get(pid)
        if lp is None:
            raise PluginError(f"plugin {pid!r} is not active")
        fn = lp.plugin.operations().get(name)
        if fn is None:
            raise PluginError(f"plugin {pid!r} has no operation {name!r}")
        return lp, fn

    def validator(self, ref: str) -> ValidatorFn | None:
        pid, _, name = ref.partition(".")
        lp = self.get(pid)
        return lp.plugin.validators().get(name) if lp else None
