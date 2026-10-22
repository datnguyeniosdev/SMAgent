"""fal.ai adapter against a mocked queue API, plus the config-driven design_image workflow."""

import asyncio
import json

import httpx
import pytest

from app.connectors.base import ConnectorError
from app.connectors.fal import FalImageProvider, _host_ok
from app.intake import TaskRequest
from app.models import TaskState
from tests.conftest import make_png


def fal_api(
    statuses=("IN_QUEUE", "IN_PROGRESS", "COMPLETED"),
    final=None,
    image_url="https://v3.fal.media/files/x.png",
    image_bytes=None,
    submit_status=200,
    response_status=200,
    schema=None,  # model input properties served at the public OpenAPI URL (None: 404)
):
    log = {"submit": None, "cancel": 0, "auth": set(), "polls": 0}
    seq = list(statuses)

    def h(req: httpx.Request):
        log["auth"].add(req.headers.get("authorization"))
        url = str(req.url)
        if req.method == "POST" and url.startswith("https://queue.fal.run/fal-ai/flux/schnell"):
            if submit_status != 200:
                return httpx.Response(submit_status, text="nope")
            log["submit"] = json.loads(req.content)
            base = "https://queue.fal.run/fal-ai/flux/requests/r1"
            return httpx.Response(
                200,
                json={
                    "request_id": "r1",
                    "status_url": base + "/status",
                    "response_url": base,
                    "cancel_url": base + "/cancel",
                    "queue_position": 0,
                },
            )
        if url.endswith("/status"):
            log["polls"] += 1
            s = seq.pop(0) if len(seq) > 1 else seq[0]
            body = {"status": s}
            if s == "COMPLETED":
                body.update(final or {"metrics": {"inference_time": 0.4}})
            return httpx.Response(200, json=body)
        if url.endswith("/cancel"):
            log["cancel"] += 1
            return httpx.Response(202, json={"status": "CANCELLATION_REQUESTED"})
        if url == "https://queue.fal.run/fal-ai/flux/requests/r1" and response_status != 200:
            return httpx.Response(response_status, json={
                "detail": "API key does not have access to model: fal-ai/flux/schnell",
                "type": "resource_restricted"})  # fmt: skip
        if url == "https://queue.fal.run/fal-ai/flux/requests/r1":
            return httpx.Response(
                200,
                json={
                    "images": [
                        {"url": image_url, "width": 64, "height": 32, "content_type": "image/png"}
                    ],
                    "seed": 7,
                    "has_nsfw_concepts": [False],
                },
            )
        if url.startswith("https://fal.ai/api/openapi/") and schema is not None:
            return httpx.Response(
                200, json={"components": {"schemas": {"ModelInput": {"properties": schema}}}}
            )
        if url == image_url:
            return httpx.Response(
                200, content=image_bytes if image_bytes is not None else make_png(64, 32)
            )
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(h)), log


async def test_queue_flow_returns_validated_image():
    client, log = fal_api()
    fal = FalImageProvider("k-123", client=client, poll_s=0)
    data = await fal.generate("gold coin icon", "64x32", transparent=False)
    assert data.startswith(b"\x89PNG") and log["polls"] == 3
    assert log["submit"] == {
        "prompt": "gold coin icon",
        "num_images": 1,
        "output_format": "png",
        "image_size": {"width": 64, "height": 32},
    }
    assert log["auth"] == {"Key k-123", None}  # key only on fal API calls, not on the CDN download
    assert fal.last["request_id"] == "r1" and fal.last["seed"] == 7


@pytest.mark.parametrize(
    "kw,match",
    [
        ({"final": {"error": "bad prompt", "error_type": "validation"}}, "generation failed"),
        ({"image_url": "https://evil.example.com/x.png"}, "untrusted host"),
        ({"image_url": "http://v3.fal.media/x.png"}, "untrusted host"),
        ({"image_bytes": b"<html>not an image</html>"}, "not a recognised image"),
        ({"submit_status": 401}, r"fal refused \(HTTP 401\): nope"),
    ],
)
async def test_failures_are_explicit(kw, match):
    client, _ = fal_api(**kw)
    with pytest.raises(ConnectorError, match=match):
        await FalImageProvider("k", client=client, poll_s=0).generate("p", "64x32", False)


async def test_timeout_and_cancellation_cancel_the_request():
    client, log = fal_api(statuses=("IN_PROGRESS",))
    fal = FalImageProvider("k", client=client, poll_s=0.01, timeout_s=0.05)
    with pytest.raises(ConnectorError) as e:
        await fal.generate("p", "64x32", False)
    assert e.value.outcome == "unknown" and log["cancel"] == 1
    client, log = fal_api(statuses=("IN_PROGRESS",))
    t = asyncio.create_task(
        FalImageProvider("k", client=client, poll_s=0.01).generate("p", "64x32", False)
    )
    await asyncio.sleep(0.05)
    t.cancel()
    await asyncio.gather(t, return_exceptions=True)
    assert log["cancel"] == 1


def test_host_allowlist():
    assert _host_ok("https://v3.fal.media/files/a.png") and _host_ok("https://fal.media/a.png")
    assert not _host_ok("https://fal.media.evil.com/a.png") and not _host_ok(
        "https://evilfal.media/a.png"
    )


async def test_unconfigured_provider():
    fal = FalImageProvider(None)
    assert (await fal.health_status())[0] == "unavailable"
    with pytest.raises(ConnectorError, match="FAL_KEY"):
        await fal.generate("p", "64x32", False)


async def test_design_image_workflow_end_to_end(settings, tmp_path):
    import shutil
    from pathlib import Path

    from app.gateway_config import load_config
    from tests.conftest import build_orch
    from tests.test_config_gateway import CHANNEL, gateway

    cfg_dir = tmp_path / "config"
    shutil.copytree(Path(__file__).parent.parent / "config", cfg_dir)
    settings.telegram_report_channel_id, settings.telegram_control_chat_id = CHANNEL, -100
    settings.telegram_allowed_user_ids = frozenset({11})
    client, log = fal_api()
    fal = FalImageProvider("k", client=client, poll_s=0)
    orch, _ = build_orch(settings, [], image=fal, gateway_cfg=load_config(cfg_dir))
    gw = gateway(orch)
    stop = asyncio.Event()

    async def approve():
        while not stop.is_set():
            for a in orch.pending_approvals():
                orch.decide_approval(a["approval_id"], True, "11")
            await asyncio.sleep(0.01)

    ap = asyncio.create_task(approve())
    t = await orch.submit_request(
        TaskRequest(
            objective="coin icon",
            task_type="design_image",
            data={"prompt": "flat gold coin icon", "width": 64, "height": 32, "transparent": False},
        )
    )
    from tests.conftest import wait_done

    await wait_done(orch, t)
    stop.set()
    await ap
    assert t.state == TaskState.succeeded, t.error
    steps = orch.load_steps(t.run_id)
    assert steps["generate"].executor_ref == "capability:design.image_generate→tool:media.generate"
    assert steps["generate"].approved_by == "11"  # paid generation required approval
    assert steps["validate"].output["status"] == "passed"
    final = orch.collab.reports_for(t.task_id)[-1]
    assert (
        len(final.artifact_ids) == 1 and final.telegram.photo_message_ids
    )  # image posted with the final message
    assert [k for k, *_ in gw.bot.sent][-2:] == ["message", "photo"]
    bad = await orch.submit_request(
        TaskRequest(objective="x", task_type="design_image", data={"prompt": "x"})
    )
    await wait_done(orch, bad)
    assert bad.state == TaskState.failed and "invalid input for workflow design.image" in bad.error


async def test_agents_generate_art_via_mcp(settings, cfg_dir):
    from pathlib import Path

    from mcp.client import Client

    from app.gateway_config import load_config
    from app.mcp_server import build_mcp_server
    from tests.conftest import build_orch, wait_done

    client, _ = fal_api()
    orch, _ = build_orch(settings, [], image=FalImageProvider("k", client=client, poll_s=0),
                         gateway_cfg=load_config(cfg_dir))  # fmt: skip
    stop = asyncio.Event()

    async def approve():  # the operator taps Approve on Telegram
        while not stop.is_set():
            for a in orch.pending_approvals():
                orch.decide_approval(a["approval_id"], True, "11")
            await asyncio.sleep(0.01)

    ap = asyncio.create_task(approve())
    async with Client(build_mcp_server(orch)) as c:
        vague = await c.call_tool(
            "generate_art", {"prompt": "coin", "purpose": "", "requirements": "nice"}
        )
        assert vague.is_error and "describe the expected output" in vague.content[0].text
        r = (await c.call_tool("generate_art", {
            "prompt": "flat gold coin icon", "purpose": "in-game reward sprite shown at 64 px",
            "requirements": "flat vector, gold #F5C542, dark outline, solid #1B2A49 background, no text",
            "width": 64, "height": 32, "project": "2048 Shooter (K05)"})).structured_content  # fmt: skip

        task = orch.tasks[r["task_id"]]
        await wait_done(orch, task)
        stop.set()
        await ap
        assert task.state == TaskState.succeeded, task.error
        res = (await c.call_tool("get_task_result", {"task_id": task.task_id})).structured_content
        assert (
            res["review"]["state"] == "pending" and res["images"] == []
        )  # not usable before review
        await orch.apply_action(
            action(orch, "art_accept", task.task_id), "11"
        )  # ✅ Accept on Telegram
        res = (await c.call_tool("get_task_result", {"task_id": task.task_id})).structured_content
        assert res["review"]["state"] == "accepted" and res["review"]["by"] == "11"
        img = res["images"][0]
        assert (
            Path(img["path"]).read_bytes().startswith(b"\x89PNG") and img["validation"] == "passed"
        )
        assert orch.project_of(task) == "2048 Shooter (K05)"  # labels the Telegram messages
        brief = task.request["data"]["prompt"]
        assert (
            "Purpose: in-game reward sprite" in brief
            and "Output requirements: flat vector" in brief
        )
        assert "Canvas: 64x32 px" in brief
        approval = orch.store.list("approval", task_id=task.task_id)[0]
        assert (
            "Brief" in approval["text"] and "flat vector" in approval["text"]
        )  # shown before paying
        bad = await c.call_tool("generate_art", {"prompt": "coin", "purpose": "icon",
                                                 "requirements": "flat vector, no text", "width": 8, "height": 8})  # fmt: skip
        assert bad.is_error  # below the workflow's 16 px minimum


async def test_generate_art_without_provider_says_so(settings):
    from mcp.client import Client

    from app.mcp_server import build_mcp_server
    from tests.conftest import build_orch

    orch, _ = build_orch(settings, [])
    async with Client(build_mcp_server(orch)) as c:
        r = await c.call_tool("generate_art", {"prompt": "coin", "purpose": "icon",
                                               "requirements": "flat vector, no text"})  # fmt: skip
        assert r.is_error and "FAL_KEY" in r.content[0].text


def test_dotenv_keys_reach_providers(tmp_path, monkeypatch, cfg_dir):
    import os

    from app.config import get_settings

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("FAL_KEY=fal-test-key\n")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("CONFIG_DIR", str(cfg_dir))
    monkeypatch.setenv("FAL_KEY", "")
    monkeypatch.delenv("FAL_KEY")  # recorded, so the value .env sets is removed again afterwards
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    get_settings.cache_clear()
    from app.main import build

    models = cfg_dir / "providers" / "models.yaml"
    try:
        assert build(get_settings()).image_provider is None  # fal is off in the shipped config
        models.write_text(models.read_text().replace("enabled: false ", "enabled: true "))
        orch = build(get_settings())
        assert isinstance(orch.image_provider, FalImageProvider) and orch.image_provider.available
    finally:
        os.environ.pop("FAL_KEY", None)
        get_settings.cache_clear()


async def test_restricted_key_says_why_and_that_the_job_ran():
    """The real failure seen live: submit accepted, result refused for a model-restricted key."""
    client, _ = fal_api(response_status=403)
    with pytest.raises(ConnectorError) as e:
        await FalImageProvider("k", client=client, poll_s=0).generate("p", "64x32", False)
    msg = str(e.value)
    assert "generated request r1 but did not return it" in msg
    assert "does not have access to model: fal-ai/flux/schnell" in msg
    assert e.value.outcome == "unknown" and e.value.category == "auth"


NANO = {  # the relevant part of fal-ai/nano-banana-pro's input schema
    "prompt": {"type": "string"},
    "aspect_ratio": {
        "anyOf": [
            {
                "enum": [
                    "auto",
                    "21:9",
                    "16:9",
                    "3:2",
                    "4:3",
                    "5:4",
                    "1:1",
                    "4:5",
                    "3:4",
                    "2:3",
                    "9:16",
                ],
                "type": "string",
            },
            {"type": "null"},
        ]
    },
    "resolution": {"type": "string", "enum": ["1K", "2K", "4K"]},
}


def test_size_input_follows_the_model():
    from app.connectors.fal import size_input

    assert size_input({}, 64, 32) == {
        "image_size": {"width": 64, "height": 32}
    }  # unknown: exact size
    assert size_input({"image_size": {}}, 64, 32) == {"image_size": {"width": 64, "height": 32}}
    assert size_input(NANO, 512, 512) == {"aspect_ratio": "1:1", "resolution": "1K"}
    assert size_input(NANO, 1920, 1080) == {"aspect_ratio": "16:9", "resolution": "2K"}
    assert size_input(NANO, 1080, 1920) == {"aspect_ratio": "9:16", "resolution": "2K"}
    assert size_input(NANO, 6000, 3000) == {
        "aspect_ratio": "16:9",
        "resolution": "4K",
    }  # 2:1 -> nearest, max res


async def test_aspect_only_model_request_and_flag():
    client, log = fal_api(schema=NANO)
    fal = FalImageProvider("k", model="fal-ai/flux/schnell", client=client, poll_s=0)
    await fal.generate("coin", "512x512", False)
    assert log["submit"]["aspect_ratio"] == "1:1" and log["submit"]["resolution"] == "1K"
    assert "image_size" not in log["submit"] and fal.exact_size is False


async def test_aspect_check_for_models_without_exact_size():
    from types import SimpleNamespace

    from app.plugins.media import MediaPlugin

    gate = MediaPlugin().gate_image_valid
    ctx = SimpleNamespace(read_artifact=lambda aid: make_png(1024, 1024))
    ok = await gate(ctx, {"images": [{"artifact_id": "a", "exact_size": False, "width": 1024, "height": 1024}],
                          "rules": {"width": 512, "height": 512}})  # fmt: skip
    assert ok.status == "passed"  # 1024x1024 for a 512x512 request: same aspect, accepted
    ctx = SimpleNamespace(read_artifact=lambda aid: make_png(1024, 576))
    bad = await gate(ctx, {"images": [{"artifact_id": "a", "exact_size": False, "width": 1024, "height": 576}],
                           "rules": {"width": 512, "height": 512}})  # fmt: skip
    assert (
        bad.status == "failed"
        and "does not match the requested 512x512" in bad.findings[0]["message"]
    )
    exact = await gate(ctx, {"images": [{"artifact_id": "a", "width": 1024, "height": 576}],
                             "rules": {"width": 512, "height": 512}})  # fmt: skip
    assert exact.status == "failed"  # exact-size models are still held to the exact size


def action(orch, kind, task_id):
    return next(
        t for t, a in orch.actions._actions.items() if a.kind == kind and a.task_id == task_id
    )


async def test_art_feedback_regenerates_without_a_second_cost_approval(settings, cfg_dir):
    from app.gateway_config import load_config
    from tests.conftest import build_orch, wait_done

    client, log = fal_api()
    orch, _ = build_orch(settings, [], image=FalImageProvider("k", client=client, poll_s=0),
                         gateway_cfg=load_config(cfg_dir))  # fmt: skip
    first = await orch.submit_request(TaskRequest(objective="coin", task_type="design_image", data={
        "prompt": "gold coin\n\nPurpose: icon", "width": 64, "height": 32, "transparent": False}))  # fmt: skip
    await wait_done(orch, first)
    report = orch.collab.reports_for(first.task_id)[-1]
    assert "what to change" in str(
        await orch.apply_action(action(orch, "art_feedback", first.task_id), "11")
    )

    fb = await orch.collab.submit_feedback(text="make the outline thicker", user_id="11", source="telegram",
                                           report_id=report.report_id)  # fmt: skip
    assert fb.route_id == "art_revision"
    second = orch.tasks[fb.follow_up_task_id]
    await wait_done(orch, second)
    gen = lambda t: orch.load_steps(t.run_id)["generate"].approved_by  # noqa: E731
    assert second.state == TaskState.succeeded and gen(first) == "1"  # the operator's Approve tap
    assert (
        gen(second) == "operator (asked for this revision on Telegram)"
    )  # no second cost approval
    assert (
        "make the outline thicker" in log["submit"]["prompt"]
        and "gold coin" in log["submit"]["prompt"]
    )
    assert orch.art_review(first.task_id) == {**orch.art_review(first.task_id), "state": "changes_requested",
                                              "next_task_id": second.task_id}  # fmt: skip
    assert orch.art_review(second.task_id)["state"] == "pending"  # the new version is reviewed too


def test_reports_on_telegram_are_summaries():
    from app.formatting import summary_only

    text = "- Buttons realigned\n```\n$ dotnet test\nPassed 69\n```\n2026-10-21 23:00:01 INFO build ok\n- Tests: 69/69"
    assert summary_only(text) == "- Buttons realigned\n\n- Tests: 69/69"
    assert len(summary_only("x" * 5000)) == 700
