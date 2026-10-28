"""Game research plugin: collect what a game project offers as evidence and plan how to study the rest.

Engine knowledge sits in small adapters (marker files, folders to skip, tools that can drive a build);
the workflow itself is engine-agnostic. Phase 1: inventory (files + metadata + sha256 manifest) and a
runtime/playtest plan that names missing capabilities and falls back to an assisted mode.
"""

import fnmatch
import hashlib
import json
import re
from dataclasses import dataclass

from app.builtin_tools import resolve_in_workspace
from app.engine import StepOutput
from app.intake import TaskRequest
from app.plugins.base import Classification, OperationSpec, PlanResult, PluginManifest
from app.tools import ToolContext, ToolDefinition, ToolError, ToolOutput
from app.workflow import Criterion, StepDef, WorkflowDef

MAX_FILES = 5000  # ponytail: hard cap per inventory, page through subfolders if a game needs more
KEYWORDS = {
    "research": 0.5,
    "playtest": 0.6,
    "evidence": 0.4,
    "reverse": 0.4,
    "inventory": 0.4,
    "game": 0.15,
    "level": 0.15,
    "build": 0.1,
}


@dataclass(frozen=True)
class EngineAdapter:
    name: str
    source_markers: tuple[str, ...]  # globs (relative to the root) that mark a source tree
    build_markers: tuple[str, ...]  # globs that mark a shipped build
    skip_dirs: frozenset[str]  # generated folders that are not evidence
    control_tools: tuple[str, ...]  # tool-name globs that can launch and drive a build


ADAPTERS = [
    EngineAdapter("fixture", ("game.json",), ("build_info.json",), frozenset(), ("mcp.fixture.*",)),
    EngineAdapter(
        "unity",
        ("ProjectSettings/ProjectVersion.txt",),
        ("*_Data/globalgamemanagers", "*.apk", "*.ipa"),
        frozenset({"Library", "Temp", "Logs", "obj"}),
        ("mcp.unity.*",),
    ),
]
ALWAYS_SKIP = frozenset({".git", "node_modules", ".agw-children"})


def detect(root) -> tuple[EngineAdapter | None, str | None, str | None]:
    """(adapter, "source" | "build", marker path) for the first adapter whose markers match."""
    for kind in ("source", "build"):
        for a in ADAPTERS:
            for pattern in a.source_markers if kind == "source" else a.build_markers:
                hit = next((p for p in sorted(root.glob(pattern)) if p.is_file()), None)
                if hit:
                    return a, kind, str(hit.relative_to(root))
    return None, None, None


def _metadata(path) -> dict:
    text = path.read_text("utf-8", errors="replace")
    if path.suffix == ".json":
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"invalid_json": path.name}
    return {"text": text[:500]}


def _sha256(path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


async def tool_inventory(ctx: ToolContext, args: dict) -> ToolOutput:
    root = resolve_in_workspace(ctx, args.get("path", "."))
    if not root.is_dir():
        raise ToolError(f"not a folder: {args.get('path', '.')}", "not_found")
    adapter, mode, marker = detect(root)
    skip = ALWAYS_SKIP | (adapter.skip_dirs if adapter else frozenset())
    exclude = args.get("exclude", [])
    files, skipped = [], {"symlink": 0, "excluded": 0, "over_limit": 0}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if set(rel.parts) & skip:
            continue
        if p.is_symlink():  # a link may point outside the allowed scope
            skipped["symlink"] += 1
            continue
        if not p.is_file():
            continue
        if any(fnmatch.fnmatch(str(rel), g) for g in exclude):
            skipped["excluded"] += 1
            continue
        if len(files) >= MAX_FILES:
            skipped["over_limit"] += 1
            continue
        files.append({"path": str(rel), "size": p.stat().st_size, "sha256": _sha256(p)})
    manifest = {
        "engine": adapter and adapter.name,
        "mode": mode,
        "marker": marker,
        "metadata": _metadata(root / marker) if marker else {},
        "files": files,
        "skipped": skipped,
    }
    art = ctx.artifacts.add(
        ctx.task_id,
        "manifest",
        "evidence_manifest.json",
        json.dumps(manifest, indent=2, ensure_ascii=False),
        step_id=ctx.step_id,
        producer="game_research.inventory",
    )
    data = {k: v for k, v in manifest.items() if k != "files"}
    data |= {
        "files": len(files),
        "bytes": sum(f["size"] for f in files),
        "manifest_artifact": art.artifact_id,
    }
    return ToolOutput(
        f"{adapter and adapter.name} {mode}: {len(files)} files", data, [art.artifact_id]
    )


def runtime_plan(inv: dict, tools: list[str]) -> dict:
    """What the evidence covers, what is missing, and how to observe the running game."""
    engine, mode = inv.get("engine"), inv.get("mode")
    adapter = next((a for a in ADAPTERS if a.name == engine), None)
    control = sorted(
        t for t in tools if adapter and any(fnmatch.fnmatch(t, g) for g in adapter.control_tools)
    )
    limitations = []
    if mode == "source":
        available = ["source", "metadata", "level_data", "scripts"]
        missing = [] if control else ["runtime_control"]
    elif mode == "build":
        available = ["build", "metadata"]
        missing = ["source", "level_data", "scripts"] + ([] if control else ["runtime_control"])
        limitations.append(
            "build only: levels, rules and numbers must be observed by playing, not read from source"
        )
    else:
        available, missing = [], ["source", "build", "runtime_control"]
        limitations.append("no supported engine detected: only the file inventory is evidence")
    playable = mode in ("source", "build")
    if playable and control:
        how = "automated"
        steps = [
            f"launch the {mode} with {control[0]}",
            "play each level, capture screenshots and state at fixed intervals",
            "record observed tiles, rules and numbers with their capture as evidence",
        ]
    elif playable:
        how = "assisted"
        limitations.append(
            f"no tool can drive {engine} builds (looked for {', '.join(adapter.control_tools)}): "
            "a person runs the game and shares captures"
        )
        steps = [
            f"operator launches the {mode} on a device or editor",
            "operator plays each level and shares screenshots or a recording per level",
            "agent records only what the captures show; the rest stays unknown",
        ]
    else:
        how, steps = "none", []
    return {
        "engine": engine,
        "mode": mode,
        "available": available,
        "missing_capabilities": missing,
        "control": how,
        "control_tools": control,
        "playtest_plan": steps,
        "limitations": limitations,
    }


class GameResearchPlugin:
    manifest = PluginManifest(
        id="game_research",
        name="Game research",
        version="0.1.0",
        description="Inventory a game (source or build) as checksummed evidence and plan how to "
        "observe the rest by playtest, automated or assisted.",
        categories=["game-development", "research"],
        keywords=list(KEYWORDS),
        permissions=["workspace:read", "artifact:write"],
        artifact_types=["manifest", "report"],
        operations=[
            OperationSpec(
                task_type="research",
                description="Evidence inventory (files, metadata, sha256) + runtime/playtest plan",
                required_capabilities=["tool:game_research.inventory"],
            ),
        ],
    )

    def classify(self, req: TaskRequest, attachments: list) -> Classification | None:
        words = set(re.findall(r"[a-z]+", req.objective.lower()))
        hits = words & set(KEYWORDS)
        score = sum(KEYWORDS[w] for w in hits)
        if score < 0.5:
            return None
        return Classification(
            "game_research", "research", min(score, 1.0), [f"keywords: {sorted(hits)}"]
        )

    def plan(self, req: TaskRequest, task_type: str, env) -> PlanResult:
        if not env.workspace_configured:
            return PlanResult(question="Set WORKSPACE_ROOT to the game (source or build) first.")
        args = {k: env.config[k] for k in ("exclude",) if k in env.config}
        steps = [
            StepDef(id="inventory", executor="tool", ref="game_research.inventory", inputs=args),
            StepDef(
                id="runtime",
                executor="plugin",
                ref="game_research.runtime_plan",
                depends_on=["inventory"],
                inputs={"inventory": "$steps.inventory.output", "tools": env.tools},
            ),
        ]
        return PlanResult(
            WorkflowDef(
                id="game_research.research",
                domain="game_research",
                steps=steps,
                success_criteria=[Criterion(kind="step_succeeded", step="runtime")],
                outputs={
                    "inventory": "$steps.inventory.output",
                    "runtime": "$steps.runtime.output",
                },
            )
        )

    def tools(self, services) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                "game_research.inventory",
                "Collect a game folder's files (path, size, sha256) and engine metadata into an "
                "evidence manifest; symlinks and generated folders are left out.",
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "maxLength": 300},
                        "exclude": {"type": "array", "items": {"type": "string", "maxLength": 200}},
                    },
                    "additionalProperties": False,
                },
                tool_inventory,
                frozenset({"workspace:read", "artifact:write"}),
                timeout_s=300,
                source="plugin:game_research",
            )
        ]

    def idempotent_operations(self) -> set[str]:
        return {"runtime_plan"}

    def operations(self) -> dict:
        return {"runtime_plan": self.op_runtime_plan}

    def validators(self) -> dict:
        return {}

    async def op_runtime_plan(self, ctx, inputs: dict) -> StepOutput:
        plan = runtime_plan(inputs.get("inventory") or {}, inputs.get("tools") or [])
        lines = [
            f"# Runtime plan: {plan['engine']} ({plan['mode']})",
            f"control: {plan['control']}",
        ]
        lines += [f"- missing: {m}" for m in plan["missing_capabilities"]]
        lines += [f"- limitation: {x}" for x in plan["limitations"]]
        lines += [f"{i}. {s}" for i, s in enumerate(plan["playtest_plan"], 1)]
        art = ctx.add_artifact("report", "runtime_plan.md", "\n".join(lines) + "\n")
        return StepOutput(plan, [art.artifact_id])


def create_plugin():
    return GameResearchPlugin()
