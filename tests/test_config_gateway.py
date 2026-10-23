"""Config-driven gateway: validation, registry, routing, Telegram feedback loop, visual feedback,
handoff, MCP server facade and the UC-01..UC-09 use cases. Providers/runtimes are fakes;
everything else (engine, store, tools, Telegram handler code, MCP protocol) is real."""

import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.dispatcher.event.bases import SkipHandler
from mcp import Client

from app.agent_registry import CapabilityError
from app.collaboration import build_implementation_prompt
from app.connectors.base import AgentResult, Cap, ConnectorError
from app.gateway_config import load_config
from app.intake import TaskRequest
from app.mcp_server import TOOL_NAMES, build_mcp_server
from app.models import TaskState
from app.telegram_bot import TelegramGateway
from tests.conftest import FakeConnector, build_orch, make_png, wait_done

ROOT = Path(__file__).parent.parent
CONTROL, CHANNEL, DISCUSSION, USER, STRANGER = -100, -200, -300, 11, 99
DESIGN = {
    "summary": "Move the score panel to the top-right and enlarge buttons.",
    "issues": [
        {
            "element": "ScorePanel",
            "problem": "overlaps the play button",
            "fix": "anchor top-right, 24px margin",
        }
    ],
    "layout": {"ScorePanel": {"anchor": "top-right"}},
    "acceptance_criteria": [
        "No UI element overlaps at 1920x1080",
        "Buttons are at least 120px wide",
    ],
}
VISION = {Cap.text, Cap.image_input, Cap.structured_output}


class Provider(FakeConnector):
    kind = "provider"


class Runtime(FakeConnector):
    kind = "runtime"


@pytest.fixture
def env(monkeypatch):
    for k in ("GEMINI_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.setenv(k, "test-key-not-real")


def gemini(seen=None, result=DESIGN, fail_first=None):
    calls = []

    def h(req):
        calls.append(req)
        if seen is not None:
            seen.append(req)
        if fail_first and len(calls) <= fail_first[0]:
            raise fail_first[1]
        return AgentResult("", "gemini", "gemini-test", structured=result)

    return Provider("gemini", VISION, h)


def make(settings, cfg_dir, connectors, **kw):
    settings.telegram_control_chat_id, settings.telegram_report_channel_id = CONTROL, CHANNEL
    settings.telegram_allowed_user_ids = frozenset({USER})
    settings.telegram_discussion_chat_id = DISCUSSION
    cfg = load_config(cfg_dir)
    assert not cfg.errors, cfg.errors
    orch, notes = build_orch(settings, connectors, gateway_cfg=cfg, **kw)
    return orch, notes


class Bot:
    """Fake Bot API: records sends and returns increasing message ids."""

    def __init__(self):
        self.sent, self.n = [], 1000

    async def _send(self, kind, **kw):
        self.n += 1
        self.sent.append((kind, kw, self.n))
        return SimpleNamespace(message_id=self.n)

    async def send_message(self, **kw):
        return await self._send("message", **kw)

    async def send_photo(self, **kw):
        return await self._send("photo", **kw)

    async def send_document(self, **kw):
        return await self._send("document", **kw)


def gateway(orch):
    gw = TelegramGateway(orch.s, orch, orch.attachments, orch.actions, bot=Bot())
    orch.notifier = gw
    return gw


async def auto_approve(orch, stop):
    while not stop.is_set():
        for a in orch.pending_approvals():
            try:
                orch.decide_approval(a["approval_id"], True, str(USER))
            except Exception:
                pass
        await asyncio.sleep(0.01)


def reply_msg(text, reply_to, user=USER, chat=DISCUSSION, update_id=1, mid=5000):
    m = SimpleNamespace(
        text=text,
        caption=None,
        chat=SimpleNamespace(id=chat),
        message_id=mid,
        from_user=SimpleNamespace(id=user),
        reply_to_message=reply_to,
        is_automatic_forward=False,
        forward_origin=None,
        replies=[],
    )

    async def reply(t):
        m.replies.append(t)

    m.reply = reply
    return m, {"event_update": SimpleNamespace(update_id=update_id)}


def auto_forward(channel_mid, discussion_mid):
    return SimpleNamespace(
        text="report",
        caption=None,
        chat=SimpleNamespace(id=DISCUSSION),
        message_id=discussion_mid,
        from_user=None,
        reply_to_message=None,
        is_automatic_forward=True,
        forward_origin=SimpleNamespace(chat=SimpleNamespace(id=CHANNEL), message_id=channel_mid),
    )


async def run(orch, req, timeout=20):
    t = await orch.submit_request(req)
    await wait_done(orch, t, timeout)
    return t


def steps(orch, t):
    return orch.load_steps(t.run_id) if t.run_id else {}


# ======================================================== configuration


def test_example_config_validates():
    cfg = load_config(ROOT / "config")
    assert cfg.errors == []
    assert {"assistant", "designer", "implementer", "coordinator", "researcher"} <= set(cfg.agents)
    assert len(cfg.agents["designer"].capabilities) == 4  # one agent, several capabilities
    assert len(cfg.capabilities["design.layout_analysis"].executors) == 2  # several executors
    assert cfg.workflows["visual_feedback"].version == "1"


@pytest.mark.parametrize(
    "file,content,expect",
    [
        (
            "agents/x.yaml",
            "schema_version: 1\nagents:\n  - {id: assistant, capabilities: [general.answer]}\n",
            "duplicate agent id 'assistant'",
        ),
        (
            "agents/x.yaml",
            "schema_version: 1\nagents:\n  - {id: ghost, capabilities: [nope.cap]}\n",
            "unknown capability 'nope.cap'",
        ),
        ("agents/x.yaml", "schema_version: 2\nagents: []\n", "schema_version must be 1"),
        (
            "providers/x.yaml",
            "schema_version: 1\nproviders:\n  - {id: p2, type: gemini, api_key_env: K, api_key: sk-abcdefghijklmnopqrstu, capabilities: [model.text]}\n",
            "secrets must come from environment",
        ),
        (
            "mcp_servers/x.yaml",
            "schema_version: 1\nmcp_servers:\n  - {name: evil, url: 'http://169.254.169.254/mcp'}\n",
            "SSRF guard",
        ),
        (
            "workflows/x.yaml",
            "schema_version: 1\nworkflows:\n  - {id: cyc, steps: [{id: a, executor: internal, ref: select, depends_on: [b]}, {id: b, executor: internal, ref: select, depends_on: [a]}]}\n",
            "dependency cycle",
        ),
        (
            "workflows/x.yaml",
            "schema_version: 1\nworkflows:\n  - {id: w2, steps: [{id: a, executor: capability, ref: general.answer, agent: researcher}]}\n",
            "does not declare capability",
        ),
        (
            "capabilities/x.yaml",
            "schema_version: 1\ncapabilities:\n  - {id: c2, executors: [{type: provider, ref: nowhere}]}\n",
            "unknown provider 'nowhere'",
        ),
    ],
)
def test_config_errors_are_diagnosed(cfg_dir, file, content, expect):
    (cfg_dir / file).write_text(content)
    cfg = load_config(cfg_dir)
    assert any(expect in d.message for d in cfg.errors), [str(d) for d in cfg.diagnostics]


def test_routing_rejects_unknown_workflow_and_dynamic_planning(cfg_dir):
    p = cfg_dir / "routing" / "routing.yaml"
    p.write_text(p.read_text().replace("workflow: unity.ui_task", "workflow: nope"))
    assert any("unknown workflow 'nope'" in d.message for d in load_config(cfg_dir).errors)
    p.write_text(
        p.read_text().replace(
            "dynamic_planning: {enabled: false}", "dynamic_planning: {enabled: true}"
        )
    )
    assert any("dynamic_planning" in d.message for d in load_config(cfg_dir).errors)


def test_validate_cli(cfg_dir, capsys):
    from app.gateway_config import main

    assert main([str(cfg_dir)]) == 0 and "OK" in capsys.readouterr().out
    (cfg_dir / "agents" / "bad.yaml").write_text(
        "schema_version: 1\nagents:\n  - {id: Bad Id, capabilities: []}\n"
    )
    assert main([str(cfg_dir)]) == 1 and "INVALID" in capsys.readouterr().out


# ======================================================== registry and router


async def test_executor_selection_priority_preference_and_errors(
    settings, cfg_dir, env, monkeypatch
):
    orch, _ = make(settings, cfg_dir, [gemini(), Provider("openai", VISION | {Cap.tool_calling})])
    reg = orch.registry
    assert reg.select("general.answer").executor_id == "provider:openai"  # priority 10
    assert (
        reg.select("design.layout_analysis", "designer").executor_id == "provider:gemini"
    )  # agent preference
    assert (
        reg.select("general.answer", exclude={"provider:openai"}).executor_id == "provider:gemini"
    )
    for args, code in [
        (("nope",), "unknown_capability"),
        (("general.answer", "ghost"), "unknown_agent"),
        (("general.answer", "researcher"), "unknown_capability"),
        (("research.web_search",), "mcp_disconnected"),
    ]:
        with pytest.raises(CapabilityError) as e:
            reg.select(*args)
        assert e.value.code == code
    monkeypatch.delenv("GEMINI_API_KEY")
    with pytest.raises(CapabilityError) as e:
        reg.select("design.layout_analysis", "designer", exclude={"provider:openai"})
    assert (
        e.value.code == "missing_credentials" and "GEMINI_API_KEY is not set" in e.value.details[0]
    )
    orch.gateway_cfg.agents["designer"].enabled = False
    with pytest.raises(CapabilityError) as e:
        reg.select("design.layout_analysis", "designer")
    assert e.value.code == "agent_disabled"


async def test_unconfigured_capabilities_reported_honestly(settings, cfg_dir):
    orch, _ = make(settings, cfg_dir, [])
    st = {c.id: c.status.value for c in orch.caps.list()}
    assert st["capability:design.layout_analysis"] == "unavailable"
    assert st["capability:prompt.implementation"] == "validated"
    assert st["agent:designer"] == "unavailable" and st["agent:coordinator"] == "validated"
    assert st["agent:researcher"] == "degraded"  # web search has no executor
    assert (
        "GEMINI_API_KEY is not set" in orch.caps.get("capability:design.layout_analysis").evidence
    )


async def test_uc02_config_only_agent_and_workflow(settings, cfg_dir, env):
    (cfg_dir / "agents" / "reviewer.yaml").write_text(
        "schema_version: 1\nagents:\n  - id: reviewer\n    name: Reviewer\n"
        "    capabilities: [general.answer, document.extract, report.publish]\n"
    )
    (cfg_dir / "workflows" / "review.yaml").write_text(
        "schema_version: 1\nworkflows:\n  - id: review.quick\n    version: '3'\n    steps:\n"
        "      - {id: answer, executor: capability, ref: general.answer, agent: reviewer, inputs: {prompt: $task.objective}}\n"
    )
    routing = cfg_dir / "routing" / "routing.yaml"
    routing.write_text(
        routing.read_text().replace(
            "  routes:\n",
            "  routes:\n    - {id: quick-review, task_type: quick_review, workflow: review.quick}\n",
        )
    )
    orch, _ = make(
        settings,
        cfg_dir,
        [Provider("openai", VISION, lambda r: AgentResult("looks good", "openai", "m"))],
    )
    assert orch.caps.get("agent:reviewer").status.value == "validated"
    assert orch.registry.agents_for("document.extract") == ["researcher", "reviewer"]
    t = await run(orch, TaskRequest(objective="review this", task_type="quick_review"))
    assert (
        t.state == TaskState.succeeded and steps(orch, t)["answer"].output["text"] == "looks good"
    )
    run_rec = orch.load_run(t.run_id)
    assert (
        run_rec.workflow.id == "review.quick" and run_rec.workflow.version == "3"
    )  # version persisted with the run


# ======================================================== UC-01, UC-03, UC-09 and the MCP facade


async def test_uc01_general_task_via_mcp_and_default_workflow(settings, cfg_dir, env):
    orch, _ = make(
        settings, cfg_dir, [Provider("openai", VISION, lambda r: AgentResult("42", "openai", "m"))]
    )
    async with Client(build_mcp_server(orch)) as c:
        assert sorted(t.name for t in (await c.list_tools()).tools) == sorted(TOOL_NAMES)
        r = (
            await c.call_tool("delegate_task", {"objective": "what is the answer?"})
        ).structured_content
        task = orch.tasks[r["task_id"]]
        await wait_done(orch, task)
        st = (await c.call_tool("get_task_status", {"task_id": task.task_id})).structured_content
        assert (
            st["state"] == "succeeded"
            and st["workflow"] == "general.answer"
            and st["workflow_version"] == "1"
        )
        res = (await c.call_tool("get_task_result", {"task_id": task.task_id})).structured_content
        assert res["routing"]["mode"] == "config_default" and res["reports"][0]["text"] == "42"
        caps = (await c.call_tool("list_capabilities", {})).structured_content["result"]
        assert {"id": "general.answer", "status": "operational"} == {
            k: v
            for k, v in next(x for x in caps if x["id"] == "general.answer").items()
            if k in ("id", "status")
        }
        det = (
            await c.call_tool("get_capability_details", {"capability_id": "research.web_search"})
        ).structured_content
        assert (
            det["executors"][0]["eligible"] is False
            and "not registered" in det["executors"][0]["reason"]
        )
        bad = await c.call_tool("get_task_status", {"task_id": "tsk_nope"})
        assert bad.is_error


async def test_uc03_report_with_screenshots_published_and_unverified(settings, cfg_dir):
    orch, _ = make(settings, cfg_dir, [])
    gw = gateway(orch)
    t = await run(orch, TaskRequest(objective="build the pause menu", task_type="unity_ui"))
    async with Client(build_mcp_server(orch)) as c:
        res = (await c.call_tool("get_task_result", {"task_id": t.task_id})).structured_content
        h = res["handoffs"][0]
        assert h["mode"] == "return_to_caller" and h["status"] == "pending_manual_continuation"
        assert h["resumed_session"] is False  # never claims a session resumed
        img = base64.b64encode(make_png(64, 32)).decode()
        rep = (
            await c.call_tool(
                "submit_report",
                {
                    "task_id": t.task_id,
                    "text": "Pause menu done",
                    "agent_status": "completed",
                    "images": [{"name": "pause.png", "data_base64": img}],
                    "handoff_id": h["handoff_id"],
                },
            )
        ).structured_content
        # Agent says "completed"; the gateway has no gate evidence and the handoff was pending -> unverified.
        assert rep["verified_status"] == "unverified"
        assert rep["pending_actions"] == ["agent reports completion; gateway has not verified it"]
        bad = await c.call_tool(
            "submit_report",
            {
                "task_id": t.task_id,
                "text": "x",
                "images": [{"name": "a.png", "data_base64": "%%%"}],
            },
        )
        assert bad.is_error
    stored = orch.collab.get_report(rep["report_id"])
    assert stored.agent_status == "completed" and stored.artifact_validation == {
        rep["artifact_ids"][0]: "passed"
    }
    assert (
        stored.telegram.channel_chat_id == CHANNEL and stored.telegram.photo_message_ids
    )  # published with ids
    assert [k for k, *_ in gw.bot.sent][-2:] == ["message", "photo"]
    assert (
        orch.collab.handoffs_for(t.task_id)[0].status == "succeeded"
    )  # caller completed the handoff


async def test_external_report_without_task_and_feedback_readback(settings, cfg_dir, env):
    orch, _ = make(settings, cfg_dir, [])
    gateway(orch)
    async with Client(build_mcp_server(orch)) as c:
        assert (await c.call_tool("submit_report", {"text": "no anchor"})).is_error
        rep = (
            await c.call_tool(
                "submit_report",
                {"project": "2048", "text": "Added pause menu", "agent_status": "completed"},
            )
        ).structured_content
        task = orch.tasks[rep["task_id"]]
        assert task.state.value == "succeeded" and task.title == "Added pause menu"
        assert orch.collab.get_report(rep["report_id"]).telegram.channel_message_id
        assert orch.project_of(task) == "2048"
        fb = await orch.collab.submit_feedback(
            text="also add a sound toggle", user_id="11", source="api", report_id=rep["report_id"]
        )
        # an agent's own report: the reply waits for that project's agent (no gateway follow-up task)
        assert fb.route_id == "channel" and fb.follow_up_task_id is None and fb.delivered is False
        res = (await c.call_tool("get_task_result", {"task_id": task.task_id})).structured_content
        assert [f["text"] for f in res["feedback"]] == ["also add a sound toggle"]


# ======================================================== UC-04/05: feedback -> Gemini -> prompt -> handoff


async def submit_ui_report(orch, gw, name="ui.png", objective="Build the main menu UI"):
    t = await run(
        orch,
        TaskRequest(
            objective=objective,
            task_type="unity_ui",
            constraints=["Keep the existing prefab names"],
        ),
    )
    rep = await orch.collab.submit_report(
        t.task_id,
        "Main menu implemented",
        "claude_code",
        "completed",
        images=[(name, make_png(80, 40))],
    )
    return t, orch.collab.get_report(rep.report_id)


async def test_uc04_uc05_visual_feedback_end_to_end(settings, cfg_dir, env):
    seen = []
    runtime = Runtime(
        "claude_code",
        {Cap.text, Cap.workspace_write},
        lambda r: (_ for _ in ()).throw(
            ConnectorError("Failed to authenticate. API Error: 401", "auth")
        ),
    )
    orch, _ = make(settings, cfg_dir, [gemini(seen), runtime])
    gw = gateway(orch)
    stop = asyncio.Event()
    approver = asyncio.create_task(auto_approve(orch, stop))
    t, report = await submit_ui_report(orch, gw)
    # The channel post is auto-forwarded into the discussion group; the user replies to that copy.
    await gw.on_discussion(auto_forward(report.telegram.channel_message_id, 7001))
    assert orch.collab.get_report(report.report_id).telegram.discussion_message_id == 7001
    fwd_copy = SimpleNamespace(
        chat=SimpleNamespace(id=DISCUSSION),
        message_id=7001,
        forward_origin=SimpleNamespace(
            chat=SimpleNamespace(id=CHANNEL), message_id=report.telegram.channel_message_id
        ),
    )
    text = "The layout is not correct. Use Gemini to redesign the layout, then create a prompt for Claude to implement the changes in the existing Unity project."
    m, kw = reply_msg(text, fwd_copy)
    await gw.on_discussion(m, **kw)
    assert m.replies[0].startswith("💬 <b>FEEDBACK</b>") and "started (visual)" in m.replies[0]
    fb = [f for f in orch.store.list("feedback")][0]
    assert (
        fb["task_id"] == t.task_id
        and fb["report_id"] == report.report_id
        and fb["telegram_update_id"] == 1
    )
    follow = orch.tasks[fb["follow_up_task_id"]]
    await wait_done(orch, follow, 30)
    stop.set()
    await approver
    assert follow.state == TaskState.succeeded, follow.error
    st = steps(orch, follow)
    # Gemini got the real screenshot bytes and the feedback text
    assert (
        seen[0].attachments[0][0].mime_type == "image/png"
        and "layout is not correct" in seen[0].prompt
    )
    assert st["design"].executor_ref == "capability:design.layout_analysis→provider:gemini"
    prompt = st["prompt"].output["prompt"]
    assert (
        "Keep the existing prefab names" in prompt
        and text in prompt
        and "NOT yet implemented" in prompt
    )
    assert "anchor top-right, 24px margin" in prompt and report.artifact_ids[0] in prompt
    # runtime failed (401) -> fallback to return_to_caller, honestly reported
    ho = st["handoff"].output
    assert ho["mode"] == "return_to_caller" and ho["status"] == "pending_manual_continuation"
    assert [a["executor"] for a in ho["_executor_attempts"]][:1] == [
        "runtime:claude_code.new_execution"
    ]
    assert orch.caps.get("runtime:claude_code").status.value == "degraded"
    pub = orch.collab.get_report(st["publish"].output["report_id"])
    assert (
        pub.task_id == follow.task_id
        and pub.text == DESIGN["summary"]
        and pub.telegram.channel_message_id
    )
    assert follow.request["parent_task_id"] == t.task_id


async def test_handoff_new_execution_success_reports_session_without_resume(settings, cfg_dir, env):
    runtime = Runtime(
        "claude_code",
        {Cap.text, Cap.workspace_write},
        lambda r: AgentResult("Changed MainMenu.prefab", "claude_code", "m", session_id="sess-9"),
    )
    orch, _ = make(settings, cfg_dir, [gemini(), runtime])
    gw = gateway(orch)
    stop = asyncio.Event()
    approver = asyncio.create_task(auto_approve(orch, stop))
    t, report = await submit_ui_report(orch, gw)
    fb = await orch.collab.submit_feedback(
        text="fix the layout spacing", user_id="11", source="api", report_id=report.report_id
    )
    follow = orch.tasks[fb.follow_up_task_id]
    await wait_done(orch, follow, 30)
    stop.set()
    await approver
    h = orch.collab.handoffs_for(follow.task_id)[0]
    assert (
        h.status == "succeeded"
        and h.mode == "new_execution"
        and h.session_ref == "sess-9"
        and h.resumed_session is False
    )
    assert (
        steps(orch, follow)["handoff"].output["verified"] is False
    )  # agent summary is not verification
    assert (
        runtime.calls[-1].read_only is False
        and "Implementation request" in runtime.calls[-1].prompt
    )


async def test_feedback_correlation_ambiguity_auth_and_duplicates(settings, cfg_dir, env):
    orch, _ = make(settings, cfg_dir, [gemini()])
    gw = gateway(orch)
    t, report = await submit_ui_report(orch, gw)
    unknown = SimpleNamespace(
        chat=SimpleNamespace(id=DISCUSSION), message_id=4242, forward_origin=None
    )
    m, kw = reply_msg("this is wrong", unknown, update_id=10)
    await gw.on_discussion(m, **kw)
    assert (
        "can't tell which task" in m.replies[0] and orch.store.list("feedback") == []
    )  # never guessed
    m, kw = reply_msg(
        "layout wrong",
        SimpleNamespace(
            chat=SimpleNamespace(id=CHANNEL),
            message_id=report.telegram.channel_message_id,
            forward_origin=None,
        ),
        user=STRANGER,
        update_id=11,
    )
    await gw.on_discussion(m, **kw)
    assert m.replies == [] and orch.store.list("feedback") == []  # unauthorized: ignored
    photo_reply = SimpleNamespace(
        chat=SimpleNamespace(id=CHANNEL),
        message_id=report.telegram.photo_message_ids[0],
        forward_origin=None,
    )
    for _ in range(2):  # Telegram may deliver the same update twice
        m, kw = reply_msg("thanks, noted", photo_reply, update_id=12)
        await gw.on_discussion(m, **kw)
    fbs = orch.store.list("feedback")
    assert (
        len(fbs) == 1
        and fbs[0]["report_id"] == report.report_id
        and fbs[0]["route_id"] == "acknowledge"
    )
    m, kw = reply_msg("/feedback tsk_0000000000 text", None, update_id=13)
    await gw.on_discussion(m, **kw)
    assert "unknown task" in m.replies[0]


async def test_uc09_concurrent_tasks_do_not_cross_associate(settings, cfg_dir, env):
    seen = []
    orch, _ = make(settings, cfg_dir, [gemini(seen)])
    gw = gateway(orch)
    (ta, ra), (tb, rb) = await asyncio.gather(
        submit_ui_report(orch, gw, "a.png", "Menu A"), submit_ui_report(orch, gw, "b.png", "Menu B")
    )
    fa, fb_ = await asyncio.gather(
        orch.collab.submit_feedback(
            text="layout of A is off", user_id="11", source="api", report_id=ra.report_id
        ),
        orch.collab.submit_feedback(
            text="layout of B is off", user_id="11", source="api", report_id=rb.report_id
        ),
    )
    for f in (fa, fb_):
        await wait_done(orch, orch.tasks[f.follow_up_task_id], 30)
    ctx = {
        f.task_id: steps(orch, orch.tasks[f.follow_up_task_id])["context"].output for f in (fa, fb_)
    }
    assert (
        ctx[ta.task_id]["objective"] == "Menu A"
        and ctx[ta.task_id]["screenshots"] == ra.artifact_ids
    )
    assert (
        ctx[tb.task_id]["objective"] == "Menu B"
        and ctx[tb.task_id]["screenshots"] == rb.artifact_ids
    )
    assert {s.prompt.split("Previous report")[0].strip() for s in seen} == {
        "Objective: Menu A",
        "Objective: Menu B",
    }
    # a follow-up of A cannot read B's screenshot or load B's context
    follow_a = orch.tasks[fa.follow_up_task_id]
    assert not orch.artifact_ok(follow_a, rb.artifact_ids[0])
    with pytest.raises(Exception, match="unrelated task"):
        orch.collab.load_context(tb.task_id, None, None, follow_a.task_id)


# ======================================================== UC-06, UC-07, UC-08


async def test_uc06_research_document_workflow(settings, cfg_dir):
    orch, _ = make(settings, cfg_dir, [])
    gateway(orch)
    doc = orch.attachments.save(
        b"# Notes\n\nBudget: 10 USD\nGrowth was 5% in May.\n## Risks\n- none\n",
        "notes.md",
        "text/markdown",
        "api",
    )
    t = await run(
        orch,
        TaskRequest(
            objective="research report on the notes",
            task_type="research_report",
            attachment_ids=[doc.attachment_id],
            data={"document_ref": doc.attachment_id},
        ),
    )
    assert t.state == TaskState.succeeded, t.error
    st = steps(orch, t)
    assert st["extract"].executor_ref == "capability:document.extract→tool:documents.extract"
    assert st["validate"].output["status"] == "passed"
    rep = orch.collab.get_report(st["publish"].output["report_id"])
    assert rep.artifact_ids == st["build"].artifact_ids and rep.telegram.channel_message_id


async def test_uc07_provider_failure_retry_and_fallback(settings, cfg_dir, env):
    # transient timeout on the first call is retried by the capability retry policy
    flaky = gemini(fail_first=(1, ConnectorError("timed out", "timeout", retryable=True)))
    orch, _ = make(settings, cfg_dir, [flaky, Runtime("claude_code", {Cap.workspace_write})])
    gateway(orch)
    stop = asyncio.Event()
    approver = asyncio.create_task(auto_approve(orch, stop))
    t, report = await submit_ui_report(orch, orch.notifier)
    fb = await orch.collab.submit_feedback(
        text="redesign the layout", user_id="11", source="api", report_id=report.report_id
    )
    follow = orch.tasks[fb.follow_up_task_id]
    await wait_done(orch, follow, 30)
    assert follow.state == TaskState.succeeded and len(flaky.calls) == 2
    # provider hard-failing (auth) -> falls back to the next eligible provider
    bad = gemini(fail_first=(99, ConnectorError("401", "auth")))
    good = Provider("openai", VISION, lambda r: AgentResult("", "openai", "m", structured=DESIGN))
    orch2, _ = make(settings, cfg_dir, [bad, good, Runtime("claude_code", {Cap.workspace_write})])
    gateway(orch2)
    approver2 = asyncio.create_task(auto_approve(orch2, stop2 := asyncio.Event()))
    t2, r2 = await submit_ui_report(orch2, orch2.notifier)
    f2 = await orch2.collab.submit_feedback(
        text="layout broken", user_id="11", source="api", report_id=r2.report_id
    )
    fu = orch2.tasks[f2.follow_up_task_id]
    await wait_done(orch2, fu, 30)
    d = steps(orch2, fu)["design"].output
    assert fu.state == TaskState.succeeded and d["_executor"] == "provider:openai"
    assert (
        d["_executor_attempts"][0]["executor"] == "provider:gemini"
        and d["_executor_attempts"][0]["category"] == "auth"
    )
    # no provider at all: the follow-up fails at planning with an explicit reason
    orch3, _ = make(settings, cfg_dir, [])
    gateway(orch3)
    t3, r3 = await submit_ui_report(orch3, orch3.notifier)
    f3 = await orch3.collab.submit_feedback(
        text="layout broken", user_id="11", source="api", report_id=r3.report_id
    )
    fu3 = orch3.tasks[f3.follow_up_task_id]
    await wait_done(orch3, fu3)
    assert (
        fu3.state == TaskState.failed
        and fu3.error_category == "unsupported"
        and "design.layout_analysis" in fu3.error
    )
    stop.set()
    stop2.set()
    await asyncio.gather(approver, approver2)


async def test_uc08_approval_blocks_side_effect(settings, cfg_dir):
    settings.workspace_root = None
    orch, notes = make(settings, cfg_dir, [], auto=None)
    t = await orch.submit_request(
        TaskRequest(
            objective="write notes",
            task_type="write_file",
            data={"path": "NOTES.md", "content": "hello"},
        )
    )
    for _ in range(100):
        if orch.pending_approvals(t.task_id):
            break
        await asyncio.sleep(0.02)
    target = orch._scratch(t.task_id) / "NOTES.md"
    assert t.state == TaskState.waiting_for_approval and not target.exists()
    pending = orch.pending_approvals(t.task_id)[0]
    orch.decide_approval(pending["approval_id"], True, str(USER))
    with pytest.raises(Exception):
        orch.decide_approval(pending["approval_id"], True, str(USER))  # duplicate decision rejected
    await wait_done(orch, t)
    assert t.state == TaskState.succeeded and target.read_text() == "hello"
    t2 = await orch.submit_request(
        TaskRequest(
            objective="write again",
            task_type="write_file",
            data={"path": "OTHER.md", "content": "x"},
        )
    )
    for _ in range(100):
        if orch.pending_approvals(t2.task_id):
            break
        await asyncio.sleep(0.02)
    orch.decide_approval(orch.pending_approvals(t2.task_id)[0]["approval_id"], False, str(USER))
    await wait_done(orch, t2)
    assert t2.state == TaskState.cancelled and not (orch._scratch(t2.task_id) / "OTHER.md").exists()


async def test_expired_approval_marks_task_expired(settings, cfg_dir):
    settings.workspace_root = None
    settings.action_ttl_s = 0.2
    orch, _ = make(settings, cfg_dir, [], auto=None)
    t = await run(
        orch,
        TaskRequest(objective="w", task_type="write_file", data={"path": "A.md", "content": "x"}),
    )
    assert t.state == TaskState.expired and t.error_category == "expired"


# ======================================================== prompt builder, human input, REST


def test_prompt_builder_is_deterministic_and_does_not_invent_paths():
    args = dict(
        objective="Build menu",
        feedback="layout wrong",
        design_spec=DESIGN,
        constraints=["c1"],
        artifacts=[
            {"artifact_id": "art_b", "filename": "b.png", "kind": "screenshot"},
            {"artifact_id": "art_a", "filename": "a.png", "kind": "screenshot"},
        ],
        validation_requirements=["v1"],
        task_id="tsk_1",
    )
    a, b = (
        build_implementation_prompt(**args),
        build_implementation_prompt(**{**args, "artifacts": list(reversed(args["artifacts"]))}),
    )
    assert a == b and a["artifact_refs"] == ["art_b", "art_a"] or a["sha256"] == b["sha256"]
    p = a["prompt"]
    assert (
        p.index("art_a") < p.index("art_b")
        and "NOT yet implemented" in p
        and "- c1" in p
        and "- [ ] No UI element overlaps" in p
    )
    assert ".cs" not in p and "Assets/" not in p


async def test_human_input_request_answer_expiry(settings, cfg_dir):
    orch, _ = make(settings, cfg_dir, [])
    gw = gateway(orch)
    async with Client(build_mcp_server(orch)) as c:
        req = (
            await c.call_tool(
                "request_human_input", {"question": "Ship it?", "options": ["yes", "no"]}
            )
        ).structured_content
        assert (
            gw.bot.sent[-1][1]["chat_id"] == CONTROL
            and req["request_id"] in gw.bot.sent[-1][1]["text"]
        )
        assert (
            await c.call_tool("get_human_response", {"request_id": req["request_id"]})
        ).structured_content["status"] == "pending"
        m = SimpleNamespace(
            from_user=SimpleNamespace(id=USER), chat=SimpleNamespace(id=CONTROL), answers=[]
        )

        async def answer(t, **k):
            m.answers.append(t)

        m.answer = answer
        await gw.on_answer(m, SimpleNamespace(args=f"{req['request_id']} maybe"))
        assert "must be one of" in m.answers[-1]
        await gw.on_answer(m, SimpleNamespace(args=f"{req['request_id']} yes"))
        got = (
            await c.call_tool("get_human_response", {"request_id": req["request_id"]})
        ).structured_content
        assert got == {
            "request_id": req["request_id"],
            "status": "answered",
            "response": "yes",
            "responder": str(USER),
        }
        await gw.on_answer(m, SimpleNamespace(args=f"{req['request_id']} no"))
        assert "is answered" in m.answers[-1]
        short = (
            await c.call_tool("request_human_input", {"question": "q", "ttl_s": 0})
        ).structured_content
        await asyncio.sleep(0.01)
        assert (
            await c.call_tool("get_human_response", {"request_id": short["request_id"]})
        ).structured_content["status"] == "expired"


def test_rest_config_registry_reports_and_reload(tmp_path, monkeypatch, cfg_dir):
    from fastapi.testclient import TestClient

    from app.config import get_settings

    monkeypatch.setenv("API_TOKEN", "tok-123456")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("CONFIG_DIR", str(cfg_dir))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    from app.main import app

    h = {"Authorization": "Bearer tok-123456"}
    with TestClient(app) as c:
        assert c.get("/api/v1/config/validate", headers=h).json()["valid"] is True
        reg = c.get("/api/v1/agents/registry", headers=h).json()
        assert {a["id"] for a in reg["agents"]} >= {"designer", "implementer"}
        doc = c.post(
            "/api/v1/attachments",
            headers=h,
            files={"file": ("n.md", b"# N\n\nKey: v\n## Limits\n", "text/markdown")},
        ).json()
        t = c.post(
            "/api/v1/tasks",
            headers=h,
            json={
                "objective": "research",
                "task_type": "research_report",
                "attachment_ids": [doc["attachment_id"]],
                "data": {"document_ref": doc["attachment_id"]},
            },
        ).json()
        for _ in range(200):
            if c.get(f"/api/v1/tasks/{t['task_id']}", headers=h).json()["task"]["state"] in (
                "succeeded",
                "failed",
            ):
                break
        reps = c.get(f"/api/v1/tasks/{t['task_id']}/reports", headers=h).json()
        assert len(reps) == 2  # the workflow's report.publish step + the end-of-workflow report
        assert reps[-1]["verified_status"] == "verified"  # gate passed and nothing pending
        fb = c.post(
            f"/api/v1/reports/{reps[0]['report_id']}/feedback", headers=h, json={"text": "thanks"}
        ).json()
        assert fb["route_id"] == "acknowledge" and fb["follow_up_task_id"]
        for _ in range(200):
            ack = c.get(f"/api/v1/tasks/{fb['follow_up_task_id']}", headers=h).json()["task"]
            if ack["state"] == "succeeded":
                break
        ack_reps = c.get(f"/api/v1/tasks/{fb['follow_up_task_id']}/reports", headers=h).json()
        assert ack_reps == []  # acknowledged by the bot's threaded reply, no channel post
        assert c.post("/mcp", json={}).status_code == 401  # MCP endpoint is authenticated
        (cfg_dir / "agents" / "broken.yaml").write_text(
            "schema_version: 1\nagents:\n  - {id: x, capabilities: [missing]}\n"
        )
        r = c.post("/api/v1/config/reload", headers=h)
        assert r.status_code == 422 and "unknown capability" in str(r.json())
        (cfg_dir / "agents" / "broken.yaml").write_text(
            "schema_version: 1\nagents:\n  - {id: x, capabilities: [general.answer]}\n"
        )
        assert "x" in c.post("/api/v1/config/reload", headers=h).json()["agents"]
    get_settings.cache_clear()


async def test_artifact_retention_never_deletes_active_or_referenced(settings, cfg_dir):
    from datetime import timedelta

    orch, _ = make(settings, cfg_dir, [], auto=None)
    done = await run(orch, TaskRequest(objective="w", tool="workspace.list_files"))
    old = orch.artifacts.add(done.task_id, "report", "old.md", "x")
    keep_ref = orch.artifacts.add(done.task_id, "screenshot", "ref.png", make_png(4, 4))
    for a in (old, keep_ref):
        a.created_at -= timedelta(days=60)
    active = await orch.submit_request(
        TaskRequest(
            objective="needs input",
            artifact_ids=[keep_ref.artifact_id],
            task_type="write_file",
            data={"path": "Z.md", "content": "z"},
        )
    )
    for _ in range(100):
        if orch.pending_approvals(active.task_id):
            break
        await asyncio.sleep(0.02)
    assert not active.done, (active.state, active.error)
    res = orch.cleanup_artifacts(30)
    assert old.artifact_id in res["deleted"] and not old.path.exists()
    assert (
        keep_ref.artifact_id not in res["deleted"] and keep_ref.path.exists()
    )  # referenced by an active task
    await orch.stop(active.task_id)


async def test_report_deployment_posts_to_channel_and_records(settings, cfg_dir):
    orch, _ = make(settings, cfg_dir, [])
    gw = gateway(orch)
    async with Client(build_mcp_server(orch)) as c:
        r = (
            await c.call_tool(
                "report_deployment",
                {
                    "project": "2048 Shooter (K05)",
                    "environment": "uat",
                    "status": "succeeded",
                    "version": "1.0.0",
                    "build_number": "42",
                    "commit": "fbefa4d",
                    "platforms": ["android", "ios"],
                    "targets": ["Firebase App Distribution"],
                    "notes": "uat 1.0.0 <b>x</b>",
                },
            )
        ).structured_content
        assert r["delivered"] is True and r["channel_message_id"]
        kind, kw, mid = gw.bot.sent[-1]
        assert kw["chat_id"] == CHANNEL and mid == r["channel_message_id"]
        text = kw["text"]
        assert text.startswith("🚀 <b>DEPLOY</b> · #2048_Shooter_K05")  # prefix · project tag
        assert "<b>✅ uat · succeeded</b>" in text and "project <b>2048 Shooter (K05)</b>" in text
        assert "ref <code>dep_" in text and "&lt;b&gt;x&lt;/b&gt;" in text  # user text escaped
        bad = await c.call_tool(
            "report_deployment", {"project": "x", "environment": "prod", "status": "great"}
        )
        assert bad.is_error
    rec = orch.collab.deployments()[-1]
    assert rec.status == "succeeded" and rec.delivered and rec.source == "mcp"
    gw.bot.send_message = None  # delivery failure is reported, not hidden
    orch.notifier = SimpleNamespace(
        publish_channel=lambda html: (_ for _ in ()).throw(RuntimeError("down"))
    )
    d = await orch.collab.report_deployment(
        type(rec)(project="p", environment="prod", status="failed", error="build failed")
    )
    assert d.delivered is False and "down" in d.delivery_error


async def test_final_report_for_every_task_with_images(settings, cfg_dir):
    orch, _ = make(settings, cfg_dir, [])
    gw = gateway(orch)
    t = await run(orch, TaskRequest(objective="w", tool="workspace.list_files"))
    orch.artifacts.set_validation(
        orch.artifacts.add(t.task_id, "image", "bad.png", make_png(4, 4)).artifact_id, "failed"
    )
    good = orch.artifacts.add(t.task_id, "screenshot", "shot.png", make_png(8, 8))
    await orch._final_report(t)  # re-publish now that the task has images
    final = orch.collab.reports_for(t.task_id)[-1]
    assert final.artifact_ids == [good.artifact_id]  # failed-validation image excluded
    kinds = [(k, kw.get("reply_parameters")) for k, kw, _ in gw.bot.sent[-2:]]
    head = gw.bot.sent[-2][1]["text"]
    assert kinds[0][0] == "message" and head.startswith(f"✅ <b>DONE</b> · #project · #{t.task_id}")
    assert "session <code>" in head and "🖼 1" in head
    assert kinds[1][0] == "photo" and kinds[1][1].message_id == final.telegram.channel_message_id
    # failed tasks are reported too
    t2 = await run(orch, TaskRequest(objective="x", tool="nope.tool"))
    last = [kw["text"] for k, kw, _ in gw.bot.sent if k == "message"][-1]
    assert t2.state == TaskState.failed and last.startswith("❌ <b>FAILED</b>")
    ev = [e for e in orch.store.events(t2.task_id) if e["type"] == "task.final_report"]
    assert ev and ev[-1]["data"]["delivered"] is True


async def test_discussion_handler_skips_commands_when_shared_with_control(settings, cfg_dir):
    from aiogram.dispatcher.event.bases import SkipHandler

    orch, _ = make(settings, cfg_dir, [])
    gw = gateway(orch)
    m, kw = reply_msg("/status", None)
    with pytest.raises(SkipHandler):  # falls through to the /status command handler
        await gw.on_discussion(m, **kw)


def test_envelope_and_templates_prefill_ids():
    from app import formatting as fmt
    from app.telegram_bot import templates

    html = fmt.envelope(
        "report", "body", project="2048 Shooter (K05)", task_id="tsk_0123456789",
        title="Pause <menu>", session="ses_1", ref="rep_abcdef0123", reply="Reply to this post",
    )  # fmt: skip
    lines = html.split("\n")
    assert lines[0] == "📋 <b>REPORT</b> · #2048_Shooter_K05 · #tsk_0123456789"
    assert lines[1] == "<b>Pause &lt;menu&gt;</b>"
    assert "session <code>ses_1</code> · ref <code>rep_abcdef0123</code>" in lines[-2]
    t = templates(fmt.plain(html))
    assert (
        "<code>/report tsk_0123456789</code>" in t and "<code>/feedback rep_abcdef0123 </code>" in t
    )
    assert "📎 <b>With images</b>" in t and "<code>/feedback rep_abcdef0123 </code> — caption" in t
    assert "<code>/task </code>" in templates("") and "/answer hin_0123456789 " in templates(
        "QUESTION ref hin_0123456789"
    )


async def test_single_group_mode_report_and_reply(settings, cfg_dir):
    """Reports posted straight into the group (no channel): a reply there resolves to the report."""
    orch, _ = make(settings, cfg_dir, [])
    gw = gateway(orch)
    orch.s.telegram_report_channel_id = DISCUSSION
    t = orch.register_external("Pause menu", "mcp", "2048")
    rep = await orch.collab.submit_report(t.task_id, "Pause menu\nadded", "mcp-caller")
    assert (
        gw.bot.sent[-1][1]["chat_id"] == DISCUSSION and rep.telegram.channel_chat_id == DISCUSSION
    )
    target = SimpleNamespace(
        chat=SimpleNamespace(id=DISCUSSION), message_id=rep.telegram.channel_message_id,
        forward_origin=None,
    )  # fmt: skip
    m, kw = reply_msg("make the buttons bigger", target)
    await gw.on_discussion(m, **kw)
    assert [f.text for f in orch.collab.feedback_for(t.task_id)] == ["make the buttons bigger"]
    assert len(m.replies) == 1 and m.replies[0].startswith("💬 <b>FEEDBACK</b> · #2048")
    assert "Queued" in m.replies[0] and "ref <code>fb_" in m.replies[0]  # one threaded ack


def fmt_plain(html):
    from app import formatting as fmt

    return fmt.plain(html)


def photo_msg(mid, reply_to=None, caption=None, group=None, update_id=None):
    m, kw = reply_msg(caption, reply_to, mid=mid, update_id=update_id or mid)
    m.caption, m.text = caption, None
    m.photo = [SimpleNamespace(file_id=f"f{mid}", file_size=100)]
    m.document, m.media_group_id = None, group
    return m, kw


async def test_photo_reply_attaches_screenshots_to_feedback(settings, cfg_dir, monkeypatch):
    from app import telegram_bot

    monkeypatch.setattr(telegram_bot, "ALBUM_WAIT_S", 0)
    orch, _ = make(settings, cfg_dir, [])
    gw = gateway(orch)
    png = make_png(40, 20)

    async def download(file_id, destination):
        destination.write(png)
        return destination

    gw.bot.download = download
    orch.s.telegram_report_channel_id = DISCUSSION  # single-group mode
    t = orch.register_external("Menu", "mcp", "2048")
    rep = await orch.collab.submit_report(t.task_id, "Menu\ndone", "mcp-caller")  # no images
    target = SimpleNamespace(
        chat=SimpleNamespace(id=DISCUSSION), message_id=rep.telegram.channel_message_id,
        forward_origin=None,
    )  # fmt: skip
    # single photo, caption with a layout keyword -> visual route although the report had no images
    m, kw = photo_msg(7001, target, "fix the layout spacing")
    await gw.on_discussion(m, **kw)
    fb = orch.collab.feedback_for(t.task_id)[-1]
    assert len(fb.artifact_ids) == 1 and fb.route_id == "visual"
    follow = orch.tasks[fb.follow_up_task_id]
    assert (
        follow.request["artifact_ids"][0] == fb.artifact_ids[0]
    )  # screenshot reaches the follow-up
    assert orch.artifacts.get(fb.artifact_ids[0]).validation_status == "passed"
    assert "🖼 1 screenshot(s) attached" in m.replies[-1]
    await orch.stop(follow.task_id)
    # a reply to the bot's own FEEDBACK confirmation resolves through the ids in its envelope ...
    gw.bot.id = 4242
    confirm = SimpleNamespace(
        chat=SimpleNamespace(id=DISCUSSION), message_id=9001, forward_origin=None,
        from_user=SimpleNamespace(id=4242), text=fmt_plain(m.replies[-1]), caption=None,
    )  # fmt: skip
    m1, kw1 = photo_msg(7020, confirm, "and the title")
    await gw.on_discussion(m1, **kw1)
    assert orch.collab.feedback_for(t.task_id)[-1].text == "and the title"
    # ... but not when someone else wrote that text
    fake = SimpleNamespace(**{**confirm.__dict__, "from_user": SimpleNamespace(id=USER)})
    m2, kw2 = reply_msg("hijack", fake, mid=7021, update_id=7021)
    await gw.on_discussion(m2, **kw2)
    assert "can't tell which task" in m2.replies[-1]
    # "/feedback #project <text>" as a photo caption on a reply: no id, so the replied-to post decides
    m4, kw4 = photo_msg(7030, target, "/feedback #2048 tighter spacing")
    await gw.on_discussion(m4, **kw4)
    last = orch.collab.feedback_for(t.task_id)[-1]
    assert last.text == "tighter spacing" and last.report_id == rep.report_id and last.artifact_ids
    await orch.stop(last.follow_up_task_id)
    # album of two, caption only on the first part, sent as a reply -> one feedback with both images
    parts = [
        photo_msg(7002 + i, target, "looks off" if i == 0 else None, group="g1") for i in range(2)
    ]
    for p, _ in parts:
        with pytest.raises(SkipHandler):
            await gw.on_discussion(p, **_)
        await gw.on_album_part(p)
    await asyncio.sleep(0.05)
    fb2 = orch.collab.feedback_for(t.task_id)[-1]
    assert fb2.text == "looks off" and len(fb2.artifact_ids) == 2 and fb2.route_id == "channel"
    n_before = len(orch.collab.feedback_for(t.task_id))

    # an unreadable "photo" is rejected before anything is stored
    async def bad(file_id, destination):
        destination.write(b"not an image")
        return destination

    gw.bot.download = bad
    m3, kw3 = photo_msg(7010, target, "x")
    await gw.on_discussion(m3, **kw3)
    assert "not a readable" in m3.replies[-1]
    assert len(orch.collab.feedback_for(t.task_id)) == n_before


def test_parse_feedback_tolerates_tags():
    from app.telegram_bot import parse_feedback

    assert parse_feedback("/feedback rep_0123456789 make it blue") == (
        "rep_0123456789",
        "make it blue",
    )
    assert parse_feedback("/feedback #agent_gateway #tsk_0123456789 bigger") == (
        "tsk_0123456789",
        "bigger",
    )
    assert parse_feedback("/feedback #agent_gateway bigger buttons") == (None, "bigger buttons")


def test_webhook_endpoint_requires_secret_and_feeds_updates(tmp_path, monkeypatch, cfg_dir):
    from fastapi.testclient import TestClient

    from app.config import get_settings

    secret = "s" * 32
    monkeypatch.setenv("API_TOKEN", "tok-123456")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("CONFIG_DIR", str(cfg_dir))
    monkeypatch.setenv("TELEGRAM_MODE", "webhook")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", secret)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    from app.main import app

    fed = []

    class FakeGw:
        async def feed(self, data):
            fed.append(data["update_id"])

    with TestClient(app) as c:
        url = "/telegram/webhook"
        assert c.post(url, json={"update_id": 1}).status_code == 404  # Telegram disabled
        app.state.telegram = FakeGw()
        assert c.post(url, json={"update_id": 1}).status_code == 401  # no secret
        bad = {"X-Telegram-Bot-Api-Secret-Token": "wrong"}
        assert c.post(url, json={"update_id": 1}, headers=bad).status_code == 401
        good = {"X-Telegram-Bot-Api-Secret-Token": secret}
        assert c.post(url, json={"nope": 1}, headers=good).status_code == 400
        assert c.post(url, json={"update_id": 7}, headers=good).json() == {"ok": True}
    assert fed == [7]
    get_settings.cache_clear()


def test_webhook_mode_requires_a_valid_secret(monkeypatch):
    from app.config import Settings

    with pytest.raises(ValueError, match="TELEGRAM_WEBHOOK_SECRET"):
        Settings(_env_file=None, telegram_mode="webhook")
    with pytest.raises(ValueError, match="https"):
        Settings(_env_file=None, telegram_mode="webhook", telegram_webhook_secret="a" * 20,
                 telegram_webhook_url="http://x")  # fmt: skip
    assert (
        Settings(
            _env_file=None, telegram_mode="webhook", telegram_webhook_secret="a" * 20
        ).telegram_mode
        == "webhook"
    )


def test_mcp_oauth_probes_are_answered_quietly(tmp_path, monkeypatch, cfg_dir):
    import logging

    from fastapi.testclient import TestClient

    from app.config import get_settings

    monkeypatch.setenv("API_TOKEN", "tok-123456")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("CONFIG_DIR", str(cfg_dir))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    from app.main import _QuietProbes, app

    with TestClient(app) as c:
        for path in (
            "/.well-known/oauth-protected-resource/mcp",
            "/mcp/.well-known/openid-configuration",
        ):
            r = c.get(path)
            assert r.status_code == 404 and "no OAuth" in r.json()["detail"]  # not a 401 under /mcp
        assert c.post("/register", json={}).status_code == 404
        r = c.post("/mcp", json={})
        assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Bearer ")

    def rec(p):
        return logging.LogRecord(
            "uvicorn.access", 20, "", 0, '%s - "%s %s"', ("1", "GET", p, "1.1", 404), None
        )  # noqa: E731

    assert not _QuietProbes().filter(rec("/.well-known/oauth-authorization-server"))
    assert _QuietProbes().filter(rec("/api/v1/tasks"))
    get_settings.cache_clear()


def test_mcp_without_token_serves_only_local_requests(tmp_path, monkeypatch, cfg_dir):
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import _local_request

    monkeypatch.setenv("MCP_REQUIRE_TOKEN", "false")
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("CONFIG_DIR", str(cfg_dir))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    from app.main import app

    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "1"}}}  # fmt: skip
    accept = {"Accept": "application/json, text/event-stream"}
    with TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000)) as c:
        r = c.post("/mcp", json=init, headers=accept)  # no Authorization header
        assert r.status_code == 200 and "agent-gateway" in r.text
        foreign = c.post("/mcp", json=init, headers={**accept, "Origin": "https://evil.example"})
        assert foreign.status_code == 403
    with TestClient(app, base_url="http://evil.example") as c:  # DNS-rebinding style Host
        assert c.post("/mcp", json=init, headers=accept).status_code == 403
    assert not _local_request({"client": ("192.168.1.9", 1)}, {b"host": b"127.0.0.1:8000"})
    get_settings.cache_clear()
