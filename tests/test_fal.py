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
        ({"submit_status": 401}, "rejected the API key"),
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
