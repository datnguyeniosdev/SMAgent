"""Media & Image plugin: real image inspection/validation; generation only through a
configured provider (capability "image.generate"). Never fabricates output."""

import hashlib
import json
import re

from app.assets import AssetRules, inspect_image, validate
from app.connectors.base import ConnectorError
from app.engine import StepOutput
from app.intake import TaskRequest
from app.plugins.base import Classification, OperationSpec, PlanResult, PluginManifest
from app.storage import AttachmentError
from app.tools import ToolContext, ToolDefinition, ToolError, ToolOutput
from app.workflow import Criterion, GateResult, StepDef, WorkflowDef

IMAGE_MIME = {"image/png", "image/jpeg", "image/webp", "image/gif"}
KEYWORDS = {
    "image": 0.45,
    "images": 0.45,
    "picture": 0.4,
    "photo": 0.4,
    "icon": 0.35,
    "sprite": 0.25,
    "logo": 0.35,
    "generate": 0.15,
    "draw": 0.35,
    "render": 0.2,
    "dimensions": 0.3,
    "png": 0.35,
    "jpeg": 0.35,
    "illustration": 0.4,
}
GEN_SCHEMA = {
    "type": "object",
    "required": ["prompt"],
    "properties": {
        "prompt": {"type": "string", "minLength": 3, "maxLength": 4000},
        "width": {"type": "integer", "minimum": 16, "maximum": 3840},
        "height": {"type": "integer", "minimum": 16, "maximum": 3840},
        "transparent": {"type": "boolean"},
    },
}


def _info(data: bytes) -> dict:
    info = inspect_image(data)
    return {
        "format": info.format,
        "width": info.width,
        "height": info.height,
        "has_alpha": info.has_alpha,
        "color_mode": info.color_mode,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


async def tool_inspect(ctx: ToolContext, args: dict) -> ToolOutput:
    try:
        name, mime, data = ctx.read_input(args["ref"])
    except Exception as e:
        raise ToolError(str(e), "forbidden") from e
    info = _info(data)
    if info["format"] == "unknown":
        raise ToolError(f"{name}: not a readable PNG/JPEG/WebP/GIF image", "unsupported")
    return ToolOutput(
        f"{name}: {info['format']} {info['width']}x{info['height']}", {"source": name, **info}
    )


def _configured(services, attr: str):
    """Registration follows what is configured; each call uses the provider as switched now (/mcp, /keys)."""
    return getattr(services, f"_{attr}", getattr(services, attr, None))


def _current(services, attr: str, what: str):
    provider = getattr(services, attr, None)
    if provider is None or not getattr(provider, "available", True):
        raise ToolError(f"{what} is switched off (/mcp) or not configured (/keys)", "unavailable")
    return provider


GEN3D_SCHEMA = {
    "type": "object",
    "required": ["prompt"],
    "properties": {
        "prompt": {"type": "string", "minLength": 3, "maxLength": 800},
        "image_ref": {"type": "string", "pattern": "^((att|art)_[0-9a-f]+)?$"},  # "" = text only
        "texture": {"type": "boolean"},
        "format": {"type": "string", "enum": ["glb", "fbx", "obj", "usdz", "stl"]},
    },
}


def make_generate_3d(services):
    async def generate_3d(ctx: ToolContext, args: dict) -> ToolOutput:
        provider = _current(services, "model3d_provider", "3D generation")
        image, mime = None, "image/png"
        if args.get("image_ref"):
            try:
                _, mime, image = ctx.read_input(args["image_ref"])
            except Exception as e:
                raise ToolError(str(e), "forbidden") from e
        fmt = args.get("format", "glb")
        try:
            r = await provider.generate_3d(
                args["prompt"], image, mime, bool(args.get("texture", True)), fmt
            )
        except ConnectorError as e:
            raise ToolError(str(e), e.category) from e
        try:
            model = ctx.artifacts.add(ctx.task_id, "model3d", f"model.{fmt}", r["model"], step_id=ctx.step_id,
                                      producer=f"{provider.name}:{provider.model}", prompt=args["prompt"],
                                      meshy_task=r["task_id"])  # fmt: skip
        except AttachmentError as e:  # generated and paid for: say where it is
            raise ToolError(
                f"{e}; the model is in your Meshy account (task {r['task_id']})", "invalid_output"
            ) from e
        ids, out = [model.artifact_id], {"artifact_id": model.artifact_id, "format": fmt, "bytes": len(r["model"]),
                                          "provider": provider.name, "model": provider.model,
                                          "credits": r.get("credits")}  # fmt: skip
        if r.get("thumbnail") and _info(r["thumbnail"])["format"] != "unknown":
            thumb = ctx.artifacts.add(ctx.task_id, "image", "preview.png", r["thumbnail"], step_id=ctx.step_id,
                                      producer=f"{provider.name}:{provider.model}")  # fmt: skip
            ids.append(thumb.artifact_id)
            out["preview_artifact_id"] = thumb.artifact_id
        return ToolOutput(f"{fmt} model, {len(r['model'])} bytes", out, ids)

    return generate_3d


def make_generate(services):
    async def generate(ctx: ToolContext, args: dict) -> ToolOutput:
        provider = _current(services, "image_provider", "image generation")
        w, h = args.get("width", 1024), args.get("height", 1024)
        data = await provider.generate(args["prompt"], f"{w}x{h}", bool(args.get("transparent")))
        info = _info(data)
        if info["format"] == "unknown":  # never store something that is not an image
            raise ToolError(
                "provider returned bytes that are not a recognised image", "invalid_output"
            )
        art = ctx.artifacts.add(
            ctx.task_id,
            "image",
            "generated.png" if info["format"] == "png" else f"generated.{info['format']}",
            data,
            step_id=ctx.step_id,
            producer=f"{provider.name}:{provider.model}",
            prompt=args["prompt"],
            requested=f"{w}x{h}",
            width=info["width"],
            height=info["height"],
        )
        return ToolOutput(
            f"{info['format']} {info['width']}x{info['height']}",
            {
                **info,
                "artifact_id": art.artifact_id,
                "provider": provider.name,
                "model": provider.model,
                # False: the model only takes an aspect ratio, so the size check compares ratios
                "exact_size": getattr(provider, "exact_size", True),
            },
            [art.artifact_id],
        )

    return generate


class MediaPlugin:
    manifest = PluginManifest(
        id="media",
        name="Media & Images",
        version="1.0.0",
        description="Inspect and validate images; generate images through a configured provider.",
        categories=["media", "images"],
        keywords=list(KEYWORDS),
        permissions=["data:read", "artifact:write", "provider:image"],
        artifact_types=["image", "report"],
        operations=[
            OperationSpec(
                task_type="inspect",
                description="Read and validate supplied images (format, size, alpha, hash)",
                required_capabilities=["tool:media.inspect"],
            ),
            OperationSpec(
                task_type="generate",
                description="Generate an image with a configured provider and validate it",
                required_capabilities=["image.generate", "tool:media.generate"],
                side_effects=True,
                input_schema=GEN_SCHEMA,
                example={"data": {"prompt": "flat gold coin icon", "width": 1024, "height": 1024}},
            ),
        ],
    )

    def classify(self, req: TaskRequest, attachments: list) -> Classification | None:
        words = set(re.findall(r"[a-z]+", req.objective.lower()))
        hits = words & set(KEYWORDS)
        imgs = [a for a in attachments if a["mime_type"] in IMAGE_MIME]
        score = sum(KEYWORDS[w] for w in hits) + (0.3 if imgs else 0)
        if not score:
            return None
        gen = (
            bool(hits & {"generate", "draw", "illustration", "logo", "render"})
            or "prompt" in req.data
        )
        return Classification(
            "media",
            "generate" if gen and not imgs else "inspect",
            min(score, 1.0),
            [f"keywords: {sorted(hits)}", f"images: {len(imgs)}"],
        )

    def plan(self, req: TaskRequest, task_type: str, env) -> PlanResult:
        if task_type == "inspect":
            imgs = [a["id"] for a in env.attachments if a["mime_type"] in IMAGE_MIME]
            if not imgs:
                return PlanResult(question="Attach the image(s) to inspect.")
            steps = [
                StepDef(id=f"inspect_{i}", executor="tool", ref="media.inspect", inputs={"ref": r})
                for i, r in enumerate(imgs)
            ]
            steps.append(
                StepDef(
                    id="validate",
                    executor="validator",
                    ref="media.image_valid",
                    depends_on=[s.id for s in steps],
                    inputs={
                        "images": [f"$steps.{s.id}.output" for s in steps],
                        "rules": req.data.get("rules", {}),
                    },
                )
            )
            steps.append(
                StepDef(
                    id="report",
                    executor="plugin",
                    ref="media.report",
                    depends_on=["validate"],
                    inputs={
                        "images": [f"$steps.inspect_{i}.output" for i in range(len(imgs))],
                        "gate": "$steps.validate.output",
                    },
                )
            )
            return PlanResult(
                WorkflowDef(
                    id="media.inspect",
                    domain="media",
                    steps=steps,
                    success_criteria=[Criterion(kind="gate_passed", step="validate")],
                    outputs={"report": "$steps.report.output"},
                )
            )
        d = {
            "prompt": req.data.get("prompt", req.objective),
            "width": req.data.get("width", 1024),
            "height": req.data.get("height", 1024),
            "transparent": req.data.get("transparent", False),
        }
        steps = [
            StepDef(id="generate", executor="tool", ref="media.generate", inputs=d, timeout_s=300),
            StepDef(
                id="validate",
                executor="validator",
                ref="media.image_valid",
                depends_on=["generate"],
                inputs={
                    "images": ["$steps.generate.output"],
                    "rules": {
                        "width": d["width"],
                        "height": d["height"],
                        "require_alpha": d["transparent"],
                    },
                },
            ),
        ]
        return PlanResult(
            WorkflowDef(
                id="media.generate",
                domain="media",
                steps=steps,
                success_criteria=[
                    Criterion(kind="artifact_exists", step="generate"),
                    Criterion(kind="gate_passed", step="validate"),
                ],
                outputs={"image": "$steps.generate.output"},
            )
        )

    def tools(self, services) -> list[ToolDefinition]:
        ref = {"type": "string", "pattern": "^(att|art)_[0-9a-f]+$"}
        out = [
            ToolDefinition(
                "media.inspect",
                "Read an image input: format, dimensions, alpha, size, SHA-256.",
                {
                    "type": "object",
                    "properties": {"ref": ref},
                    "required": ["ref"],
                    "additionalProperties": False,
                },
                tool_inspect,
                frozenset({"data:read"}),
                environment="artifact",
                timeout_s=30,
                source="plugin:media",
            )
        ]
        provider = _configured(services, "image_provider")
        if provider is not None:  # only registered when a real provider is configured
            out.append(
                ToolDefinition(
                    "media.generate",
                    f"Generate one image with {provider.name} ({provider.model}).",
                    {**GEN_SCHEMA, "additionalProperties": False},
                    make_generate(services),
                    frozenset({"provider:image", "artifact:write"}),
                    risk="external",
                    environment=f"provider:{provider.name}",
                    timeout_s=300,
                    requires_approval=True,
                    side_effects=True,
                    source="plugin:media",
                )
            )
        m3d = _configured(services, "model3d_provider")
        if m3d is not None:
            out.append(
                ToolDefinition(
                    "media.generate_3d",
                    f"Generate one 3D model with {m3d.name} (text or reference image -> glb/fbx/obj).",
                    {**GEN3D_SCHEMA, "additionalProperties": False},
                    make_generate_3d(services),
                    frozenset({"provider:image", "artifact:write"}),
                    risk="external",
                    environment=f"provider:{m3d.name}",
                    timeout_s=1500,
                    requires_approval=True,
                    side_effects=True,
                    source="plugin:media",
                )
            )
        return out

    def idempotent_operations(self) -> set[str]:
        return {"report"}

    def operations(self) -> dict:
        return {"report": self.op_report}

    def validators(self) -> dict:
        return {"image_valid": self.gate_image_valid}

    async def op_report(self, ctx, inputs: dict) -> StepOutput:
        imgs = [i for i in inputs.get("images") or [] if isinstance(i, dict)]
        art = ctx.add_artifact(
            "report",
            "media_report.json",
            json.dumps({"images": imgs, "validation": inputs.get("gate")}, indent=2, default=str),
        )
        return StepOutput({"images": len(imgs)}, [art.artifact_id])

    async def gate_image_valid(self, ctx, inputs: dict) -> GateResult:
        imgs = [i for i in inputs.get("images") or [] if isinstance(i, dict)]
        r = inputs.get("rules") or {}
        rules = AssetRules(
            require_alpha=bool(r.get("require_alpha")),
            max_dimension=r.get("max_dimension", 8192),
            expected_width=r.get("width"),
            expected_height=r.get("height"),
        )
        findings = []
        for img in imgs:
            img_rules = rules
            if img.get("exact_size") is False and rules.expected_width and rules.expected_height:
                img_rules = AssetRules(
                    require_alpha=rules.require_alpha, max_dimension=rules.max_dimension
                )
                want = rules.expected_width / rules.expected_height
                got = img["width"] / img["height"] if img.get("height") else 0
                if not got or abs(got - want) / want > 0.12:
                    # the model offers a few ratios only: a paid image is not thrown away for it, the
                    # operator sees the note in review and the agent crops or scales on import
                    findings.append({"severity": "medium", "message": (
                        f"{img.get('artifact_id')}: aspect {img.get('width')}x{img.get('height')} differs from "
                        f"the requested {rules.expected_width}x{rules.expected_height} (the model's nearest "
                        "ratio); crop or scale on import")})  # fmt: skip
            info = (
                inspect_image(ctx.read_artifact(img["artifact_id"]))
                if img.get("artifact_id")
                else None
            )
            if info is None:  # supplied input: re-check from recorded metadata
                from app.assets import ImageInfo

                info = ImageInfo(
                    img["format"], img["width"], img["height"], img["has_alpha"], img["color_mode"]
                )
            issues = [
                i
                for i in validate(info, img_rules)
                if not (
                    i.startswith("format is")
                    and info.format != "unknown"
                    and not r.get("require_png")
                )
            ]
            findings += [
                {
                    "severity": "medium" if "alpha" in i else "high",
                    "message": f"{img.get('source', img.get('artifact_id'))}: {i}",
                }
                for i in issues
            ]
        if not imgs:
            return GateResult(
                validator_id="media.image_valid",
                status="skipped",
                findings=[{"severity": "high", "message": "no images"}],
            )
        return GateResult(
            validator_id="media.image_valid",
            status="failed" if any(f["severity"] == "high" for f in findings) else "passed",
            evidence=[i["artifact_id"] for i in imgs if i.get("artifact_id")],
            findings=findings,
            metrics={"images": len(imgs)},
        )


def create_plugin():
    return MediaPlugin()
