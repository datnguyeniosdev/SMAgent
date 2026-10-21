"""Data Processing plugin: deterministic CSV/JSON profiling, validation, transformation,
aggregation, joins and conversion. No user code, SQL or formulas are ever executed.
Every output is a new artifact with metadata sufficient to reproduce the operation.
"""

import csv
import io
import json
import math
import re
import statistics
from collections import Counter, defaultdict

from app.engine import StepOutput
from app.intake import TaskRequest
from app.plugins.base import Classification, OperationSpec, PlanResult, PluginManifest
from app.tools import ToolContext, ToolDefinition, ToolError, ToolOutput
from app.workflow import Criterion, GateResult, StepDef, WorkflowDef

MAX_ROWS = 200_000
MAX_COLS = 500
TABULAR = {"text/csv", "application/json"}
KEYWORDS = {
    "csv": 0.5,
    "json": 0.3,
    "data": 0.3,
    "dataset": 0.45,
    "rows": 0.4,
    "columns": 0.4,
    "filter": 0.35,
    "aggregate": 0.45,
    "group": 0.3,
    "join": 0.4,
    "convert": 0.3,
    "transform": 0.4,
    "schema": 0.35,
    "profile": 0.35,
    "table": 0.25,
    "sum": 0.2,
    "average": 0.25,
    "duplicates": 0.35,
}
TYPES = ("int", "float", "bool", "str")

# ---------- parsing ----------


def parse_value(v):
    if v is None:
        return None
    if not isinstance(v, str):
        return v
    s = v.strip()
    if s == "" or s.lower() in ("null", "none", "na", "n/a"):
        return None
    if re.fullmatch(r"[+-]?\d+", s):
        return int(s)
    if re.fullmatch(r"[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?", s):
        f = float(s)
        return f if math.isfinite(f) else s
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    return s


def type_of(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    return "str"


def load_rows(name: str, mime: str, data: bytes) -> tuple[list[dict], list[str], list[str]]:
    """Returns rows (typed), column order, warnings."""
    warnings = []
    text = data.decode("utf-8-sig")
    if mime == "text/csv" or name.lower().endswith(".csv"):
        reader = csv.DictReader(io.StringIO(text))
        cols = reader.fieldnames or []
        if len(cols) > MAX_COLS:
            raise ToolError(f"{len(cols)} columns exceeds {MAX_COLS}", "too_large")
        if len(set(cols)) != len(cols):
            raise ToolError("duplicate column names", "invalid_input")
        rows = []
        for i, r in enumerate(reader):
            if i >= MAX_ROWS:
                raise ToolError(f"more than {MAX_ROWS} rows", "too_large")
            if None in r:  # more fields than header
                warnings.append(f"row {i + 2}: extra fields dropped")
                r.pop(None)
            rows.append({k: parse_value(v) for k, v in r.items()})
        return rows, list(cols), warnings
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise ToolError(f"invalid JSON: {e}", "invalid_input") from e
    if isinstance(obj, dict) and isinstance(obj.get("rows"), list):
        obj = obj["rows"]
    if not isinstance(obj, list) or not all(isinstance(r, dict) for r in obj):
        raise ToolError("JSON must be a list of objects (or {'rows': [...]})", "invalid_input")
    if len(obj) > MAX_ROWS:
        raise ToolError(f"more than {MAX_ROWS} rows", "too_large")
    cols: list[str] = []
    for r in obj:
        for k in r:
            if k not in cols:
                cols.append(k)
    for r in obj:
        for k, v in r.items():
            if isinstance(v, (dict, list)):
                raise ToolError(f"nested value in column {k!r} is not supported", "unsupported")
    return [{c: r.get(c) for c in cols} for r in obj], cols, warnings


def dump_rows(rows: list[dict], cols: list[str], fmt: str) -> bytes:
    if fmt == "json":
        return json.dumps(rows, indent=2, default=str).encode()
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({c: ("" if r.get(c) is None else r.get(c)) for c in cols})
    return buf.getvalue().encode()


def profile(rows: list[dict], cols: list[str]) -> dict:
    out = {}
    for c in cols:
        vals = [r.get(c) for r in rows]
        types = Counter(t for v in vals if (t := type_of(v)))
        nums = [v for v in vals if type_of(v) in ("int", "float")]
        col = {
            "types": dict(types),
            "nulls": sum(v is None for v in vals),
            "distinct": len({json.dumps(v, default=str) for v in vals}),
        }
        if len(types) > 1:
            col["mixed_types"] = True
        if nums:
            col.update(min=min(nums), max=max(nums), mean=round(statistics.fmean(nums), 6))
        out[c] = col
    dup = len(rows) - len({json.dumps(r, sort_keys=True, default=str) for r in rows})
    return {"rows": len(rows), "columns": out, "duplicate_rows": dup}


# ---------- tools ----------


def _input(ctx: ToolContext, ref: str):
    if ctx.read_input is None:
        raise ToolError("no input access", "forbidden")
    try:
        name, mime, data = ctx.read_input(ref)
    except Exception as e:
        raise ToolError(str(e), "forbidden") from e
    if mime not in TABULAR and not name.lower().endswith((".csv", ".json")):
        raise ToolError(f"{name}: {mime} is not CSV/JSON", "unsupported")
    rows, cols, warnings = load_rows(name, mime, data)
    return name, rows, cols, warnings


def _save(ctx: ToolContext, rows, cols, fmt: str, stem: str, meta: dict):
    data = dump_rows(rows, cols, fmt)
    art = ctx.artifacts.add(
        ctx.task_id,
        "data",
        f"{stem}.{fmt}",
        data,
        step_id=ctx.step_id,
        producer=meta.get("operation"),
        **{"data_meta": meta},
    )
    return art


async def tool_profile(ctx: ToolContext, args: dict) -> ToolOutput:
    name, rows, cols, warnings = _input(ctx, args["ref"])
    p = {"source": name, "ref": args["ref"], **profile(rows, cols), "warnings": warnings}
    return ToolOutput(f"{name}: {p['rows']} rows x {len(cols)} columns", p)


def check_schema(rows, cols, schema: dict) -> list[dict]:
    issues = []
    want = schema.get("columns", {})
    for c, t in want.items():
        if c not in cols:
            issues.append({"severity": "high", "message": f"missing column {c!r}"})
            continue
        bad = [i for i, r in enumerate(rows) if r.get(c) is not None and not _fits(r[c], t)]
        if bad:
            issues.append(
                {
                    "severity": "high",
                    "message": f"{c}: {len(bad)} values not {t} (first row {bad[0] + 1})",
                }
            )
    for c in schema.get("required", []):
        n = sum(r.get(c) is None for r in rows)
        if n:
            issues.append({"severity": "high", "message": f"{c}: {n} missing values"})
    for c in schema.get("unique", []):
        vals = [r.get(c) for r in rows]
        if len(vals) != len(set(map(str, vals))):
            issues.append({"severity": "high", "message": f"{c}: duplicate values"})
    return issues


def _fits(v, t: str) -> bool:
    return {
        "int": type_of(v) == "int",
        "float": type_of(v) in ("int", "float"),
        "bool": type_of(v) == "bool",
        "str": True,
    }.get(t, False)


async def tool_validate(ctx: ToolContext, args: dict) -> ToolOutput:
    name, rows, cols, warnings = _input(ctx, args["ref"])
    issues = check_schema(rows, cols, args["schema"])
    data = {
        "source": name,
        "rows": len(rows),
        "valid": not issues,
        "issues": issues,
        "warnings": warnings,
    }
    return ToolOutput(f"{name}: {'valid' if not issues else f'{len(issues)} issues'}", data)


def _cmp(v, op, x):
    if op == "equals":
        return v == x
    if op == "not_equals":
        return v != x
    if op == "in":
        return v in x
    if v is None or type_of(v) not in ("int", "float") or type_of(x) not in ("int", "float"):
        return False
    return v > x if op == "gt" else v < x if op == "lt" else v >= x if op == "gte" else v <= x


def apply_ops(
    rows: list[dict], cols: list[str], ops: list[dict]
) -> tuple[list[dict], list[str], list[dict]]:
    log = []
    for op in ops:
        kind, before = op["op"], len(rows)
        if kind == "select":
            missing = [c for c in op["columns"] if c not in cols]
            if missing:
                raise ToolError(f"select: unknown columns {missing}", "invalid_input")
            cols = list(op["columns"])
            rows = [{c: r[c] for c in cols} for r in rows]
        elif kind == "rename":
            cols = [op["mapping"].get(c, c) for c in cols]
            rows = [{op["mapping"].get(k, k): v for k, v in r.items()} for r in rows]
        elif kind == "filter":
            c = op["column"]
            if c not in cols:
                raise ToolError(f"filter: unknown column {c!r}", "invalid_input")
            cond = next(
                k for k in ("equals", "not_equals", "gt", "lt", "gte", "lte", "in") if k in op
            )
            rows = [r for r in rows if _cmp(r.get(c), cond, op[cond])]
        elif kind == "sort":
            c = op["by"]
            if c not in cols:
                raise ToolError(f"sort: unknown column {c!r}", "invalid_input")
            rows = sorted(
                rows,
                key=lambda r: (
                    r.get(c) is None,
                    str(type_of(r.get(c))),
                    r.get(c) if r.get(c) is not None else 0,
                ),
                reverse=bool(op.get("desc")),
            )
        elif kind == "dedupe":
            keys = op.get("columns") or cols
            seen, out = set(), []
            for r in rows:
                k = json.dumps([r.get(c) for c in keys], default=str)
                if k not in seen:
                    seen.add(k)
                    out.append(r)
            rows = out
        elif kind == "cast":
            c, t = op["column"], op["type"]
            failed = 0
            for r in rows:
                v = r.get(c)
                try:
                    r[c] = (
                        None
                        if v is None
                        else {
                            "int": int,
                            "float": float,
                            "str": str,
                            "bool": lambda x: str(x).lower() == "true",
                        }[t](v)
                    )
                except (ValueError, TypeError):
                    r[c], failed = None, failed + 1
            op = {**op, "failed_conversions": failed}
        elif kind == "limit":
            rows = rows[: op["n"]]
        else:
            raise ToolError(f"unsupported operation {kind!r}", "unsupported")
        log.append(
            {**op, "rows_before": before, "rows_after": len(rows), "discarded": before - len(rows)}
        )
    return rows, cols, log


async def tool_transform(ctx: ToolContext, args: dict) -> ToolOutput:
    name, rows, cols, warnings = _input(ctx, args["ref"])
    rows_in = len(rows)
    out_rows, out_cols, log = apply_ops([dict(r) for r in rows], cols, args["operations"])
    fmt = args.get("output_format", "csv")
    meta = {
        "operation": "data.transform",
        "source": name,
        "source_ref": args["ref"],
        "operations": log,
        "rows_in": rows_in,
        "rows_out": len(out_rows),
        "columns_out": out_cols,
        "format": fmt,
        "warnings": warnings,
    }
    art = _save(ctx, out_rows, out_cols, fmt, "transformed", meta)
    return ToolOutput(
        f"{rows_in} -> {len(out_rows)} rows",
        {**meta, "artifact_id": art.artifact_id},
        [art.artifact_id],
    )


AGG = {
    "sum": sum,
    "count": len,
    "min": min,
    "max": max,
    "mean": lambda v: round(statistics.fmean(v), 6),
}


async def tool_aggregate(ctx: ToolContext, args: dict) -> ToolOutput:
    name, rows, cols, warnings = _input(ctx, args["ref"])
    group_by, metrics = args.get("group_by", []), args["metrics"]
    for c in [*group_by, *metrics]:
        if c not in cols:
            raise ToolError(f"unknown column {c!r}", "invalid_input")
    groups: dict = defaultdict(list)
    for r in rows:
        groups[tuple(r.get(c) for c in group_by)].append(r)
    out, skipped = [], Counter()
    for key, members in groups.items():
        row = dict(zip(group_by, key, strict=True))
        for col, fns in metrics.items():
            vals = [m[col] for m in members if type_of(m.get(col)) in ("int", "float")]
            skipped[col] += len(members) - len(vals)
            for fn in fns:
                row[f"{col}_{fn}"] = (
                    len(members) if fn == "count" else (AGG[fn](vals) if vals else None)
                )
        out.append(row)
    out_cols = [*group_by, *[f"{c}_{fn}" for c, fns in metrics.items() for fn in fns]]
    fmt = args.get("output_format", "csv")
    meta = {
        "operation": "data.aggregate",
        "source": name,
        "source_ref": args["ref"],
        "group_by": group_by,
        "metrics": metrics,
        "rows_in": len(rows),
        "rows_out": len(out),
        "columns_out": out_cols,
        "non_numeric_skipped": dict(skipped),
        "format": fmt,
    }
    art = _save(ctx, out, out_cols, fmt, "aggregated", meta)
    return ToolOutput(
        f"{len(groups)} groups", {**meta, "artifact_id": art.artifact_id}, [art.artifact_id]
    )


async def tool_join(ctx: ToolContext, args: dict) -> ToolOutput:
    ln, left, lc, _ = _input(ctx, args["left"])
    rn, right, rc, _ = _input(ctx, args["right"])
    on, how = args["on"], args.get("how", "inner")
    if on not in lc or on not in rc:
        raise ToolError(f"join column {on!r} missing on one side", "invalid_input")
    index = defaultdict(list)
    for r in right:
        index[r.get(on)].append(r)
    extra = [c for c in rc if c != on]
    clash = [c for c in extra if c in lc]
    out_cols = lc + [f"{c}_right" if c in clash else c for c in extra]
    out, unmatched = [], 0
    for r in left:
        matches = index.get(r.get(on), [])
        if not matches:
            unmatched += 1
            if how == "left":
                out.append({**r, **{f"{c}_right" if c in clash else c: None for c in extra}})
        for m in matches:
            out.append({**r, **{f"{c}_right" if c in clash else c: m.get(c) for c in extra}})
    fmt = args.get("output_format", "csv")
    meta = {
        "operation": "data.join",
        "left": ln,
        "right": rn,
        "on": on,
        "how": how,
        "rows_left": len(left),
        "rows_right": len(right),
        "rows_out": len(out),
        "unmatched_left": unmatched,
        "renamed_columns": clash,
        "columns_out": out_cols,
        "format": fmt,
    }
    art = _save(ctx, out, out_cols, fmt, "joined", meta)
    return ToolOutput(
        f"{len(out)} joined rows", {**meta, "artifact_id": art.artifact_id}, [art.artifact_id]
    )


async def tool_convert(ctx: ToolContext, args: dict) -> ToolOutput:
    name, rows, cols, warnings = _input(ctx, args["ref"])
    meta = {
        "operation": "data.convert",
        "source": name,
        "source_ref": args["ref"],
        "rows_in": len(rows),
        "rows_out": len(rows),
        "columns_out": cols,
        "format": args["to"],
        "warnings": warnings,
    }
    art = _save(ctx, rows, cols, args["to"], name.rsplit(".", 1)[0] or "converted", meta)
    return ToolOutput(
        f"converted to {args['to']}", {**meta, "artifact_id": art.artifact_id}, [art.artifact_id]
    )


# ---------- plugin ----------

REF = {"type": "string", "pattern": "^(att|art)_[0-9a-f]+$"}
FMT = {"enum": ["csv", "json"]}
SCHEMA = {
    "type": "object",
    "properties": {
        "columns": {"type": "object", "additionalProperties": {"enum": list(TYPES)}},
        "required": {"type": "array", "items": {"type": "string"}},
        "unique": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}
OPS = {
    "type": "array",
    "minItems": 1,
    "maxItems": 30,
    "items": {
        "type": "object",
        "required": ["op"],
        "properties": {
            "op": {"enum": ["select", "rename", "filter", "sort", "dedupe", "cast", "limit"]}
        },
    },
}


def _obj(props, req):
    return {"type": "object", "properties": props, "required": req, "additionalProperties": False}


class DataPlugin:
    manifest = PluginManifest(
        id="data",
        name="Data Processing",
        version="1.0.0",
        description="Validated CSV/JSON profiling, schema checks, transforms, aggregation, joins and conversion.",
        categories=["data", "csv", "json"],
        keywords=list(KEYWORDS),
        permissions=["data:read", "artifact:write"],
        artifact_types=["data", "report"],
        operations=[
            OperationSpec(
                task_type="profile",
                description="Profile columns, types, nulls and duplicates",
                required_capabilities=["tool:data.profile"],
            ),
            OperationSpec(
                task_type="transform",
                description="Validate -> transform -> check invariants -> export -> summary",
                required_capabilities=["tool:data.transform", "tool:data.profile"],
                input_schema={
                    "type": "object",
                    "properties": {"operations": OPS, "schema": SCHEMA, "output_format": FMT},
                    "required": ["operations"],
                },
                example={
                    "data": {
                        "operations": [{"op": "filter", "column": "region", "equals": "EU"}],
                        "output_format": "csv",
                    }
                },
            ),
            OperationSpec(
                task_type="aggregate",
                description="Group-by aggregation",
                required_capabilities=["tool:data.aggregate"],
                input_schema={
                    "type": "object",
                    "required": ["metrics"],
                    "properties": {
                        "group_by": {"type": "array", "items": {"type": "string"}},
                        "metrics": {"type": "object", "minProperties": 1},
                        "output_format": FMT,
                    },
                },
            ),
            OperationSpec(
                task_type="join",
                description="Join two supplied datasets on a column",
                required_capabilities=["tool:data.join"],
                input_schema={
                    "type": "object",
                    "required": ["on"],
                    "properties": {
                        "on": {"type": "string"},
                        "how": {"enum": ["inner", "left"]},
                        "output_format": FMT,
                    },
                },
            ),
            OperationSpec(
                task_type="convert",
                description="Convert CSV <-> JSON",
                required_capabilities=["tool:data.convert"],
                input_schema={"type": "object", "required": ["to"], "properties": {"to": FMT}},
            ),
        ],
    )

    def classify(self, req: TaskRequest, attachments: list) -> Classification | None:
        words = set(re.findall(r"[a-z]+", req.objective.lower()))
        hits = words & set(KEYWORDS)
        tab = [a for a in attachments if a["mime_type"] in TABULAR]
        score = sum(KEYWORDS[w] for w in hits) + (0.4 if tab else 0)
        if not score:
            return None
        if "operations" in req.data:
            ttype, score = "transform", score + 0.4
        elif "metrics" in req.data or hits & {"aggregate", "sum", "average", "group"}:
            ttype = "aggregate"
        elif "on" in req.data or "join" in hits:
            ttype = "join"
        elif "to" in req.data or "convert" in hits:
            ttype = "convert"
        elif hits & {"filter", "transform"}:
            ttype = "transform"
        else:
            ttype = "profile"
        return Classification(
            "data",
            ttype,
            min(score, 1.0),
            [f"keywords: {sorted(hits)}", f"tabular inputs: {len(tab)}"],
        )

    def plan(self, req: TaskRequest, task_type: str, env) -> PlanResult:
        refs = [
            a["id"]
            for a in env.attachments
            if a["mime_type"] in TABULAR or a["filename"].lower().endswith((".csv", ".json"))
        ]
        if not refs:
            return PlanResult(question="Attach the CSV or JSON dataset to process.")
        d = req.data
        fmt = d.get("output_format", "csv")
        if task_type == "profile":
            steps = [
                StepDef(id=f"profile_{i}", executor="tool", ref="data.profile", inputs={"ref": r})
                for i, r in enumerate(refs)
            ]
            steps.append(
                StepDef(
                    id="summary",
                    executor="plugin",
                    ref="data.summary",
                    depends_on=[s.id for s in steps],
                    inputs={"profiles": [f"$steps.{s.id}.output" for s in steps]},
                )
            )
            return PlanResult(
                WorkflowDef(
                    id="data.profile",
                    domain="data",
                    steps=steps,
                    outputs={"profiles": "$steps.summary.output"},
                )
            )
        if task_type == "join":
            if len(refs) != 2:
                return PlanResult(question=f"Join needs exactly two datasets; got {len(refs)}.")
            produce = StepDef(
                id="produce",
                executor="tool",
                ref="data.join",
                inputs={
                    "left": refs[0],
                    "right": refs[1],
                    "on": d["on"],
                    "how": d.get("how", "inner"),
                    "output_format": fmt,
                },
            )
            pre = []
        else:
            src = refs[0]
            pre = [
                StepDef(id="profile_in", executor="tool", ref="data.profile", inputs={"ref": src})
            ]
            if "schema" in d:
                pre.append(
                    StepDef(
                        id="schema_in",
                        executor="tool",
                        ref="data.validate",
                        inputs={"ref": src, "schema": d["schema"]},
                    )
                )
                pre.append(
                    StepDef(
                        id="schema_gate",
                        executor="internal",
                        ref="assert",
                        depends_on=["schema_in"],
                        inputs={"value": "$steps.schema_in.output.valid", "equals": True},
                    )
                )
            tool, inputs = {
                "transform": (
                    "data.transform",
                    {"ref": src, "operations": d.get("operations", []), "output_format": fmt},
                ),
                "aggregate": (
                    "data.aggregate",
                    {
                        "ref": src,
                        "group_by": d.get("group_by", []),
                        "metrics": d.get("metrics", {}),
                        "output_format": fmt,
                    },
                ),
                "convert": ("data.convert", {"ref": src, "to": d.get("to", "json")}),
            }[task_type]
            if task_type == "transform" and not d.get("operations"):
                return PlanResult(
                    question="Which transformation? Send `operations`, e.g. "
                    '[{"op":"filter","column":"region","equals":"EU"}].'
                )
            produce = StepDef(
                id="produce",
                executor="tool",
                ref=tool,
                inputs=inputs,
                depends_on=[s.id for s in pre if s.id != "schema_in"],
            )
        steps = [
            *pre,
            produce,
            StepDef(
                id="check",
                executor="validator",
                ref="data.output_valid",
                depends_on=["produce"],
                inputs={
                    "artifact_ids": "$steps.produce.artifacts",
                    "meta": "$steps.produce.output",
                    "schema": d.get("output_schema", {}),
                },
            ),
            StepDef(
                id="profile_out",
                executor="tool",
                ref="data.profile",
                depends_on=["check"],
                inputs={"ref": "$steps.produce.output.artifact_id"},
            ),
            StepDef(
                id="summary",
                executor="plugin",
                ref="data.summary",
                depends_on=["profile_out"],
                inputs={"profiles": ["$steps.profile_out.output"], "meta": "$steps.produce.output"},
            ),
        ]
        return PlanResult(
            WorkflowDef(
                id=f"data.{task_type}",
                domain="data",
                steps=steps,
                success_criteria=[
                    Criterion(kind="gate_passed", step="check"),
                    Criterion(kind="artifact_exists", step="produce"),
                ],
                outputs={
                    "output_artifact": "$steps.produce.output.artifact_id",
                    "summary": "$steps.summary.output",
                },
            )
        )

    def tools(self, services) -> list[ToolDefinition]:
        p = frozenset({"data:read", "artifact:write"})
        r = frozenset({"data:read"})

        def t(name, desc, schema, fn, perms):
            return ToolDefinition(
                name,
                desc,
                schema,
                fn,
                perms,
                environment="artifact",
                timeout_s=60,
                source="plugin:data",
            )

        return [
            t(
                "data.profile",
                "Profile a CSV/JSON input: rows, column types, nulls, duplicates.",
                _obj({"ref": REF}, ["ref"]),
                tool_profile,
                r,
            ),
            t(
                "data.validate",
                "Validate a CSV/JSON input against column types, required and unique columns.",
                _obj({"ref": REF, "schema": SCHEMA}, ["ref", "schema"]),
                tool_validate,
                r,
            ),
            t(
                "data.transform",
                "Apply select/rename/filter/sort/dedupe/cast/limit to a dataset; writes a new artifact.",
                _obj({"ref": REF, "operations": OPS, "output_format": FMT}, ["ref", "operations"]),
                tool_transform,
                p,
            ),
            t(
                "data.aggregate",
                "Group-by aggregation (sum/count/mean/min/max); writes a new artifact.",
                _obj(
                    {
                        "ref": REF,
                        "group_by": {"type": "array", "items": {"type": "string"}},
                        "metrics": {
                            "type": "object",
                            "additionalProperties": {"type": "array", "items": {"enum": list(AGG)}},
                        },
                        "output_format": FMT,
                    },
                    ["ref", "metrics"],
                ),
                tool_aggregate,
                p,
            ),
            t(
                "data.join",
                "Inner/left join of two datasets on one column; writes a new artifact.",
                _obj(
                    {
                        "left": REF,
                        "right": REF,
                        "on": {"type": "string"},
                        "how": {"enum": ["inner", "left"]},
                        "output_format": FMT,
                    },
                    ["left", "right", "on"],
                ),
                tool_join,
                p,
            ),
            t(
                "data.convert",
                "Convert a dataset between CSV and JSON; writes a new artifact.",
                _obj({"ref": REF, "to": FMT}, ["ref", "to"]),
                tool_convert,
                p,
            ),
        ]

    def idempotent_operations(self) -> set[str]:
        return {"summary"}

    def operations(self) -> dict:
        return {"summary": self.op_summary}

    def validators(self) -> dict:
        return {"output_valid": self.gate_output_valid}

    async def op_summary(self, ctx, inputs: dict) -> StepOutput:
        profiles = [p for p in inputs.get("profiles") or [] if isinstance(p, dict)]
        meta = inputs.get("meta") or {}
        md = ["# Data summary", ""]
        if meta:
            md += [
                "## Operation",
                "```json",
                json.dumps(
                    {k: v for k, v in meta.items() if k != "_record_id"}, indent=2, default=str
                ),
                "```",
                "",
            ]
        for p in profiles:
            md += [
                f"## {p.get('source')}",
                f"Rows: {p['rows']} · duplicate rows: {p['duplicate_rows']}",
                "",
                "| column | types | nulls | distinct | min | max | mean |",
                "|---|---|---|---|---|---|---|",
            ]
            for c, s in p["columns"].items():
                md.append(
                    f"| {c} | {s['types']} | {s['nulls']} | {s['distinct']} | {s.get('min', '')} | "
                    f"{s.get('max', '')} | {s.get('mean', '')} |"
                )
            md.append("")
        art = ctx.add_artifact("report", "data_summary.md", "\n".join(md))
        return StepOutput(
            {"datasets": len(profiles), "rows": [p["rows"] for p in profiles]}, [art.artifact_id]
        )

    async def gate_output_valid(self, ctx, inputs: dict) -> GateResult:
        ids, meta = inputs.get("artifact_ids") or [], inputs.get("meta") or {}
        if not ids:
            return GateResult(
                validator_id="data.output_valid",
                status="failed",
                findings=[{"severity": "high", "message": "no output artifact"}],
            )
        art = ctx.artifact(ids[0])
        rows, cols, _ = load_rows(art.filename, art.mime_type or "", ctx.read_artifact(ids[0]))
        findings = []
        if meta.get("rows_out") is not None and len(rows) != meta["rows_out"]:
            findings.append(
                {
                    "severity": "high",
                    "message": f"row count {len(rows)} != recorded {meta['rows_out']}",
                }
            )
        if meta.get("columns_out") and cols != meta["columns_out"]:
            findings.append(
                {"severity": "high", "message": f"columns {cols} != recorded {meta['columns_out']}"}
            )
        if (
            meta.get("rows_in") is not None
            and meta.get("operation") == "data.transform"
            and len(rows) > meta["rows_in"]
        ):
            findings.append(
                {"severity": "high", "message": "transform produced more rows than it read"}
            )
        findings += check_schema(rows, cols, inputs.get("schema") or {})
        return GateResult(
            validator_id="data.output_valid",
            status="failed" if findings else "passed",
            evidence=ids,
            findings=findings,
            metrics={"rows": len(rows), "columns": len(cols)},
        )


def create_plugin():
    return DataPlugin()
