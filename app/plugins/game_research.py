"""Game research plugin: collect what a game project offers as evidence and plan how to study the rest.

Engine knowledge sits in small adapters (marker files, folders to skip, tools that can drive a build);
the workflow itself is engine-agnostic: inventory (files + metadata + sha256 manifest), an integrity
check against that manifest or an earlier baseline, conflict records where sources disagree (both
claims kept), and a runtime/playtest plan that names missing capabilities and falls back to an
assisted mode.
"""

import fnmatch
import hashlib
import json
import re
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, Field, ValidationError

from app.builtin_tools import resolve_in_workspace
from app.engine import StepError, StepOutput
from app.intake import TaskRequest
from app.plugins.base import Classification, OperationSpec, PlanResult, PluginManifest
from app.storage import AttachmentError
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


class EngineAdapter(BaseModel):
    """All an engine needs: data, no code. More come from PLUGIN_CONFIG game_research.adapters."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,30}$")
    source_markers: list[str] = Field(min_length=1)  # globs (relative to the root): a source tree
    build_markers: list[str] = Field(min_length=1)  # globs that mark a shipped build
    skip_dirs: list[str] = []  # generated folders that are not evidence
    control_tools: list[str] = []  # tool-name globs that can launch and drive a build


ADAPTERS = [
    EngineAdapter(
        name="fixture",
        source_markers=["game.json"],
        build_markers=["build_info.json"],
        control_tools=["mcp.fixture.*"],
    ),
    EngineAdapter(
        name="unity",
        source_markers=["ProjectSettings/ProjectVersion.txt"],
        build_markers=["*_Data/globalgamemanagers", "*.apk", "*.ipa"],
        skip_dirs=["Library", "Temp", "Logs", "obj"],
        control_tools=["mcp.unity.*"],
    ),
]
ALWAYS_SKIP = frozenset({".git", "node_modules", ".agw-children"})


def adapters(extra: list[dict] = ()) -> list[EngineAdapter]:
    """Built-in adapters, then configured ones (checked in that order)."""
    try:
        out = ADAPTERS + [EngineAdapter.model_validate(a) for a in extra]
    except ValidationError as e:
        raise ToolError(f"invalid engine adapter: {e.errors()[0]['msg']}", "invalid_input") from e
    names = [a.name for a in out]
    if len(set(names)) != len(names):
        raise ToolError(f"duplicate engine adapter names: {names}", "invalid_input")
    return out


def detect(
    root, candidates: list[EngineAdapter]
) -> tuple[EngineAdapter | None, str | None, str | None]:
    """(adapter, "source" | "build", marker path) for the first adapter whose markers match."""
    for kind in ("source", "build"):
        for a in candidates:
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
    adapter, mode, marker = detect(root, adapters(args.get("adapters", [])))
    skip = ALWAYS_SKIP | set(adapter.skip_dirs if adapter else ())
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
        "root": args.get("path", "."),
        "engine": adapter and adapter.name,
        "adapter": adapter and adapter.model_dump(),
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


TEXT_EVIDENCE = {".md", ".txt"}
CODE_EVIDENCE = {
    ".lua",
    ".cs",
    ".gd",
    ".py",
    ".js",
    ".ts",
    ".json",
    ".yaml",
    ".yml",
    ".cfg",
    ".ini",
    ".toml",
}
MAX_FACT_BYTES = 256 * 1024
CODE_FACT = re.compile(r"[\"']?([A-Za-z_]\w*)[\"']?\s*[:=]\s*(-?\d+(?:\.\d+)?)\b")
PROSE_FACT = re.compile(r"\b([A-Za-z_]{3,})\s+(?:at\s+|of\s+|is\s+)?(-?\d+(?:\.\d+)?)\b")


def _key(word: str) -> str:
    k = word.lower()
    return k[:-1] if len(k) > 4 and k.endswith("s") and not k.endswith("ss") else k


def extract_facts(path: str, text: str, digest: str) -> list[dict]:
    """Numeric claims ("speed = 6", "runs at speed 5") with where they were read."""
    suffix = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
    patterns = [CODE_FACT] + ([PROSE_FACT] if suffix in TEXT_EVIDENCE else [])
    facts = []
    for n, line in enumerate(text.splitlines(), 1):
        seen = set()
        for pattern in patterns:
            for m in pattern.finditer(line):
                key, value = _key(m.group(1)), str(float(m.group(2))).removesuffix(".0")
                if (key, value) in seen:
                    continue
                seen.add((key, value))
                facts.append({"fact": key, "value": value, "source": path, "line": n,
                              "quote": line.strip()[:200], "sha256": digest})  # fmt: skip
    return facts


def conflicts(facts: list[dict]) -> list[dict]:
    """One open record per fact that sources disagree on; every claim is kept as evidence.
    ponytail: facts are keyed by name only, so per-level or per-object values of the same name
    also show up as conflicts; scope keys (file/level/object) when that gets noisy."""
    by_key: dict[str, list[dict]] = {}
    for f in facts:
        by_key.setdefault(f["fact"], []).append(f)
    out = []
    for key, claims in sorted(by_key.items()):
        if len({c["value"] for c in claims}) > 1 and len({c["source"] for c in claims}) > 1:
            keep = ("value", "source", "line", "quote", "sha256")
            out.append({"fact": key, "status": "open",
                        "claims": [{k: c[k] for k in keep} for c in claims]})  # fmt: skip
    return out


async def tool_evidence(ctx: ToolContext, args: dict) -> ToolOutput:
    """Check the evidence against its checksums (this run's manifest, and a baseline manifest from
    an earlier run when given), then extract facts from unchanged files and record conflicts."""
    current = json.loads(ctx.read_input(args["manifest"])[2])
    baseline = json.loads(ctx.read_input(args["baseline"])[2]) if args.get("baseline") else current
    now_files = {f["path"]: f["sha256"] for f in current["files"]}
    then_files = {f["path"]: f["sha256"] for f in baseline["files"]}
    changed = sorted(
        p for p in now_files.keys() & then_files.keys() if now_files[p] != then_files[p]
    )
    missing = sorted(then_files.keys() - now_files.keys())
    added = sorted(now_files.keys() - then_files.keys()) if args.get("baseline") else []
    root = resolve_in_workspace(ctx, current.get("root", "."))
    facts = []
    for path, digest in now_files.items():
        suffix = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if path in changed or suffix not in TEXT_EVIDENCE | CODE_EVIDENCE:
            continue
        p = root / path
        if p.is_symlink() or not p.is_file() or p.stat().st_size > MAX_FACT_BYTES:
            continue
        data = p.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:  # changed since this run's inventory
            changed.append(path)
            continue
        facts += extract_facts(path, data.decode("utf-8", errors="replace"), digest)
    found = conflicts(facts)
    integrity = {"ok": not (changed or missing), "changed": sorted(changed), "missing": missing,
                 "added": added, "baseline": args.get("baseline") or args["manifest"]}  # fmt: skip
    art = ctx.artifacts.add(
        ctx.task_id,
        "report",
        "evidence_check.json",
        json.dumps({"integrity": integrity, "facts": facts, "conflicts": found}, indent=2,
                   ensure_ascii=False),
        step_id=ctx.step_id,
        producer="game_research.evidence",
    )  # fmt: skip
    summary = f"{len(facts)} facts, {len(found)} conflicts, integrity {'ok' if integrity['ok'] else 'CHANGED'}"
    data = {"integrity": integrity, "facts": len(facts), "conflicts": found,
            "evidence_artifact": art.artifact_id}  # fmt: skip
    return ToolOutput(summary, data, [art.artifact_id])


UNKNOWN, DISPUTED = "?", "!"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "default"


def level_coverage(observations: list[dict], expected: list[str]) -> dict:
    """Merge playtest observations into per-level maps. A cell is known only when a capture shows it;
    unseen cells stay "?" and cells two captures disagree on are "!" with both readings kept.
    Nothing is inferred: no symmetry, no "probably floor"."""
    levels: dict[str, dict] = {}
    for obs in observations:
        lv = levels.setdefault(obs["level"], {"width": obs["width"], "height": obs["height"],
                                              "cells": {}, "sources": []})  # fmt: skip
        if (lv["width"], lv["height"]) != (obs["width"], obs["height"]):
            raise ValueError(f"{obs['source']}: level {obs['level']} size differs between captures")
        lv["sources"].append(obs["source"])
        for y, row in enumerate(obs["rows"][: obs["height"]]):
            for x, ch in enumerate(row[: obs["width"]]):
                if ch != UNKNOWN:
                    lv["cells"].setdefault((x, y), {}).setdefault(ch, []).append(obs["source"])
    out = []
    for name, lv in sorted(levels.items()):
        w, h, rows, unknown, disputes = lv["width"], lv["height"], [], [], []
        for y in range(h):
            row = ""
            for x in range(w):
                seen = lv["cells"].get((x, y), {})
                if not seen:
                    row += UNKNOWN
                    unknown.append([x, y])
                elif len(seen) > 1:
                    row += DISPUTED
                    disputes.append({"cell": [x, y], "readings": [
                        {"value": v, "sources": src} for v, src in sorted(seen.items())]})  # fmt: skip
                else:
                    row += next(iter(seen))
            rows.append(row)
        known = w * h - len(unknown) - len(disputes)
        out.append({"level": name, "width": w, "height": h, "map": rows,
                    "coverage": round(known / (w * h), 3), "known": known, "unknown": unknown,
                    "disputed": disputes, "sources": lv["sources"]})  # fmt: skip
    seen_names = {lv["level"] for lv in out}
    return {
        "levels": out,
        "unobserved_levels": [x for x in expected if x not in seen_names],
        "expected_levels": expected or None,  # None: the evidence does not say how many there are
    }


def _observation(name: str, raw: bytes) -> dict | None:
    try:
        obs = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None  # a screenshot or video: someone still has to transcribe it
    ok = (
        isinstance(obs, dict)
        and isinstance(obs.get("level"), str)
        and all(isinstance(obs.get(k), int) and obs[k] > 0 for k in ("width", "height"))
        and isinstance(obs.get("rows"), list)
        and all(isinstance(r, str) for r in obs["rows"])
    )
    return {**obs, "source": name} if ok else None


def runtime_plan(inv: dict, tools: list[str]) -> dict:
    """What the evidence covers, what is missing, and how to observe the running game."""
    engine, mode = inv.get("engine"), inv.get("mode")
    globs = (inv.get("adapter") or {}).get("control_tools", [])
    control = sorted(t for t in tools if any(fnmatch.fnmatch(t, g) for g in globs))
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
            f"no tool can drive {engine} builds (looked for {', '.join(globs) or 'none'}): "
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
                description="Evidence inventory (files, metadata, sha256), integrity check, conflict records "
                "and a runtime/playtest plan",
                required_capabilities=["tool:game_research.inventory"],
            ),
            OperationSpec(
                task_type="design",
                description="Save a new design to the creative workspace, citing research artifacts "
                "(artifact_ids) by checksum; research evidence is never written",
                input_schema={
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "minLength": 1, "maxLength": 120},
                        "content": {"type": "string", "minLength": 1, "maxLength": 200_000},
                    },
                    "required": ["title", "content"],
                },
                side_effects=True,
            ),
        ],
    )

    def classify(self, req: TaskRequest, attachments: list) -> Classification | None:
        words = set(re.findall(r"[a-z]+", req.objective.lower()))
        hits = words & set(KEYWORDS)
        score = sum(KEYWORDS[w] for w in hits)
        if score < 0.5:
            return None
        ttype = "design" if "creative" in words else "research"
        return Classification(
            "game_research", ttype, min(score, 1.0), [f"keywords: {sorted(hits)}"]
        )

    def plan(self, req: TaskRequest, task_type: str, env) -> PlanResult:
        if task_type == "design":
            if not req.data.get("content"):
                return PlanResult(question="Send the design as data.title and data.content.")
            step = StepDef(
                id="design",
                executor="plugin",
                ref="game_research.save_design",
                inputs={
                    "title": req.data.get("title") or req.objective[:120],
                    "content": req.data["content"],
                    "references": req.artifact_ids,
                },
            )
            return PlanResult(
                WorkflowDef(
                    id="game_research.design",
                    domain="game_research",
                    steps=[step],
                    outputs={"design": "$steps.design.output"},
                )
            )
        if not env.workspace_configured:
            return PlanResult(question="Set WORKSPACE_ROOT to the game (source or build) first.")
        args = {k: env.config[k] for k in ("exclude", "adapters") if k in env.config}
        steps = [
            StepDef(id="inventory", executor="tool", ref="game_research.inventory", inputs=args),
            StepDef(
                id="evidence",
                executor="tool",
                ref="game_research.evidence",
                depends_on=["inventory"],
                inputs={"manifest": "$steps.inventory.output.manifest_artifact"}
                | ({"baseline": req.artifact_ids[0]} if req.artifact_ids else {}),
            ),
            StepDef(
                id="coverage",
                executor="plugin",
                ref="game_research.coverage",
                depends_on=["inventory"],
                inputs={"inventory": "$steps.inventory.output", "captures": "$task.attachment_ids"},
            ),
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
                success_criteria=[
                    Criterion(kind="step_succeeded", step="evidence"),
                    Criterion(kind="step_succeeded", step="coverage"),
                    Criterion(kind="step_succeeded", step="runtime"),
                ],
                outputs={
                    "inventory": "$steps.inventory.output",
                    "evidence": "$steps.evidence.output",
                    "coverage": "$steps.coverage.output",
                    "runtime": "$steps.runtime.output",
                },
            )
        )

    def tools(self, services) -> list[ToolDefinition]:
        ref = {"type": "string", "pattern": "^art_[0-9a-f]+$"}
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
                        "adapters": {"type": "array", "items": {"type": "object"}, "maxItems": 50},
                    },
                    "additionalProperties": False,
                },
                tool_inventory,
                frozenset({"workspace:read", "artifact:write"}),
                timeout_s=300,
                source="plugin:game_research",
            ),
            ToolDefinition(
                "game_research.evidence",
                "Verify evidence files against a manifest's sha256 (and an earlier baseline "
                "manifest), extract numeric facts and record conflicts between sources.",
                {
                    "type": "object",
                    "properties": {"manifest": ref, "baseline": ref},
                    "required": ["manifest"],
                    "additionalProperties": False,
                },
                tool_evidence,
                frozenset({"workspace:read", "artifact:write"}),
                timeout_s=300,
                source="plugin:game_research",
            ),
        ]

    def idempotent_operations(self) -> set[str]:
        return {"runtime_plan", "coverage"}

    def operations(self) -> dict:
        return {
            "runtime_plan": self.op_runtime_plan,
            "coverage": self.op_coverage,
            "save_design": self.op_save_design,
        }

    def validators(self) -> dict:
        return {}

    async def op_coverage(self, ctx, inputs: dict) -> StepOutput:
        inv = inputs.get("inventory") or {}
        observations, untranscribed = [], []
        for ref in inputs.get("captures") or []:
            name, _, raw = ctx.read_input(ref)  # checksum-verified
            obs = _observation(name, raw)
            (observations.append(obs) if obs else untranscribed.append(name))
        levels = (inv.get("metadata") or {}).get("levels")
        expected = [PurePosixPath(x).stem for x in levels] if isinstance(levels, list) else []
        try:
            cov = level_coverage(observations, expected)
        except ValueError as e:
            raise StepError(str(e), "invalid_input") from e
        limits = []
        if cov["expected_levels"] is None:
            limits.append(
                "the evidence does not say how many levels exist; unobserved levels are unknown"
            )
        if cov["unobserved_levels"]:
            limits.append(f"never observed: {', '.join(cov['unobserved_levels'])}")
        for lv in cov["levels"]:
            if lv["unknown"] or lv["disputed"]:
                limits.append(f"{lv['level']}: {lv['coverage']:.0%} observed, "
                              f"{len(lv['unknown'])} cells unknown, {len(lv['disputed'])} disputed")  # fmt: skip
        if untranscribed:
            limits.append(f"captures not transcribed into a level map: {', '.join(untranscribed)}")
        cov |= {"untranscribed": untranscribed, "limitations": limits}
        lines = ["# Level coverage", ""] + [
            f"- {x}" for x in limits or ["every observed level is complete"]
        ]
        for lv in cov["levels"]:
            lines += ["", f"## {lv['level']} ({lv['coverage']:.0%})", "```", *lv["map"], "```"]
        arts = [
            ctx.add_artifact(
                "report", "coverage.json", json.dumps(cov, indent=2, ensure_ascii=False)
            ),
            ctx.add_artifact("report", "coverage.md", "\n".join(lines) + "\n"),
        ]
        return StepOutput(cov, [a.artifact_id for a in arts])

    async def op_save_design(self, ctx, inputs: dict) -> StepOutput:
        """New designs live in the creative workspace, outside the game folder and the evidence
        stores; research artifacts are cited by id and sha256, read through their checksum."""
        conf = ctx.plugin_config("game_research")
        root = Path(conf.get("creative_root") or ctx.settings.data_dir / "creative").resolve()
        guarded = [ctx.settings.workspace_root, ctx.svc.artifacts.root, ctx.svc.attachments.root]
        for g in filter(None, guarded):
            g = Path(g).resolve()
            if root.is_relative_to(g) or g.is_relative_to(root):
                raise StepError(f"creative workspace must be outside {g}", "forbidden")
        refs = []
        for ref in inputs.get("references") or []:
            art = ctx.artifact(ref)
            try:
                ctx.read_artifact(ref)
            except AttachmentError as e:  # evidence changed on disk: refuse before writing anything
                raise StepError(str(e), "integrity") from e
            refs.append({"artifact_id": ref, "filename": art.filename, "sha256": art.sha256})
        project = _slug(
            Path(ctx.settings.workspace_root).name if ctx.settings.workspace_root else ""
        )
        folder = root / project
        folder.mkdir(parents=True, exist_ok=True)
        base = _slug(inputs["title"])
        header = ["---", f"title: {json.dumps(inputs['title'], ensure_ascii=False)}",
                  f"task: {ctx.task_id}", "references:"]  # fmt: skip
        header += [f"  - {json.dumps(r)}" for r in refs] or ["  []"]
        data = ("\n".join(header + ["---", ""]) + inputs["content"].rstrip() + "\n").encode()
        for n in range(1, 1000):  # new version, never overwrite an earlier design
            path = folder / (f"{base}.md" if n == 1 else f"{base}-v{n}.md")
            try:
                with path.open("xb") as f:
                    f.write(data)
                break
            except FileExistsError:
                continue
        else:
            raise StepError(f"too many versions of {base}", "conflict")
        out = {"path": str(path), "version": n, "sha256": hashlib.sha256(data).hexdigest(),
               "references": refs, "creative_root": str(root)}  # fmt: skip
        return StepOutput(out)

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
