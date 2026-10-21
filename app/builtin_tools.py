"""Built-in workspace, QA and artifact tools. All paths are confined to ctx.workspace."""

import dataclasses
import fnmatch
import hashlib
import json
from pathlib import Path

from app.assets import AssetRules, inspect_image, validate
from app.qa import QARunner
from app.tools import ToolContext, ToolDefinition, ToolError, ToolOutput, ToolRegistry
from app.workspace import git

MAX_READ_BYTES = 200_000
MAX_WRITE_BYTES = 200_000
MAX_LIST = 500
READ = frozenset({"workspace:read"})
WRITE = frozenset({"workspace:read", "workspace:write"})
EXEC = frozenset({"workspace:read", "process:execute"})


def resolve_in_workspace(ctx: ToolContext, rel: str) -> Path:
    """Canonicalize a user/model path and refuse anything outside the workspace."""
    if ctx.workspace is None:
        raise ToolError("no authorized workspace", "forbidden")
    if "\x00" in rel:
        raise ToolError("invalid path", "invalid_input")
    root = ctx.workspace.resolve()
    target = (root / rel).resolve()  # follows symlinks, so escapes via links are caught
    if not target.is_relative_to(root):
        raise ToolError(f"path escapes workspace: {rel}", "forbidden")
    if target.relative_to(root).parts[:1] == (".git",):
        raise ToolError("access to .git is not permitted", "forbidden")
    return target


def _obj(props: dict, required: list[str] = ()) -> dict:
    return {
        "type": "object",
        "properties": props,
        "required": list(required),
        "additionalProperties": False,
    }


async def list_files(ctx: ToolContext, args: dict) -> ToolOutput:
    base = resolve_in_workspace(ctx, args.get("path", "."))
    if not base.is_dir():
        raise ToolError(f"not a directory: {args.get('path', '.')}", "not_found")
    pattern = args.get("pattern", "*")
    root = ctx.workspace.resolve()
    files = []
    for p in sorted(base.rglob("*")):
        rel = p.relative_to(root)
        if ".git" in rel.parts or not p.is_file():
            continue
        if fnmatch.fnmatch(p.name, pattern):
            files.append(str(rel))
        if len(files) >= MAX_LIST:
            break
    return ToolOutput(
        "\n".join(files) or "(no files)", {"files": files, "truncated": len(files) >= MAX_LIST}
    )


async def read_file(ctx: ToolContext, args: dict) -> ToolOutput:
    p = resolve_in_workspace(ctx, args["path"])
    if not p.is_file():
        raise ToolError(f"not a file: {args['path']}", "not_found")
    size = p.stat().st_size
    if size > MAX_READ_BYTES:
        raise ToolError(f"file is {size} bytes; limit {MAX_READ_BYTES}", "too_large")
    try:
        text = p.read_text("utf-8")
    except UnicodeDecodeError as e:
        raise ToolError("not a UTF-8 text file", "unsupported") from e
    return ToolOutput(text, {"path": args["path"], "bytes": size})


async def create_file(ctx: ToolContext, args: dict) -> ToolOutput:
    data = args["content"].encode()
    if len(data) > MAX_WRITE_BYTES:
        raise ToolError("content too large", "too_large")
    p = resolve_in_workspace(ctx, args["path"])
    if p.exists():
        raise ToolError(f"{args['path']} exists; overwriting is not permitted", "conflict")
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "xb") as f:  # create-only, never overwrite
        f.write(data)
    digest = hashlib.sha256(data).hexdigest()
    return ToolOutput(
        f"created {args['path']} ({len(data)} bytes)",
        {"path": args["path"], "bytes": len(data), "sha256": digest},
    )


async def git_status(ctx: ToolContext, args: dict) -> ToolOutput:
    resolve_in_workspace(ctx, ".")
    out = await git(ctx.workspace, "status", "--porcelain=v1", "--untracked-files=all")
    changed = [line[3:] for line in out.splitlines() if line]
    return ToolOutput(out or "(clean)", {"changed": changed})


async def git_diff(ctx: ToolContext, args: dict) -> ToolOutput:
    resolve_in_workspace(ctx, ".")
    argv = ["diff"]
    if args.get("base"):
        base = args["base"]
        if base.startswith("-"):
            raise ToolError("invalid base ref", "invalid_input")
        argv.append(base)
    out = await git(ctx.workspace, *argv)
    return ToolOutput(out or "(no diff)", {"bytes": len(out)})


async def file_hash(ctx: ToolContext, args: dict) -> ToolOutput:
    p = resolve_in_workspace(ctx, args["path"])
    if not p.is_file():
        raise ToolError(f"not a file: {args['path']}", "not_found")
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return ToolOutput(
        h.hexdigest(), {"path": args["path"], "sha256": h.hexdigest(), "bytes": p.stat().st_size}
    )


async def image_validate(ctx: ToolContext, args: dict) -> ToolOutput:
    p = resolve_in_workspace(ctx, args["path"])
    if not p.is_file():
        raise ToolError(f"not a file: {args['path']}", "not_found")
    data = p.read_bytes()
    info = inspect_image(data)
    rules = AssetRules(
        require_alpha=args.get("require_alpha", False),
        expected_width=args.get("width"),
        expected_height=args.get("height"),
    )
    issues = validate(info, rules) if info.format != "unknown" else ["not a recognised image"]
    body = {
        **dataclasses.asdict(info),
        "issues": issues,
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    return ToolOutput(json.dumps(body), body)


def qa_tool(runner: QARunner) -> ToolDefinition:
    async def run(ctx: ToolContext, args: dict) -> ToolOutput:
        resolve_in_workspace(ctx, ".")
        r = await runner.run(args["check"], ctx.workspace, ctx.task_id)
        data = dataclasses.asdict(r)
        if ctx.artifacts is not None:
            art = ctx.artifacts.add(
                ctx.task_id,
                "log",
                f"qa_{r.name}.json",
                json.dumps(data, default=str, indent=2),
                step_id=ctx.step_id,
                producer=f"qa.run:{r.name}",
            )
            data["artifact_id"] = art.artifact_id
        text = (
            f"{r.name}: {r.status} (exit {r.exit_code}, {r.duration_s}s)\n{r.output_tail[-4000:]}"
        )
        if r.status == "skipped":
            raise ToolError(f"{r.name} skipped: {r.stderr}", "runtime_unavailable")
        out = ToolOutput(text, data, [data["artifact_id"]] if "artifact_id" in data else [])
        return out

    return ToolDefinition(
        name="qa.run",
        description="Run one pre-configured QA check (test/lint/build) in the task workspace.",
        input_schema=_obj(
            {"check": {"enum": sorted(runner.commands) or ["<none configured>"]}}, ["check"]
        ),
        handler=run,
        permissions=EXEC,
        risk="execute",
        timeout_s=runner.timeout_s + 10,
        side_effects=True,  # test commands may write caches/build outputs
    )


def register_builtin_tools(registry: ToolRegistry, qa: QARunner) -> None:
    path = {"type": "string", "minLength": 1, "maxLength": 500}
    for t in [
        ToolDefinition(
            "workspace.list_files",
            "List files in the task workspace (optionally filtered by glob).",
            _obj({"path": path, "pattern": {"type": "string", "maxLength": 100}}),
            list_files,
            READ,
        ),
        ToolDefinition(
            "workspace.read_file",
            "Read a UTF-8 text file (max 200 KB) from the task workspace.",
            _obj({"path": path}, ["path"]),
            read_file,
            READ,
        ),
        ToolDefinition(
            "workspace.create_file",
            "Create a new text file in the task workspace. Never overwrites.",
            _obj({"path": path, "content": {"type": "string"}}, ["path", "content"]),
            create_file,
            WRITE,
            risk="write",
            side_effects=True,
        ),
        ToolDefinition(
            "git.status",
            "Show git status (changed and untracked files) of the task workspace.",
            _obj({}),
            git_status,
            READ,
        ),
        ToolDefinition(
            "git.diff",
            "Show git diff of the task workspace, optionally against a base ref.",
            _obj({"base": {"type": "string", "maxLength": 80}}),
            git_diff,
            READ,
        ),
        ToolDefinition(
            "artifact.hash",
            "SHA-256 of a file in the task workspace.",
            _obj({"path": path}, ["path"]),
            file_hash,
            READ,
        ),
        ToolDefinition(
            "image.validate",
            "Inspect an image file: format, size, alpha, and optional expectations.",
            _obj(
                {
                    "path": path,
                    "require_alpha": {"type": "boolean"},
                    "width": {"type": "integer", "minimum": 1},
                    "height": {"type": "integer", "minimum": 1},
                },
                ["path"],
            ),
            image_validate,
            READ,
        ),
    ]:
        registry.register(t)
    if qa.commands:
        registry.register(qa_tool(qa))
