"""Research & Documents plugin.

Real, deterministic processing of supplied UTF-8 text/Markdown documents: structure and
fact extraction with provenance, comparison, and source-attributed Markdown reports.
Model summaries and web research are declared but need a verified provider; without one
they are refused explicitly (no invented citations, no pretend searches).
"""

import difflib
import json
import re
from urllib.parse import urlparse

from app.engine import StepError, StepOutput
from app.intake import TaskRequest
from app.plugins.base import Classification, OperationSpec, PlanResult, PluginManifest
from app.tools import ToolContext, ToolDefinition, ToolError, ToolOutput
from app.workflow import Criterion, GateResult, StepDef, WorkflowDef

TEXT_MIME = {"text/plain", "text/markdown"}
MAX_CHARS = 500_000
_URL = re.compile(r"https?://[^\s)>\]\"']+")
_KV = re.compile(r"^\s*[-*]?\s*([A-Z][A-Za-z0-9 /_-]{1,40}):\s+(\S.{0,300})$")
_NUM_SENT = re.compile(
    r"[^.!?\n]*\b\d[\d,.]*\s*(%|percent|million|billion|thousand|users|ms|s|kg|km|USD|\$)?[^.!?\n]*[.!?]"
)
KEYWORDS = {
    "document": 0.35,
    "documents": 0.4,
    "report": 0.3,
    "summarize": 0.4,
    "summary": 0.35,
    "extract": 0.35,
    "compare": 0.35,
    "outline": 0.4,
    "research": 0.4,
    "sources": 0.3,
    "citations": 0.4,
    "article": 0.3,
    "paper": 0.3,
    "notes": 0.2,
    "facts": 0.35,
}
REQUIRED_SECTIONS = ["## Sources", "## Extracted facts", "## Document structure", "## Limitations"]


def _load_text(ctx: ToolContext, ref: str) -> tuple[str, str]:
    if ctx.read_input is None:
        raise ToolError("no input access in this context", "forbidden")
    try:
        name, mime, data = ctx.read_input(ref)
    except Exception as e:
        raise ToolError(str(e), "forbidden") from e
    if mime not in TEXT_MIME:
        raise ToolError(
            f"{name}: {mime} documents are not supported (text/markdown only)", "unsupported"
        )
    text = data.decode("utf-8")
    if len(text) > MAX_CHARS:
        raise ToolError(f"{name}: {len(text)} chars exceeds {MAX_CHARS}", "too_large")
    return name, text


def extract(name: str, text: str) -> dict:
    lines = text.splitlines()
    headings = [
        {"level": len(m.group(1)), "text": m.group(2).strip(), "line": i + 1}
        for i, line in enumerate(lines)
        if (m := re.match(r"^(#{1,6})\s+(.+)$", line))
    ]
    title = headings[0]["text"] if headings else (lines[0].strip()[:120] if lines else name)
    facts = [
        {"type": "key_value", "key": m.group(1).strip(), "value": m.group(2).strip(), "line": i + 1}
        for i, line in enumerate(lines)
        if (m := _KV.match(line))
    ]
    seen = set()
    for i, line in enumerate(lines):
        for m in _NUM_SENT.finditer(line):
            s = m.group(0).strip()
            if len(s) > 12 and s not in seen and not line.lstrip().startswith("#"):
                seen.add(s)
                facts.append({"type": "quantitative_statement", "value": s[:300], "line": i + 1})
    links = sorted(set(_URL.findall(text)))
    words = re.findall(r"\b\w+\b", text)
    return {
        "source": name,
        "title": title,
        "headings": headings,
        "facts": facts[:200],
        "links": links,
        "word_count": len(words),
        "line_count": len(lines),
    }


async def tool_extract(ctx: ToolContext, args: dict) -> ToolOutput:
    name, text = _load_text(ctx, args["ref"])
    data = {"ref": args["ref"], **extract(name, text)}
    art = ctx.artifacts.add(
        ctx.task_id,
        "extraction",
        f"extract_{args['ref']}.json",
        json.dumps(data, indent=2),
        step_id=ctx.step_id,
        producer="documents.extract",
    )
    data["artifact_id"] = art.artifact_id
    return ToolOutput(
        f"{name}: {len(data['headings'])} headings, {len(data['facts'])} facts, "
        f"{len(data['links'])} links",
        data,
        [art.artifact_id],
    )


async def tool_compare(ctx: ToolContext, args: dict) -> ToolOutput:
    (na, ta), (nb, tb) = _load_text(ctx, args["left"]), _load_text(ctx, args["right"])
    ea, eb = extract(na, ta), extract(nb, tb)
    ha, hb = {h["text"] for h in ea["headings"]}, {h["text"] for h in eb["headings"]}
    ratio = difflib.SequenceMatcher(None, ta, tb, autojunk=False).quick_ratio()
    diff = "\n".join(
        difflib.unified_diff(ta.splitlines(), tb.splitlines(), na, nb, lineterm="", n=1)
    )
    data = {
        "left": na,
        "right": nb,
        "similarity": round(ratio, 3),
        "common_headings": sorted(ha & hb),
        "only_left": sorted(ha - hb),
        "only_right": sorted(hb - ha),
        "word_counts": [ea["word_count"], eb["word_count"]],
        "diff_lines": diff.count("\n"),
    }
    art = ctx.artifacts.add(
        ctx.task_id,
        "diff",
        "documents.diff",
        diff or "(identical)",
        step_id=ctx.step_id,
        producer="documents.compare",
    )
    return ToolOutput(f"similarity {data['similarity']}", data, [art.artifact_id])


class DocumentsPlugin:
    manifest = PluginManifest(
        id="documents",
        name="Research & Documents",
        version="1.0.0",
        description="Extract structure/facts from supplied documents, compare them, build source-attributed reports.",
        categories=["documents", "research", "reports"],
        keywords=list(KEYWORDS),
        permissions=["data:read", "artifact:write"],
        artifact_types=["extraction", "report", "diff"],
        operations=[
            OperationSpec(
                task_type="report",
                description="Extract facts from every supplied document and build a cited Markdown report",
                required_capabilities=["tool:documents.extract"],
            ),
            OperationSpec(
                task_type="compare",
                description="Compare two supplied documents",
                required_capabilities=["tool:documents.compare"],
            ),
            OperationSpec(
                task_type="cross_report",
                description="Document report plus a data profile of supplied CSV/JSON",
                required_capabilities=["tool:documents.extract", "tool:data.profile"],
            ),
            OperationSpec(
                task_type="summarize",
                description="Model-written summary grounded in supplied documents",
                required_capabilities=["tool:documents.extract", "model.text"],
            ),
            OperationSpec(
                task_type="web_research",
                description="Source-aware web research",
                required_capabilities=["search.web"],
            ),
        ],
    )

    def classify(self, req: TaskRequest, attachments: list) -> Classification | None:
        words = set(re.findall(r"[a-z]+", req.objective.lower()))
        hits = words & set(KEYWORDS)
        docs = [a for a in attachments if a["mime_type"] in TEXT_MIME]
        tabular = [a for a in attachments if a["mime_type"] in ("text/csv", "application/json")]
        score = sum(KEYWORDS[w] for w in hits) + (0.35 if docs else 0)
        if not score:
            return None
        if "research" in hits and not docs:
            ttype = "web_research"
        elif hits & {"compare"} and len(docs) == 2:
            ttype = "compare"
        elif docs and tabular:
            ttype = "cross_report"
        elif hits & {"summarize", "summary"}:
            ttype = "summarize"
        else:
            ttype = "report"
        return Classification(
            "documents",
            ttype,
            min(score, 1.0),
            [f"keywords: {sorted(hits)}", f"text documents: {len(docs)}"],
        )

    def _docs(self, env) -> list[str]:
        return [a["id"] for a in env.attachments if a["mime_type"] in TEXT_MIME]

    def plan(self, req: TaskRequest, task_type: str, env) -> PlanResult:
        docs = self._docs(env)
        if task_type == "web_research":
            return PlanResult(
                question="No web search provider is configured, so I cannot research online. "
                "Attach the source documents instead?",
                options=["documents:report"],
            )
        unsupported = [
            a["filename"]
            for a in env.attachments
            if a["mime_type"] not in TEXT_MIME | {"text/csv", "application/json"}
        ]
        if not docs:
            return PlanResult(question="Attach at least one text or Markdown document to process.")
        if task_type == "compare":
            if len(docs) != 2:
                return PlanResult(
                    question=f"Compare needs exactly 2 text documents; got {len(docs)}."
                )
            steps = [
                StepDef(
                    id="compare",
                    executor="tool",
                    ref="documents.compare",
                    inputs={"left": docs[0], "right": docs[1]},
                )
            ]
            return PlanResult(
                WorkflowDef(
                    id="documents.compare",
                    domain="documents",
                    steps=steps,
                    outputs={"comparison": "$steps.compare.output"},
                )
            )
        ext = [
            StepDef(id=f"extract_{i}", executor="tool", ref="documents.extract", inputs={"ref": d})
            for i, d in enumerate(docs)
        ]
        extra: list[StepDef] = []
        if task_type == "cross_report":
            for i, a in enumerate(
                a for a in env.attachments if a["mime_type"] in ("text/csv", "application/json")
            ):
                extra.append(
                    StepDef(
                        id=f"profile_{i}",
                        executor="tool",
                        ref="data.profile",
                        inputs={"ref": a["id"]},
                    )
                )
        deps = [s.id for s in ext + extra]
        build_inputs = {
            "extractions": [f"$steps.{s.id}.output" for s in ext],
            "profiles": [f"$steps.{s.id}.output" for s in extra],
            "unsupported": unsupported,
            "objective": "$task.objective",
        }
        steps = [*ext, *extra]
        if task_type == "summarize":
            steps.append(
                StepDef(
                    id="summary",
                    executor="agent",
                    ref="assistant",
                    depends_on=deps,
                    required_capabilities=["model.text"],
                    inputs={
                        "prompt": "$task.objective",
                        "system": "Summarize ONLY from the supplied documents. Cite each "
                        "claim as [S1], [S2] by source order. Say when the documents "
                        "do not support an answer.",
                    },
                )
            )
            build_inputs["summary"] = "$steps.summary.output.text"
            deps = [*deps, "summary"]
        steps += [
            StepDef(
                id="build",
                executor="plugin",
                ref="documents.build_report",
                depends_on=deps,
                inputs=build_inputs,
                output_schema={"type": "object", "required": ["sources", "facts"]},
            ),
            StepDef(
                id="validate",
                executor="validator",
                ref="documents.report_valid",
                depends_on=["build"],
                inputs={
                    "artifact_ids": "$steps.build.artifacts",
                    "sources": "$steps.build.output.sources",
                },
            ),
        ]
        lim = [f"unsupported attachments not processed: {unsupported}"] if unsupported else []
        return PlanResult(
            WorkflowDef(
                id=f"documents.{task_type}",
                domain="documents",
                steps=steps,
                max_concurrency=4,
                success_criteria=[Criterion(kind="gate_passed", step="validate")],
                outputs={"report": "$steps.build.output"},
            ),
            limitations=lim,
        )

    def tools(self, services) -> list[ToolDefinition]:
        ref = {"type": "string", "pattern": "^(att|art)_[0-9a-f]+$"}
        perms = frozenset({"data:read", "artifact:write"})
        return [
            ToolDefinition(
                "documents.extract",
                "Extract title, headings, facts (with line numbers) and links from a text/Markdown document.",
                {
                    "type": "object",
                    "properties": {"ref": ref},
                    "required": ["ref"],
                    "additionalProperties": False,
                },
                tool_extract,
                perms,
                environment="artifact",
                timeout_s=30,
                source="plugin:documents",
            ),
            ToolDefinition(
                "documents.compare",
                "Compare two text/Markdown documents (similarity, headings, unified diff).",
                {
                    "type": "object",
                    "properties": {"left": ref, "right": ref},
                    "required": ["left", "right"],
                    "additionalProperties": False,
                },
                tool_compare,
                perms,
                environment="artifact",
                timeout_s=30,
                source="plugin:documents",
            ),
        ]

    def idempotent_operations(self) -> set[str]:
        return {"build_report"}

    def operations(self) -> dict:
        return {"build_report": self.op_build_report}

    def validators(self) -> dict:
        return {"report_valid": self.gate_report_valid}

    async def op_build_report(self, ctx, inputs: dict) -> StepOutput:
        exts = [e for e in inputs.get("extractions") or [] if isinstance(e, dict)]
        if not exts:
            raise StepError("no extractions to report on", "invalid_input")
        sources = [
            {
                "id": f"S{i}",
                "name": e["source"],
                "ref": e["ref"],
                "title": e["title"],
                "artifact_id": e.get("artifact_id"),
            }
            for i, e in enumerate(exts, 1)
        ]
        md = [
            f"# Report: {inputs.get('objective', '')[:120]}",
            "",
            "_All facts below are extracted verbatim from the supplied documents (user-provided). "
            "No external sources were consulted._",
            "",
            "## Sources",
        ]
        md += [f"- [{s['id']}] {s['name']} — “{s['title']}” (input {s['ref']})" for s in sources]
        md += ["", "## Extracted facts"]
        facts = 0
        for s, e in zip(sources, exts, strict=True):
            for f in e["facts"][:40]:
                text = f"{f['key']}: {f['value']}" if f["type"] == "key_value" else f["value"]
                md.append(f"- {text} [{s['id']}, line {f['line']}]")
                facts += 1
        if not facts:
            md.append(
                "- No explicit facts (key: value lines or quantitative statements) were found."
            )
        md += ["", "## Document structure"]
        for s, e in zip(sources, exts, strict=True):
            md.append(f"### [{s['id']}] {s['name']} ({e['word_count']} words)")
            md += [f"{'  ' * (h['level'] - 1)}- {h['text']}" for h in e["headings"]] or [
                "- (no headings)"
            ]
        links = sorted({ln for e in exts for ln in e["links"]})
        if links:
            md += ["", "## Links found in sources", *[f"- {ln}" for ln in links]]
        profiles = [p for p in inputs.get("profiles") or [] if isinstance(p, dict)]
        if profiles:
            md += ["", "## Data summary"]
            for p in profiles:
                md.append(
                    f"- {p.get('source')}: {p.get('rows')} rows, columns {list(p.get('columns', {}))}"
                )
        if inputs.get("summary"):
            md += ["", "## Model summary (generated; verify against sources)", inputs["summary"]]
        md += [
            "",
            "## Limitations",
            "- Extraction is rule-based (headings, `Key: value` lines, quantitative sentences).",
            "- Links were not fetched or verified online.",
        ]
        if inputs.get("unsupported"):
            md.append(f"- Not processed (unsupported format): {', '.join(inputs['unsupported'])}")
        art = ctx.add_artifact("report", "report.md", "\n".join(md) + "\n")
        return StepOutput(
            {"sources": sources, "facts": facts, "links": len(links)}, [art.artifact_id]
        )

    async def gate_report_valid(self, ctx, inputs: dict) -> GateResult:
        ids = inputs.get("artifact_ids") or []
        findings = []
        if not ids:
            return GateResult(
                validator_id="documents.report_valid",
                status="failed",
                findings=[{"severity": "high", "message": "no report artifact"}],
            )
        text = ctx.read_artifact(ids[0]).decode("utf-8")
        for sec in REQUIRED_SECTIONS:
            if sec not in text:
                findings.append({"severity": "high", "message": f"missing section {sec!r}"})
        known = {s["id"] for s in inputs.get("sources") or []}
        cited = set(re.findall(r"\[(S\d+)[,\]]", text))
        for c in sorted(cited - known):
            findings.append({"severity": "high", "message": f"citation {c} has no source"})
        for url in _URL.findall(text):
            p = urlparse(url)
            if not (p.scheme in ("http", "https") and p.netloc):
                findings.append({"severity": "low", "message": f"malformed link {url}"})
        return GateResult(
            validator_id="documents.report_valid",
            status="failed" if any(f["severity"] == "high" for f in findings) else "passed",
            evidence=ids,
            findings=findings,
            metrics={"chars": len(text), "citations": len(cited), "sources": len(known)},
        )


def create_plugin():
    return DocumentsPlugin()
