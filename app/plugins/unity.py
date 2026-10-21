"""Unity plugin. All Unity knowledge (project layout, Unity-MCP guidance, asset folder,
design -> assets -> implement -> review pipeline) lives here, not in the core.

Unity-MCP / Unity CLI tools are used only when actually discovered (MCP_SERVERS allowlist).
"""

import json
import re
from pathlib import Path

from app import formatting as fmt
from app.assets import AssetRules, ManifestEntry, check_manifest, manifest_dict, manifest_entry
from app.builtin_tools import resolve_in_workspace
from app.connectors.base import AgentRequest, Cap, ConnectorError
from app.engine import StepError, StepOutput
from app.intake import TaskRequest
from app.plugins.base import Classification, OperationSpec, PlanResult, PluginManifest
from app.tools import ToolContext, ToolDefinition, ToolError, ToolOutput
from app.workflow import Criterion, GateResult, RetryPolicy, StepDef, WorkflowDef

UNITY_SYSTEM = (
    "This is a Unity project. If Unity-MCP tools are available, list them before use, use only "
    "tools that exist, and verify each editor operation's result; if they are unavailable, say so "
    "instead of claiming editor changes. Respect existing scenes, prefabs, scripts and packages."
)
DESIGNER_SYSTEM = (
    "You are the designer. Analyse the references and requirements, write testable acceptance "
    "criteria, an implementation-ready design spec, and image-generation prompts for any required "
    "assets (PNG; state exact pixel size and whether transparency is needed). Use snake_case asset "
    "names with no extension."
)
DESIGN_SCHEMA = {
    "type": "object",
    "properties": {
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
        "design_spec": {"type": "string"},
        "assets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "prompt": {"type": "string"},
                    "width": {"type": "integer"},
                    "height": {"type": "integer"},
                    "transparent": {"type": "boolean"},
                },
                "required": ["name", "prompt", "width", "height", "transparent"],
            },
        },
    },
    "required": ["acceptance_criteria", "design_spec", "assets"],
}
KEYWORDS = {
    "unity": 0.9,
    "scene": 0.3,
    "prefab": 0.6,
    "monobehaviour": 0.7,
    "gameobject": 0.6,
    "sprite": 0.35,
    "shader": 0.3,
    "animator": 0.5,
    "game": 0.15,
    "editor": 0.15,
}


async def tool_project_info(ctx: ToolContext, args: dict) -> ToolOutput:
    root = resolve_in_workspace(ctx, args.get("path", "."))
    ver = root / "ProjectSettings" / "ProjectVersion.txt"
    if not ver.is_file() and "path" not in args:
        # Repos often keep the Unity project in a subfolder (e.g. 3_dev/<game>/); look 3 levels down.
        found = sorted(
            p
            for p in root.glob("*/*/ProjectSettings/ProjectVersion.txt")
            if "Library" not in p.parts
        )
        found += sorted(root.glob("*/ProjectSettings/ProjectVersion.txt"))
        if found:
            ver = found[0]
            root = resolve_in_workspace(
                ctx, str(ver.parent.parent.relative_to(ctx.workspace.resolve()))
            )
    if not ver.is_file():
        raise ToolError(
            "not a Unity project (ProjectSettings/ProjectVersion.txt missing)", "not_found"
        )
    m = re.search(r"m_EditorVersion:\s*(\S+)", ver.read_text("utf-8", errors="replace"))
    packages = {}
    manifest = root / "Packages" / "manifest.json"
    if manifest.is_file():
        try:
            packages = json.loads(manifest.read_text("utf-8")).get("dependencies", {})
        except json.JSONDecodeError as e:
            raise ToolError(f"Packages/manifest.json is invalid JSON: {e}", "invalid_input") from e
    assets = root / "Assets"
    counts = {"scenes": 0, "prefabs": 0, "scripts": 0, "materials": 0}
    if assets.is_dir():
        for p in assets.rglob("*"):
            if p.is_symlink():
                continue
            suffix = p.suffix.lower()
            key = {
                ".unity": "scenes",
                ".prefab": "prefabs",
                ".cs": "scripts",
                ".mat": "materials",
            }.get(suffix)
            if key:
                counts[key] += 1
    data = {
        "project_path": str(root.relative_to(ctx.workspace.resolve())),
        "editor_version": m.group(1) if m else None,
        "packages": packages,
        "unity_mcp_package": next((k for k in packages if "mcp" in k.lower()), None),
        **counts,
    }
    return ToolOutput(f"Unity {data['editor_version']}: {counts}", data)


class UnityPlugin:
    manifest = PluginManifest(
        id="unity",
        name="Unity",
        version="1.0.0",
        description="Unity project inspection, editor queries via discovered Unity-MCP tools, and the "
        "design -> assets -> implement -> review pipeline.",
        categories=["unity", "game-development"],
        keywords=list(KEYWORDS),
        permissions=["workspace:read"],
        artifact_types=["report", "image", "manifest", "diff"],
        operations=[
            OperationSpec(
                task_type="inspect_project",
                description="Editor version, packages, scene/prefab/script counts",
                required_capabilities=["tool:unity.project_info"],
            ),
            OperationSpec(
                task_type="editor_status",
                description="Query the running Unity Editor through a discovered MCP tool",
                required_capabilities=["tool:mcp.unity.editor_status"],
            ),
            OperationSpec(
                task_type="feature",
                description="Design -> assets -> implement in worktree -> QA -> independent review",
                required_capabilities=["workspace.write", "model.structured"],
                side_effects=True,
            ),
        ],
    )

    def classify(self, req: TaskRequest, attachments: list) -> Classification | None:
        words = set(re.findall(r"[a-z]+", req.objective.lower()))
        hits = words & set(KEYWORDS)
        score = sum(KEYWORDS[w] for w in hits)
        if not score:
            return None
        if hits & {"inspect", "packages"} or re.search(
            r"\b(inspect|analy[sz]e|list)\b", req.objective.lower()
        ):
            ttype = "inspect_project"
        elif "status" in words or "editor" in words and "running" in words:
            ttype = "editor_status"
        else:
            ttype = "feature"
        return Classification("unity", ttype, min(score, 1.0), [f"keywords: {sorted(hits)}"])

    def plan(self, req: TaskRequest, task_type: str, env) -> PlanResult:
        if task_type == "inspect_project":
            if not env.workspace_configured:
                return PlanResult(question="Set WORKSPACE_ROOT to the Unity project first.")
            steps = [
                StepDef(id="info", executor="tool", ref="unity.project_info"),
                StepDef(
                    id="check",
                    executor="validator",
                    ref="unity.project_valid",
                    depends_on=["info"],
                    inputs={"info": "$steps.info.output"},
                ),
            ]
            return PlanResult(
                WorkflowDef(
                    id="unity.inspect_project",
                    domain="unity",
                    steps=steps,
                    success_criteria=[Criterion(kind="gate_passed", step="check")],
                    outputs={"project": "$steps.info.output"},
                )
            )
        if task_type == "editor_status":
            steps = [StepDef(id="status", executor="tool", ref="mcp.unity.editor_status")]
            return PlanResult(
                WorkflowDef(
                    id="unity.editor_status",
                    domain="unity",
                    steps=steps,
                    outputs={"status": "$steps.status.output"},
                )
            )
        if not env.workspace_configured:
            return PlanResult(
                question="Set WORKSPACE_ROOT to the Unity project before requesting changes."
            )
        step = StepDef(
            id="feature",
            executor="plugin",
            ref="unity.feature",
            timeout_s=7200,
            retry=RetryPolicy(max_attempts=1),
            required_capabilities=["workspace.write"],
        )
        return PlanResult(
            WorkflowDef(
                id="unity.feature",
                domain="unity",
                steps=[step],
                timeout_s=7200,
                outputs={"result": "$steps.feature.output"},
            )
        )

    def tools(self, services) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                "unity.project_info",
                "Read Unity project metadata (ProjectVersion, Packages/manifest.json, asset counts).",
                {
                    "type": "object",
                    "properties": {"path": {"type": "string", "maxLength": 300}},
                    "additionalProperties": False,
                },
                tool_project_info,
                frozenset({"workspace:read"}),
                timeout_s=60,
                source="plugin:unity",
            )
        ]

    def idempotent_operations(self) -> set[str]:
        return set()

    def operations(self) -> dict:
        return {"feature": self.op_feature}

    def validators(self) -> dict:
        return {"project_valid": self.gate_project_valid}

    async def gate_project_valid(self, ctx, inputs: dict) -> GateResult:
        info = inputs.get("info") or {}
        findings = []
        if not info.get("editor_version"):
            findings.append({"severity": "high", "message": "editor version not found"})
        if not info.get("packages"):
            findings.append(
                {"severity": "medium", "message": "no packages listed in Packages/manifest.json"}
            )
        if not info.get("unity_mcp_package"):
            findings.append(
                {
                    "severity": "info",
                    "message": "no MCP package in manifest: editor tools unavailable",
                }
            )
        return GateResult(
            validator_id="unity.project_valid",
            status="failed" if any(f["severity"] == "high" for f in findings) else "passed",
            findings=findings,
            metrics={k: info.get(k) for k in ("scenes", "prefabs", "scripts")},
        )

    # ---------- design -> assets -> implement -> review (moved from the Phase 1 core) ----------

    async def op_feature(self, ctx, inputs: dict) -> StepOutput:
        svc, task = ctx.svc, ctx.task
        asset_dir = ctx.plugin_config("unity").get("asset_dir", "Assets/Generated")
        atts = svc._task_attachments(task)
        designer = svc.router.route(svc_caps(atts) | {Cap.structured_output}, svc._pref("designer"))
        implementer = svc.router.route({Cap.workspace_write}, svc._pref("implementer"), task.agent)
        await ctx.approval(
            f"🔐 Design ({designer.name}) → implement ({implementer.name}, isolated worktree) → "
            f"QA → independent review for {fmt.task_line(task)}. Approve?"
        )
        d_step = svc._child(task, "designer", "Design spec")
        req = AgentRequest(
            task_id=d_step.task_id,
            prompt=task.description,
            system=DESIGNER_SYSTEM,
            attachments=atts,
            json_schema=DESIGN_SCHEMA,
            workspace=svc._scratch(d_step.task_id) if Cap.workspace_read in designer.caps else None,
        )
        res = await svc._call(d_step, designer, req, "designer")
        try:
            design = res.structured or json.loads(res.text)
            specs = design["assets"]
        except (ValueError, KeyError, TypeError) as e:
            raise ConnectorError("designer returned invalid design JSON", "provider") from e
        svc.artifacts.add(
            d_step.task_id,
            "design_spec",
            "design_spec.md",
            design["design_spec"],
            producer=designer.name,
        )
        prompt_art = svc.artifacts.add(
            d_step.task_id,
            "prompt",
            "asset_prompts.json",
            json.dumps(specs, indent=2),
            producer=designer.name,
        )
        d_step.result = "\n".join(design["acceptance_criteria"])
        svc._set_state(d_step, svc_state("succeeded"))

        a_step = svc._child(task, "asset_processor", "Assets", [d_step])
        entries, images = await self._generate_assets(ctx, a_step, specs, atts, prompt_art)
        problems = check_manifest(entries, [s["name"] for s in specs])
        report = fmt.manifest_report(entries) + ("\n" + fmt.bullet(problems) if problems else "")
        if images:
            await ctx.approval(report + "\n\nApprove these assets for integration?", images)
            for e in entries:
                e.approval_status = "approved"
        else:
            await ctx.notify(report)
        svc.artifacts.add(
            a_step.task_id,
            "manifest",
            "asset_manifest.json",
            json.dumps({"assets": manifest_dict(entries), "problems": problems}, indent=2),
        )
        a_step.result = f"{len(entries)} assets, {len(problems)} problems"
        svc._set_state(a_step, svc_state("succeeded"))

        ws = await svc.workspaces.create(task.task_id)
        target = (ws.path / asset_dir).resolve()
        if not target.is_relative_to(ws.path.resolve()):
            raise StepError("unity asset_dir escapes the worktree", "forbidden")
        target.mkdir(parents=True, exist_ok=True)
        for e in entries:
            with open(target / f"{e.name}.png", "xb") as f:  # never overwrite existing assets
                f.write(Path(e.storage_path).read_bytes())
        criteria = fmt.bullet(design["acceptance_criteria"])
        prompt = (
            f"{task.description}\n\nAcceptance criteria:\n{criteria}\n\nDesign spec:\n{design['design_spec']}\n\n"
            f"Approved assets were placed in {asset_dir}/: "
            f"{', '.join(e.name + '.png' for e in entries) or 'none (generation unavailable)'}"
        )
        agents = ctx.agents(UNITY_SYSTEM)
        step, impl = await agents.implement(
            task, ws, prompt, "Implement", connector=implementer, depends_on=[d_step, a_step]
        )
        verdict = await agents.review_loop(
            task,
            ws,
            impl,
            step,
            f"Acceptance criteria:\n{criteria}\n\nAsset manifest problems: {problems or 'none'}",
        )
        if not verdict["ok"]:
            raise StepError(
                f"{verdict['blocking']} blocking findings, {len(verdict['qa_failed'])} failed QA checks",
                "review_blocked",
                data=verdict,
            )
        return StepOutput({**verdict, "assets": len(entries), "asset_problems": problems})

    async def _generate_assets(self, ctx, step, specs, atts, prompt_art):
        svc, task = ctx.svc, ctx.task
        entries: list[ManifestEntry] = []
        images: list[tuple[str, bytes]] = []
        ref_ids = [a.attachment_id for a, _ in atts if a.is_image]
        provider = svc.image_provider
        if specs and provider is None:
            await ctx.notify(
                "⚠️ No image-generation provider configured; assets were not generated."
            )
            return entries, images
        base = AssetRules(
            svc.s.asset_require_alpha, svc.s.asset_max_dimension, svc.s.asset_power_of_two
        )
        for spec in specs:
            name = "".join(ch for ch in spec["name"] if ch.isalnum() or ch in "_-")[:60] or "asset"
            data = await provider.generate(
                spec["prompt"], f"{spec['width']}x{spec['height']}", spec["transparent"]
            )
            art = svc.artifacts.add(
                step.task_id,
                "image",
                f"{name}.png",
                data,
                prompt=spec["prompt"],
                producer=f"{provider.name}:{provider.model}",
            )
            rules = AssetRules(
                require_alpha=spec["transparent"] and base.require_alpha,
                max_dimension=base.max_dimension,
                power_of_two=base.power_of_two,
                expected_width=spec["width"],
                expected_height=spec["height"],
            )
            entries.append(
                manifest_entry(
                    art.artifact_id,
                    task.task_id,
                    name,
                    data,
                    str(art.path),
                    rules,
                    ref_ids,
                    provider.name,
                    provider.model,
                    prompt_art.artifact_id,
                )
            )
            images.append((f"{name}.png", data))
        return entries, images


def svc_caps(atts) -> set:
    caps = {Cap.text}
    if any(a.is_image for a, _ in atts):
        caps.add(Cap.image_input)
    if any(not a.is_image for a, _ in atts):
        caps.add(Cap.file_input)
    return caps


def svc_state(name: str):
    from app.models import TaskState

    return TaskState(name)


def create_plugin():
    return UnityPlugin()
