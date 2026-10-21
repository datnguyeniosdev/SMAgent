"""Run every required demonstration for real and write evidence.

Usage:  uv run python -m scripts.demo [--unity-project PATH]
Writes: docs/evidence/demo_results.json and docs/DEMO_EVIDENCE.md

Everything goes through the real gateway (TaskRequest -> planner -> engine -> tools/plugins
-> validators -> artifacts). External providers are used only if configured; otherwise the
demo records the actual capability error. Telegram is NOT used (see scripts/smoke_telegram.py).
"""

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from app.config import Settings
from app.intake import TaskRequest
from app.main import build
from tests.conftest import make_png

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
MCP_FIXTURE = str(ROOT / "tests" / "fixtures" / "mcp_echo_server.py")
CSV = b"id,region,amount\n1,EU,10\n2,US,5\n3,EU,7\n3,EU,7\n4,APAC,\n5,US,12\n"
DOC_A = b"# Launch plan\n\nBudget: 1200 USD\nOwner: Ana\n\nUsers grew 40% in Q3.\n\n## Risks\n- vendor delay\nSee https://example.com/spec\n"
DOC_B = b"# Vendor notes\n\nContract: 12 months\nPrice: 300 USD per month\n\n## Timeline\nDelivery slipped by 2 weeks in May.\n"


class DemoNotifier:
    """Collects concise reports; auto-approves only when told to (approval demo approves manually)."""

    def __init__(self):
        self.lines: list[str] = []
        self.auto_approve = True
        self.orch = None

    async def report(self, task, html, *, actions=(), files=(), photos=(), control=False):
        from app.formatting import plain

        self.lines.append(
            f"[{'control' if control else 'channel'}] {plain(html).splitlines()[0][:140]}"
        )
        if self.auto_approve:
            for a in actions:
                if a.kind == "approve":
                    asyncio.get_running_loop().call_soon(
                        lambda t=a.token: asyncio.ensure_future(
                            self.orch.apply_action(t, "demo-user")
                        )
                    )


def git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "app.py").write_text("def add(a, b):\n    return a + b\n")
    (path / "test_app.py").write_text(
        "from app import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    )
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "demo",
        "GIT_AUTHOR_EMAIL": "demo@example.com",
        "GIT_COMMITTER_NAME": "demo",
        "GIT_COMMITTER_EMAIL": "demo@example.com",
    }
    for cmd in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "add", "."],
        ["git", "commit", "-qm", "init"],
    ):
        subprocess.run(cmd, cwd=path, check=True, env=env)
    return path


def evidence(o, task, extra=None) -> dict:
    steps = []
    if task.run_id:
        for r in o.load_steps(task.run_id).values():
            steps.append(
                {
                    "step": r.step_id,
                    "state": r.state.value,
                    "attempts": r.attempt,
                    "duration_s": r.duration_s,
                    "executor": r.executor_ref,
                    "started": r.started_at.isoformat() if r.started_at else None,
                    "finished": r.finished_at.isoformat() if r.finished_at else None,
                    "error": (r.error or "")[:200] or None,
                }
            )
    arts = [
        {
            "kind": a.kind,
            "file": a.filename,
            "sha256": a.sha256[:16],
            "bytes": a.size_bytes,
            "producer": a.producer,
            "validation": a.validation_status,
        }
        for a in o._all_artifacts(task)
        if a.kind != "envelope"
    ]
    env = o.envelope(task)
    return {
        "task_id": task.task_id,
        "state": task.state.value,
        "domain": task.domain,
        "operation": task.task_type,
        "error": task.error,
        "error_category": task.error_category,
        "routing": {
            k: v
            for k, v in (task.request.get("_routing") or {}).items()
            if k in ("mode", "plugin_id", "task_type", "score", "reasons")
        },
        "steps": steps,
        "artifacts": arts,
        "validations": [
            {"id": v.validator_id, "status": v.status, "metrics": v.metrics}
            for v in env.validations
        ],
        "event_types": sorted({e["type"] for e in o.store.events(task.task_id)}),
        **(extra or {}),
    }


async def wait(o, task, timeout=120):
    t = o._running.get(task.task_id)
    if t:
        await asyncio.wait_for(asyncio.shield(t), timeout)
    return task


async def submit(o, **kw):
    task = await o.submit_request(TaskRequest(user_id="demo-user", **kw))
    return await wait(o, task)


async def main(unity_project: str | None):
    tmp = Path(tempfile.mkdtemp(prefix="agw_demo_"))
    repo = git_repo(tmp / "repo")
    settings = Settings(
        _env_file=None,
        data_dir=tmp / "var",
        workspace_root=repo,
        action_ttl_s=120,
        tool_permissions=[*Settings.model_fields["tool_permissions"].default, "mcp:execute"],
        claude_code_enabled=bool(shutil.which("claude")),
        qa_commands={
            "pytest": f"{PY} -m pytest -q -p no:cacheprovider",
            "sleep_a": f'{PY} -c "import time; time.sleep(1.5)"',
            "sleep_b": f'{PY} -c "import time; time.sleep(1.5)"',
            "slow": f'{PY} -c "import time; time.sleep(60)"',
            "fail": f"{PY} -c \"import sys; print('simulated failure'); sys.exit(3)\"",
        },
        mcp_servers=[
            {
                "name": "fx",
                "command": PY,
                "args": [MCP_FIXTURE],
                "allowed_tools": ["echo", "flaky", "explode"],
                "read_only_tools": ["echo", "flaky"],
            }
        ]
        + (
            [
                {
                    "name": "unity",
                    "command": str(Path.home() / ".unity/bin/unity"),
                    "args": ["mcp"],
                    "allowed_tools": ["editor_status"],
                    "read_only_tools": ["editor_status"],
                    "timeout_s": 60,
                }
            ]
            if (Path.home() / ".unity/bin/unity").exists()
            else []
        ),
    )
    o = build(settings)
    note = DemoNotifier()
    note.orch = o
    o.notifier = note
    await o.start()
    results: dict = {
        "environment": {
            "python": sys.version.split()[0],
            "capabilities": {c.id: c.status for c in o.caps.list() if c.kind != "tool"},
            "tools": [t.name for t in o.tools.list()],
            "mcp_notes": o.tools.discovery_notes[:10],
        }
    }

    def att(data, name, mime):
        return o.attachments.save(data, name, mime, "demo").attachment_id

    # ---------- Phase 4 ----------
    t = await submit(
        o,
        objective="transform sales data",
        attachment_ids=[att(CSV, "sales.csv", "text/csv")],
        data={
            "operations": [{"op": "filter", "column": "region", "equals": "EU"}, {"op": "dedupe"}],
            "schema": {"columns": {"id": "int"}, "required": ["id"]},
        },
    )
    out = o.artifacts.read(
        o.artifacts.get(o.load_steps(t.run_id)["produce"].output["artifact_id"])
    ).decode()
    results["P4-A sequential"] = evidence(o, t, {"output_csv": out})

    wf = {
        "id": "demo.parallel",
        "steps": [
            {"id": "a", "executor": "tool", "ref": "qa.run", "inputs": {"check": "sleep_a"}},
            {"id": "b", "executor": "tool", "ref": "qa.run", "inputs": {"check": "sleep_b"}},
            {
                "id": "aggregate",
                "executor": "internal",
                "ref": "aggregate",
                "depends_on": ["a", "b"],
                "inputs": {"a": "$steps.a.output.status", "b": "$steps.b.output.status"},
            },
        ],
        "outputs": {"result": "$steps.aggregate.output"},
    }
    t = await submit(o, objective="parallel demo", workflow=wf)
    st = {s["step"]: s for s in evidence(o, t)["steps"]}
    overlap = st["a"]["started"] < st["b"]["finished"] and st["b"]["started"] < st["a"]["finished"]
    after = st["aggregate"]["started"] >= max(st["a"]["finished"], st["b"]["finished"])
    results["P4-B parallel"] = evidence(o, t, {"overlapped": overlap, "aggregate_waited": after})

    wf = {
        "id": "demo.retry",
        "steps": [
            {
                "id": "flaky",
                "executor": "tool",
                "ref": "mcp.fx.flaky",
                "inputs": {"fail_times": 2},
                "retry": {
                    "max_attempts": 3,
                    "delay_s": 0.2,
                    "backoff": 2,
                    "retry_on": ["mcp_tool_error"],
                },
            }
        ],
        "outputs": {"result": "$steps.flaky.output"},
    }
    t = await submit(o, objective="retry demo", workflow=wf)
    results["P4-C1 retry (idempotent, real MCP)"] = evidence(o, t)
    wf = {
        "id": "demo.failure",
        "failure_policy": "continue",
        "steps": [
            {
                "id": "non_idempotent",
                "executor": "tool",
                "ref": "mcp.fx.explode",
                "on_failure": "continue",
                "retry": {"max_attempts": 3, "retry_on": ["mcp_tool_error"]},
            },
            {
                "id": "side_effect",
                "executor": "tool",
                "ref": "qa.run",
                "inputs": {"check": "fail"},
                "on_failure": "continue",
            },
            {
                "id": "gate",
                "executor": "internal",
                "ref": "assert",
                "depends_on": ["side_effect"],
                "inputs": {"value": "$steps.side_effect.output.status", "equals": "passed"},
                "on_failure": "continue",
            },
            {
                "id": "after_gate",
                "executor": "internal",
                "ref": "select",
                "depends_on": ["gate"],
                "inputs": {"value": 1},
            },
            {
                "id": "independent",
                "executor": "tool",
                "ref": "mcp.fx.echo",
                "inputs": {"text": "branch still ran"},
            },
        ],
    }
    t = await submit(o, objective="failure demo", workflow=wf)
    results["P4-C2 failure policy"] = evidence(o, t)

    wf = {
        "id": "demo.cancel",
        "steps": [{"id": "long", "executor": "tool", "ref": "qa.run", "inputs": {"check": "slow"}}],
    }
    task = await o.submit_request(TaskRequest(objective="cancel demo", workflow=wf))
    for _ in range(100):
        await asyncio.sleep(0.05)
        recs = o.load_steps(task.run_id) if task.run_id else {}
        if recs.get("long") and recs["long"].state == "running":
            break
    await asyncio.sleep(0.5)
    t0 = time.monotonic()
    await o.stop(task.task_id)
    left = subprocess.run(
        ["pgrep", "-f", "time.sleep(60)"], capture_output=True, text=True
    ).stdout.split()
    results["P4-D cancellation"] = evidence(
        o,
        task,
        {"cancel_latency_s": round(time.monotonic() - t0, 2), "sleep_processes_left": len(left)},
    )

    note.auto_approve = False
    wf = {
        "id": "demo.approval",
        "steps": [
            {"id": "prepare", "executor": "tool", "ref": "workspace.list_files"},
            {
                "id": "checkpoint",
                "executor": "approval",
                "depends_on": ["prepare"],
                "inputs": {"message": "Create release notes?"},
            },
            {
                "id": "write",
                "executor": "tool",
                "ref": "workspace.create_file",
                "depends_on": ["checkpoint"],
                "inputs": {"path": "RELEASE_NOTES.md", "content": "# Release\n"},
            },
        ],
    }
    task = await o.submit_request(TaskRequest(objective="approval demo", workflow=wf))
    pending = []
    for _ in range(200):
        await asyncio.sleep(0.05)
        pending = o.pending_approvals(task.task_id)
        if pending:
            break
    ws = o._worktrees.get(task.task_id)
    before = bool(ws and (ws.path / "RELEASE_NOTES.md").exists())
    state_while_waiting = task.state.value
    o.decide_approval(pending[0]["approval_id"], True, "demo-user")
    await wait(o, task)
    ws = o._worktrees.get(task.task_id)
    results["P4-E approval"] = evidence(
        o,
        task,
        {
            "state_while_waiting": state_while_waiting,
            "file_existed_before_approval": before,
            "file_exists_after": bool(ws and (ws.path / "RELEASE_NOTES.md").exists()),
            "approval_record": {
                k: v
                for k, v in o.store.get("approval", pending[0]["approval_id"]).items()
                if k in ("state", "decided_by", "step_id")
            },
            "main_checkout_untouched": not (repo / "RELEASE_NOTES.md").exists(),
        },
    )
    note.auto_approve = True

    rec_dir = tmp / "recovery"
    rec_dir.mkdir()
    env = {**os.environ, "MARKER_DIR": str(rec_dir), "PYTHONPATH": str(ROOT)}
    p1 = subprocess.run(
        [PY, "tests/fixtures/recovery_driver.py", "crash", str(tmp / "rvar")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**env, "SLOW_S": "30"},
        timeout=120,
    )
    p2 = subprocess.run(
        [PY, "tests/fixtures/recovery_driver.py", "recover", str(tmp / "rvar")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**env, "SLOW_S": "0"},
        timeout=120,
    )
    rec = json.loads([ln for ln in p2.stdout.splitlines() if ln.startswith("{")][-1])
    results["P4-F recovery"] = {
        "crash_exit_code": p1.returncode,
        "recover_exit_code": p2.returncode,
        "tasks_after_restart": rec["tasks"],
        "recovery_actions": rec["recovery"],
        "side_effect_log": (rec_dir / "side.log").read_text(),
        "idempotent_runs": (rec_dir / "idem.log").read_text().count("x"),
    }

    t = await submit(
        o,
        objective="Add a subtract function with a test",
        domain="software",
        task_type="implement_reviewed",
    )
    results["P4-G multi-agent"] = evidence(
        o,
        t,
        {
            "agent_capability": o.caps.get("runtime:claude_code").model_dump(
                include={"status", "evidence"}
            )
        },
    )

    # ---------- Phase 5 (QA = only the real pytest check) ----------
    o5 = build(
        settings.model_copy(
            update={
                "data_dir": tmp / "var5",
                "qa_commands": {"pytest": settings.qa_commands["pytest"]},
            }
        )
    )
    o5.notifier = note
    await o5.start()
    note.orch, o = o5, o5
    files = {
        "mathx.py": "def subtract(a, b):\n    return a - b\n",
        "test_mathx.py": "from mathx import subtract\n\n\ndef test_subtract():\n    assert subtract(5, 3) == 2\n",
    }
    t = await submit(o, objective="add these files and run the tests", data={"files": files})
    results["P5-A software"] = evidence(
        o, t, {"main_checkout_untouched": not (repo / "mathx.py").exists()}
    )

    t = await submit(
        o,
        objective="extract facts from these documents into a report",
        attachment_ids=[
            att(DOC_A, "plan.md", "text/markdown"),
            att(DOC_B, "vendor.md", "text/markdown"),
            att(b"%PDF-1.4 x", "scan.pdf", "application/pdf"),
        ],
    )
    rep = o.artifacts.read(
        o.artifacts.get(o.load_steps(t.run_id)["build"].artifact_ids[0])
    ).decode()
    results["P5-B research/documents"] = evidence(o, t, {"report_excerpt": rep[:900]})

    t = await submit(
        o,
        objective="aggregate the sales by region",
        attachment_ids=[att(CSV, "sales.csv", "text/csv")],
        data={"group_by": ["region"], "metrics": {"amount": ["sum", "count", "mean"]}},
    )
    out = o.artifacts.read(
        o.artifacts.get(o.load_steps(t.run_id)["produce"].output["artifact_id"])
    ).decode()
    results["P5-C data"] = evidence(o, t, {"output_csv": out})

    t1 = await submit(
        o,
        objective="inspect this image",
        attachment_ids=[att(make_png(128, 64), "icon.png", "image/png")],
    )
    t2 = await submit(
        o,
        objective="generate a logo image of a gold coin",
        domain="media",
        task_type="generate",
        data={"prompt": "flat gold coin icon", "width": 1024, "height": 1024},
    )
    results["P5-D media"] = {"inspect": evidence(o, t1), "generate": evidence(o, t2)}

    t = await submit(
        o,
        objective="build a report from the document and the data",
        attachment_ids=[att(DOC_A, "plan.md", "text/markdown"), att(CSV, "sales.csv", "text/csv")],
    )
    results["P5-E cross-domain"] = evidence(o, t)

    unity = {}
    if unity_project and (Path(unity_project) / "ProjectSettings" / "ProjectVersion.txt").exists():
        u_settings = settings.model_copy(
            update={
                "workspace_root": Path(unity_project).resolve(),
                "data_dir": tmp / "uvar",
                "mcp_servers": settings.mcp_servers,
            }
        )
        uo = build(u_settings)
        uo.notifier = note
        await uo.start()
        t = await uo.submit_request(
            TaskRequest(objective="inspect the unity project packages", user_id="demo-user")
        )
        await wait(uo, t)
        ev = evidence(uo, t)
        info = uo.load_steps(t.run_id)["info"].output if t.run_id else {}
        ev["project"] = {
            "editor_version": info.get("editor_version"),
            "scenes": info.get("scenes"),
            "prefabs": info.get("prefabs"),
            "scripts": info.get("scripts"),
            "packages": len(info.get("packages", {})),
            "unity_mcp_package": info.get("unity_mcp_package"),
        }
        unity["inspect_project (read-only, real project)"] = ev
        t = await uo.submit_request(
            TaskRequest(objective="unity editor status", domain="unity", task_type="editor_status")
        )
        await wait(uo, t)
        unity["editor_status"] = evidence(uo, t)
    else:
        unity["skipped"] = "no --unity-project given"
    results["P5-F unity"] = unity
    results["notifications_sample"] = note.lines[:25]

    out_dir = ROOT / "docs" / "evidence"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "demo_results.json").write_text(json.dumps(results, indent=2, default=str))
    print(
        json.dumps(
            {
                k: (v.get("state") if isinstance(v, dict) and "state" in v else "see json")
                for k, v in results.items()
            },
            indent=1,
        )
    )
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--unity-project")
    a = ap.parse_args()
    asyncio.run(main(a.unity_project))
