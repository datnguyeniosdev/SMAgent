"""Generic platform: capability registry, plugins, routing, intake, and real plugin workflows.

Everything here runs real local tools (filesystem, git, subprocess) through the actual
intake -> planner -> engine -> validators path. Only model/agent/image providers are fakes.
"""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.capabilities import CapabilityManifest, CapabilityRegistry, CapStatus
from app.connectors.base import AgentResult
from app.intake import TaskRequest
from app.models import TaskState
from app.plugins.base import PluginManager
from app.tools import ToolRegistry
from tests.conftest import MODEL, FakeConnector, build_orch, make_png, wait_done

CSV = b"id,region,amount\n1,EU,10\n2,US,5\n3,EU,7\n3,EU,7\n4,APAC,\n"


async def run(orch, req: TaskRequest, timeout=20):
    task = await orch.submit_request(req)
    await wait_done(orch, task, timeout)
    return task


def steps(orch, task):
    return {r.step_id: r for r in orch.load_steps(task.run_id).values()} if task.run_id else {}


# ---------- capability registry ----------


async def test_capability_lifecycle_and_health_isolation():
    reg = CapabilityRegistry()

    async def broken():
        raise RuntimeError("x")

    reg.register(
        CapabilityManifest(
            id="agent:a",
            kind="agent",
            name="a",
            provides=["model.text"],
            status=CapStatus.validated,
        ),
        health=broken,
    )
    reg.register(
        CapabilityManifest(
            id="agent:b",
            kind="agent",
            name="b",
            provides=["model.text"],
            status=CapStatus.configured,
        )
    )
    assert [m.id for m in reg.providers_of("model.text")] == [
        "agent:a"
    ]  # configured is not routable
    await reg.check_health()
    assert (
        reg.get("agent:a").status == CapStatus.unavailable
        and "health check error" in reg.get("agent:a").evidence
    )
    reg.set_status("agent:b", CapStatus.validated, "binary ok")
    reg.record_execution("agent:b", True, "ran")
    assert reg.get("agent:b").status == CapStatus.operational
    reg.record_execution("agent:b", False, "401")
    assert reg.get("agent:b").status == CapStatus.degraded and reg.routable("agent:b")
    with pytest.raises(ValueError):
        reg.register(CapabilityManifest(id="noprefix", kind="tool", name="x"))


async def test_unconfigured_integrations_not_operational(settings):
    orch, _ = build_orch(settings, [FakeConnector("openai", MODEL)])
    caps = {c.id: c.status for c in orch.caps.list()}
    assert (
        caps["provider:openai"] == CapStatus.validated
    )  # configured fake, never "operational" before a run
    assert "tool:media.generate" not in caps  # no image provider -> tool not even registered
    assert all(
        caps[f"plugin:{p}"] == CapStatus.validated
        for p in ("software", "documents", "data", "media", "unity")
    )


# ---------- plugins ----------


def test_plugin_isolation_and_permission_enforcement(tmp_path, monkeypatch):
    pkg = tmp_path / "evilplug"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "esc.py").write_text(
        "from app.plugins.base import PluginManifest, OperationSpec\n"
        "from app.tools import ToolDefinition\n"
        "class P:\n"
        "    manifest = PluginManifest(id='esc', name='e', version='1', description='d', permissions=['workspace:read'],\n"
        "                              operations=[OperationSpec(task_type='x', description='x')])\n"
        "    def tools(self, s):\n"
        "        return [ToolDefinition('esc.shell', 'd', {'type': 'object'}, None, frozenset({'process:execute'}))]\n"
        "    def operations(self): return {}\n"
        "def create_plugin(): return P()\n"
    )
    (pkg / "prefix.py").write_text(
        (pkg / "esc.py")
        .read_text()
        .replace("'esc.shell'", "'other.tool'")
        .replace("process:execute", "workspace:read")
        .replace("id='esc'", "id='prefix'")
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    pm = PluginManager(
        frozenset({"workspace:read", "process:execute", "data:read", "artifact:write"})
    )
    reg = ToolRegistry()
    assert (
        "not granted" in pm.load("evilplug.esc", None, reg).error
    )  # manifest did not request process:execute
    assert "prefixed" in pm.load("evilplug.prefix", None, reg).error
    assert pm.load("does.not.exist", None, reg).error.startswith("ModuleNotFoundError")
    assert reg.list() == [] and pm.active() == []
    ok = pm.load("app.plugins.data", None, reg)
    assert ok.active and "data.transform" in ok.tool_names


async def test_deactivated_plugin_is_not_routed(settings):
    orch, _ = build_orch(settings, [])
    orch.plugins.set_active("data", False)
    att = orch.attachments.save(CSV, "s.csv", "text/csv", "api")
    t = await run(
        orch, TaskRequest(objective="profile", domain="data", attachment_ids=[att.attachment_id])
    )
    assert t.state == TaskState.failed and "not active" in t.error


# ---------- routing / intake ----------


def test_intake_validation():
    with pytest.raises(ValueError):
        TaskRequest(objective="   ")
    r = TaskRequest(objective="x", constraints=["no network"], idempotency_key="k1")
    assert r.mode == "auto" and r.constraints == ["no network"]


async def test_routing_decisions_are_explained(settings):
    orch, _ = build_orch(settings, [])
    att = orch.attachments.save(CSV, "s.csv", "text/csv", "api")
    meta = [
        {"id": att.attachment_id, "filename": "s.csv", "mime_type": "text/csv", "size_bytes": 10}
    ]
    d = orch.planner.route(TaskRequest(objective="profile this dataset"), meta)
    assert (d.mode, d.plugin_id, d.task_type) == ("classified", "data", "profile") and d.candidates
    d = orch.planner.route(TaskRequest(objective="what is the weather"), [])
    assert d.mode == "clarify" and "no model" in d.reasons[0]  # no unrelated plugin is picked
    d = orch.planner.route(TaskRequest(objective="x", domain="nope"), [])
    assert d.mode == "rejected"
    orch2, _ = build_orch(settings, [FakeConnector("openai", MODEL)])
    d = orch2.planner.route(TaskRequest(objective="what is the weather"), [])
    assert (d.mode, d.plugin_id) == ("fallback", "general")


async def test_clarification_round_trip(settings):
    orch, notes = build_orch(settings, [])
    att = orch.attachments.save(CSV, "s.csv", "text/csv", "api")
    task = await orch.submit_request(
        TaskRequest(objective="please handle this", attachment_ids=[att.attachment_id])
    )
    for _ in range(50):
        if task.state == TaskState.waiting_for_user:
            break
        await asyncio.sleep(0.02)
    assert task.state == TaskState.waiting_for_user and task.question
    assert any(a.kind == "answer" for c in notes.calls for a in c["actions"])
    await orch.answer(task.task_id, "data:profile", "11")
    await wait_done(orch, task)
    assert task.state == TaskState.succeeded and task.domain == "data"


async def test_constraints_preserved_and_idempotency(settings):
    orch, _ = build_orch(settings, [])
    att = orch.attachments.save(CSV, "s.csv", "text/csv", "api")
    req = TaskRequest(
        objective="profile",
        domain="data",
        task_type="profile",
        attachment_ids=[att.attachment_id],
        constraints=["do not drop rows"],
        idempotency_key="same-key",
    )
    t1 = await run(orch, req)
    assert orch.load_run(t1.run_id).workflow.constraints == ["do not drop rows"]
    t2 = await orch.submit_request(req)
    assert t2 is t1  # duplicate submission returns the original task


async def test_direct_tool_task(settings, repo):
    orch, notes = build_orch(settings, [])
    t = await run(
        orch,
        TaskRequest(objective="list", tool="workspace.list_files", tool_args={"pattern": "*.py"}),
    )
    assert t.state == TaskState.succeeded
    assert steps(orch, t)["run"].output["files"] == ["main.py"]
    assert (
        "🔧 <b>CÔNG CỤ</b>" in notes.texts()
        and "✅ <code>workspace.list_files</code>" in notes.texts()
    )  # real tool event reported
    t = await run(orch, TaskRequest(objective="x", tool="nope.tool"))
    assert t.state == TaskState.failed and "unknown tool" in t.error


# ---------- data plugin ----------


async def test_data_transform_end_to_end(settings):
    orch, _ = build_orch(settings, [])
    att = orch.attachments.save(CSV, "sales.csv", "text/csv", "api")
    t = await run(
        orch,
        TaskRequest(
            objective="filter EU rows",
            attachment_ids=[att.attachment_id],
            data={
                "operations": [
                    {"op": "filter", "column": "region", "equals": "EU"},
                    {"op": "dedupe"},
                    {"op": "sort", "by": "amount", "desc": True},
                ],
                "schema": {"columns": {"id": "int", "amount": "float"}, "required": ["id"]},
            },
        ),
    )
    assert (t.state, t.domain, t.task_type) == (TaskState.succeeded, "data", "transform"), t.error
    st = steps(orch, t)
    out = orch.artifacts.get(st["produce"].output["artifact_id"])
    assert orch.artifacts.read(out).decode() == "id,region,amount\n1,EU,10\n3,EU,7\n"
    meta = st["produce"].output
    assert (
        meta["rows_in"] == 5 and meta["rows_out"] == 2 and meta["operations"][1]["discarded"] == 1
    )
    assert out.validation_status == "passed" and out.producer == "data.transform"
    assert orch.attachments.read(orch.attachments.get(att.attachment_id)) == CSV  # input untouched
    env = orch.envelope(t)
    assert env.status == "succeeded" and env.validations[0].validator_id == "data.output_valid"


async def test_data_schema_violation_blocks_export(settings):
    orch, _ = build_orch(settings, [])
    att = orch.attachments.save(CSV, "sales.csv", "text/csv", "api")
    t = await run(
        orch,
        TaskRequest(
            objective="transform",
            domain="data",
            task_type="transform",
            attachment_ids=[att.attachment_id],
            data={"operations": [{"op": "limit", "n": 2}], "schema": {"required": ["amount"]}},
        ),
    )
    st = steps(orch, t)
    assert (
        t.state == TaskState.failed
        and st["schema_gate"].state == "failed"
        and st["produce"].state in ("skipped", "cancelled")
    )
    assert not [a for a in orch._all_artifacts(t) if a.kind == "data"]


async def test_data_aggregate_join_convert(settings):
    orch, _ = build_orch(settings, [])
    a = orch.attachments.save(CSV, "sales.csv", "text/csv", "api")
    t = await run(
        orch,
        TaskRequest(
            objective="aggregate",
            domain="data",
            task_type="aggregate",
            attachment_ids=[a.attachment_id],
            data={"group_by": ["region"], "metrics": {"amount": ["sum", "count"]}},
        ),
    )
    out = orch.artifacts.read(
        orch.artifacts.get(steps(orch, t)["produce"].output["artifact_id"])
    ).decode()
    assert t.state == TaskState.succeeded and "EU,24,3" in out and "APAC,,1" in out
    regions = orch.attachments.save(
        b'[{"region":"EU","name":"Europe"},{"region":"US","name":"United States"}]',
        "regions.json",
        "application/json",
        "api",
    )
    a2 = orch.attachments.save(CSV, "sales.csv", "text/csv", "api")
    t = await run(
        orch,
        TaskRequest(
            objective="join",
            domain="data",
            task_type="join",
            attachment_ids=[a2.attachment_id, regions.attachment_id],
            data={"on": "region"},
        ),
    )
    meta = steps(orch, t)["produce"].output
    assert t.state == TaskState.succeeded and meta["rows_out"] == 4 and meta["unmatched_left"] == 1
    a3 = orch.attachments.save(CSV, "sales.csv", "text/csv", "api")
    t = await run(
        orch,
        TaskRequest(
            objective="convert",
            domain="data",
            task_type="convert",
            attachment_ids=[a3.attachment_id],
            data={"to": "json"},
        ),
    )
    rows = json.loads(
        orch.artifacts.read(orch.artifacts.get(steps(orch, t)["produce"].output["artifact_id"]))
    )
    assert t.state == TaskState.succeeded and rows[0] == {"id": 1, "region": "EU", "amount": 10}


# ---------- documents plugin ----------

DOC = b"# Launch plan\n\nBudget: 1200 USD\nOwner: Ana\n\nUsers grew 40% in Q3.\n\n## Risks\n- vendor delay\nSee https://example.com/spec\n"


async def test_documents_report_with_sources(settings):
    orch, _ = build_orch(settings, [])
    d = orch.attachments.save(DOC, "plan.md", "text/markdown", "api")
    pdf = orch.attachments.save(b"%PDF-1.4", "old.pdf", "application/pdf", "api")
    t = await run(
        orch,
        TaskRequest(
            objective="extract facts into a report",
            attachment_ids=[d.attachment_id, pdf.attachment_id],
        ),
    )
    assert (t.state, t.domain, t.task_type) == (TaskState.succeeded, "documents", "report"), t.error
    report = orch.artifacts.read(
        orch.artifacts.get(steps(orch, t)["build"].artifact_ids[0])
    ).decode()
    assert (
        "- Budget: 1200 USD [S1, line 3]" in report
        and "Users grew 40% in Q3. [S1, line 6]" in report
    )
    assert (
        "Not processed (unsupported format): old.pdf" in report
    )  # never silently treated as processed
    assert "No external sources were consulted" in report
    env = orch.envelope(t)
    assert env.validations[0].status == "passed" and env.validations[0].metrics["sources"] == 1


async def test_documents_gate_rejects_invented_citation(settings):
    from app.orchestrator import StepContext
    from app.plugins.documents import DocumentsPlugin

    orch, _ = build_orch(settings, [])
    t = await run(orch, TaskRequest(objective="list", tool="workspace.list_files"))
    art = orch.artifacts.add(
        t.task_id,
        "report",
        "r.md",
        "## Sources\n## Extracted facts\n- x [S9]\n## Document structure\n",
    )
    gate = await DocumentsPlugin().gate_report_valid(
        StepContext(orch, t, None, None),
        {"artifact_ids": [art.artifact_id], "sources": [{"id": "S1"}]},
    )
    msgs = [f["message"] for f in gate.findings]
    assert (
        gate.status == "failed"
        and "citation S9 has no source" in msgs
        and "missing section '## Limitations'" in msgs
    )


async def test_documents_compare_and_web_research_refused(settings):
    orch, _ = build_orch(settings, [])
    a = orch.attachments.save(DOC, "a.md", "text/markdown", "api")
    b = orch.attachments.save(
        DOC.replace(b"Risks", b"Dependencies"), "b.md", "text/markdown", "api"
    )
    t = await run(
        orch,
        TaskRequest(
            objective="compare these documents", attachment_ids=[a.attachment_id, b.attachment_id]
        ),
    )
    out = steps(orch, t)["compare"].output
    assert (
        t.task_type == "compare"
        and out["only_left"] == ["Risks"]
        and out["only_right"] == ["Dependencies"]
    )
    t = await run(
        orch,
        TaskRequest(objective="research GPU prices", domain="documents", task_type="web_research"),
    )
    assert (
        t.state == TaskState.failed and "search.web" in t.error
    )  # NOT CONFIGURED: no pretend search


async def test_cross_domain_report(settings):
    orch, _ = build_orch(settings, [])
    d = orch.attachments.save(DOC, "plan.md", "text/markdown", "api")
    c = orch.attachments.save(CSV, "sales.csv", "text/csv", "api")
    t = await run(
        orch,
        TaskRequest(
            objective="build a report from the document and data",
            attachment_ids=[d.attachment_id, c.attachment_id],
        ),
    )
    st = steps(orch, t)
    assert t.task_type == "cross_report" and st["profile_0"].executor_ref == "tool:data.profile"
    report = orch.artifacts.read(orch.artifacts.get(st["build"].artifact_ids[0])).decode()
    assert "## Data summary" in report and "sales.csv: 5 rows" in report


async def test_documents_summarize_needs_model(settings):
    orch, _ = build_orch(settings, [])
    d = orch.attachments.save(DOC, "plan.md", "text/markdown", "api")
    t = await run(
        orch,
        TaskRequest(
            objective="summarize",
            domain="documents",
            task_type="summarize",
            attachment_ids=[d.attachment_id],
        ),
    )
    assert (
        t.state == TaskState.failed
        and t.error_category == "unsupported"
        and "model.text" in t.error
    )
    seen = []

    def answer(req):
        seen.append(req.prompt)
        return AgentResult("Budget is 1200 USD [S1].", "openai", "m")

    orch, _ = build_orch(settings, [FakeConnector("openai", MODEL, answer)])
    d = orch.attachments.save(DOC, "plan.md", "text/markdown", "api")
    t = await run(
        orch,
        TaskRequest(
            objective="summarize the plan",
            domain="documents",
            task_type="summarize",
            attachment_ids=[d.attachment_id],
        ),
    )
    report = orch.artifacts.read(
        orch.artifacts.get(steps(orch, t)["build"].artifact_ids[0])
    ).decode()
    assert (
        t.state == TaskState.succeeded
        and "Model summary (generated; verify against sources)" in report
    )


# ---------- media plugin ----------


class FakeImages:
    name, model, verification = "fake_images", "img-1", "mocked"

    async def generate(self, prompt, size, transparent):
        w, h = map(int, size.split("x"))
        return make_png(w, h, alpha=transparent)


async def test_media_inspect_and_generate(settings):
    orch, _ = build_orch(settings, [])
    img = orch.attachments.save(make_png(64, 32), "icon.png", "image/png", "api")
    t = await run(
        orch, TaskRequest(objective="inspect this image", attachment_ids=[img.attachment_id])
    )
    assert (t.state, t.task_type) == (TaskState.succeeded, "inspect")
    assert steps(orch, t)["inspect_0"].output["width"] == 64
    t = await run(
        orch, TaskRequest(objective="generate a logo image", domain="media", task_type="generate")
    )
    assert (
        t.state == TaskState.failed and "image.generate" in t.error
    )  # NOT CONFIGURED, nothing faked
    orch, notes = build_orch(settings, [], image=FakeImages())
    orch.caps.set_status("image_provider:fake_images", CapStatus.validated, "test")
    t = await run(
        orch,
        TaskRequest(
            objective="generate a logo",
            domain="media",
            task_type="generate",
            data={"prompt": "gold coin", "width": 32, "height": 32, "transparent": True},
        ),
    )
    assert t.state == TaskState.succeeded, t.error
    gen = steps(orch, t)["generate"]
    assert gen.approved_by == "1" and gen.output["width"] == 32 and gen.output["has_alpha"] is True
    assert orch.artifacts.get(gen.artifact_ids[0]).validation_status == "passed"


# ---------- software plugin ----------


async def test_software_apply_files_isolated_with_real_checks(settings, repo):
    settings.qa_commands = {"pytest": f"{sys.executable} -m pytest -q -p no:cacheprovider"}
    orch, notes = build_orch(settings, [], qa_commands=settings.qa_commands)
    files = {
        "greeting.py": "def greet():\n    return 'hello'\n",
        "test_greeting.py": "from greeting import greet\n\ndef test_greet():\n    assert greet() == 'hello'\n",
    }
    t = await run(
        orch,
        TaskRequest(objective="add these files and run tests", data={"files": files}),
        timeout=60,
    )
    assert (t.state, t.domain, t.task_type) == (TaskState.succeeded, "software", "apply_files"), (
        t.error
    )
    st = steps(orch, t)
    assert st["write_0"].approved_by == "1"  # writes required approval
    assert (
        st["check_pytest"].output["status"] == "passed"
        and st["check_pytest"].output["exit_code"] == 0
    )
    assert st["report"].output["files_match"] is True
    assert sorted(st["report"].output["changed_files"]) == ["greeting.py", "test_greeting.py"]
    assert not (repo / "greeting.py").exists()  # main checkout untouched; nothing merged
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=repo, capture_output=True, text=True
    ).stdout
    assert len(log.splitlines()) == 1


async def test_software_failing_check_fails_task(settings, repo):
    settings.qa_commands = {"pytest": f"{sys.executable} -m pytest -q -p no:cacheprovider"}
    orch, _ = build_orch(settings, [], qa_commands=settings.qa_commands)
    files = {"test_bad.py": "def test_bad():\n    assert 1 == 2\n"}
    t = await run(
        orch,
        TaskRequest(
            objective="add", domain="software", task_type="apply_files", data={"files": files}
        ),
        timeout=60,
    )
    assert t.state == TaskState.failed
    gate = steps(orch, t)["gate"]
    assert gate.state == "failed" and gate.output["findings"][0]["message"].startswith(
        "pytest: failed"
    )


async def test_software_inspect_repo(settings, repo):
    orch, _ = build_orch(settings, [])
    t = await run(
        orch, TaskRequest(objective="inspect the repository structure", domain="software")
    )
    assert t.state == TaskState.succeeded and steps(orch, t)["report"].output["by_extension"] == {
        "py": 1
    }


# ---------- unity plugin ----------


async def test_unity_inspect_project_and_editor_unavailable(settings, repo):
    (repo / "ProjectSettings").mkdir()
    (repo / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 6000.3.24f1\n")
    (repo / "Packages").mkdir()
    (repo / "Packages" / "manifest.json").write_text(
        json.dumps({"dependencies": {"com.unity.ugui": "2.0.0"}})
    )
    (repo / "Assets" / "Scenes").mkdir(parents=True)
    (repo / "Assets" / "Scenes" / "Main.unity").write_text("x")
    (repo / "Assets" / "Player.cs").write_text("class Player {}")
    orch, _ = build_orch(settings, [])
    t = await run(orch, TaskRequest(objective="inspect the unity project packages"))
    assert (t.state, t.domain, t.task_type) == (TaskState.succeeded, "unity", "inspect_project"), (
        t.error
    )
    info = steps(orch, t)["info"].output
    assert info["editor_version"] == "6000.3.24f1" and info["scenes"] == 1 and info["scripts"] == 1
    gate = steps(orch, t)["check"].output
    assert any("no MCP package" in f["message"] for f in gate["findings"])
    t = await run(
        orch,
        TaskRequest(objective="unity editor status", domain="unity", task_type="editor_status"),
    )
    assert t.state == TaskState.failed and "mcp.unity.editor_status" in t.error


def test_core_has_no_domain_specific_dependencies():
    # Core may name plugins only as configuration data (enabled list, legacy workflow alias).
    allowed = {"app/config.py", "app/intake.py", "app/gateway_config.py"}
    core = [p for p in Path("app").glob("*.py")] + list(Path("app/connectors").glob("*.py"))
    offenders = [str(p) for p in core if str(p) not in allowed and "unity" in p.read_text().lower()]
    imports = [
        str(p)
        for p in core
        if "from app.plugins." in p.read_text().replace("from app.plugins.base", "")
    ]
    assert offenders == [] and imports == []


# ---------- REST over the generic service ----------


def test_rest_generic_endpoints(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.config import get_settings

    monkeypatch.setenv("API_TOKEN", "tok-123456")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "var"))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.chdir(
        tmp_path
    )  # never load the developer's real .env (it may hold a live bot token)
    get_settings.cache_clear()
    from app.main import app

    h = {"Authorization": "Bearer tok-123456"}
    with TestClient(app) as c:
        caps = c.get("/api/v1/capabilities", headers=h).json()
        assert {x["id"] for x in caps} >= {
            "plugin:data",
            "runtime:claude_code",
            "tool:data.transform",
        }
        assert {p["id"] for p in c.get("/api/v1/plugins", headers=h).json()} == {
            "software",
            "documents",
            "data",
            "media",
            "unity",
        }
        assert any(
            t["name"] == "data.profile" for t in c.get("/api/v1/tools", headers=h).json()["tools"]
        )
        att = c.post(
            "/api/v1/attachments", headers=h, files={"file": ("s.csv", CSV, "text/csv")}
        ).json()
        r = c.post(
            "/api/v1/tasks",
            headers=h,
            json={"objective": "profile this csv", "attachment_ids": [att["attachment_id"]]},
        )
        tid = r.json()["task_id"]
        for _ in range(100):
            body = c.get(f"/api/v1/tasks/{tid}", headers=h).json()
            if body["task"]["state"] in ("succeeded", "failed"):
                break
        assert (
            body["task"]["state"] == "succeeded"
            and [s["step_id"] for s in body["workflow_steps"]][-1] == "summary"
        )
        env = c.get(f"/api/v1/tasks/{tid}/result", headers=h).json()
        report = next(a for a in env["artifacts"] if a["filename"] == "data_summary.md")
        content = c.get(f"/api/v1/artifacts/{report['artifact_id']}/content", headers=h)
        assert content.status_code == 200 and b"# Data summary" in content.content
        assert c.get(f"/api/v1/artifacts/{report['artifact_id']}/content").status_code == 401
        events = [e["type"] for e in c.get(f"/api/v1/tasks/{tid}/events", headers=h).json()]
        assert (
            "workflow.created" in events
            and "workflow.completed" in events
            and "tool.finished" in events
        )
        wf = c.get(f"/api/v1/tasks/{tid}/workflow", headers=h).json()
        assert wf["definition"]["id"] == "data.profile"
        assert c.post("/api/v1/plugins/data/deactivate", headers=h).json()["active"] is False
        assert c.post(f"/api/v1/tasks/{tid}/cancel", headers=h).json()["stopped"] is False
    get_settings.cache_clear()


@pytest.mark.timeout(60)  # spawns a real MCP server process per attempt
async def test_engine_retry_reexecutes_real_tool(settings, tmp_path):
    """Regression: retries of an idempotent tool step must really call the tool again."""
    fixture = str(Path(__file__).parent / "fixtures" / "mcp_echo_server.py")
    settings.mcp_servers = [
        {
            "name": "fx",
            "command": sys.executable,
            "args": [fixture],
            "allowed_tools": ["flaky"],
            "read_only_tools": ["flaky"],
        }
    ]
    settings.workspace_root = None
    orch, _ = build_orch(settings, [])
    await orch.discover_mcp()
    wf = {
        "id": "t.retry",
        "steps": [
            {
                "id": "f",
                "executor": "tool",
                "ref": "mcp.fx.flaky",
                "inputs": {"fail_times": 2},
                "retry": {"max_attempts": 3, "retry_on": ["mcp_tool_error"]},
            }
        ],
    }
    t = await run(orch, TaskRequest(objective="retry", workflow=wf), timeout=60)
    rec = steps(orch, t)["f"]
    assert (
        t.state == TaskState.succeeded
        and rec.attempt == 3
        and "ok after 3 calls" in json.dumps(rec.output)
    )
