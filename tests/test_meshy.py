"""Meshy 3D through generate_art(output="model3d"): provider against a mocked Meshy API, then the
whole flow (approval, preview review, model file) with the tool registered after start (/keys)."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.connectors.base import ConnectorError
from app.connectors.meshy import MeshyProvider
from app.models import TaskState
from tests.conftest import make_png

GLB = b"glTF\x02\x00\x00\x00fake-model"


def meshy_api(fail: str | None = None, status_code: int = 200):
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        path = req.url.path
        if req.method == "POST":
            if status_code != 200:
                return httpx.Response(status_code, json={"message": "Insufficient credits"})
            body = json.loads(req.content)
            kind = "img" if "image-to-3d" in path else body["mode"]
            return httpx.Response(202, json={"result": f"task-{kind}"})
        if req.url.host == "assets.meshy.ai":
            return httpx.Response(200, content=make_png(64, 64) if path.endswith(".png") else GLB)
        tid = path.rsplit("/", 1)[-1]
        polls = sum(1 for c in calls if c.url.path.endswith(tid))
        if fail:
            return httpx.Response(
                200, json={"id": tid, "status": "FAILED", "task_error": {"message": fail}}
            )
        if polls == 1:
            return httpx.Response(200, json={"id": tid, "status": "IN_PROGRESS", "progress": 40})
        return httpx.Response(200, json={
            "id": tid, "status": "SUCCEEDED", "consumed_credits": 30,
            "model_urls": {"glb": f"https://assets.meshy.ai/{tid}/model.glb"},
            "thumbnail_url": f"https://assets.meshy.ai/{tid}/preview.png"})  # fmt: skip

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), calls


async def test_text_preview_then_refine_and_image_to_3d():
    client, calls = meshy_api()
    m = MeshyProvider("msy-key", "meshy-6", client, poll_s=0)
    r = await m.generate_3d("a low-poly treasure chest, wooden with gold trim")
    assert (
        r["model"] == GLB
        and r["thumbnail"].startswith(b"\x89PNG")
        and r["task_id"] == "task-refine"
    )
    posts = [json.loads(c.content) for c in calls if c.method == "POST"]
    assert (
        posts[0]["mode"] == "preview"
        and posts[0]["ai_model"] == "meshy-6"
        and posts[0]["target_formats"] == ["glb"]
    )
    assert posts[1] == {"mode": "refine", "preview_task_id": "task-preview", "target_formats": ["glb"],
                        "texture_prompt": "a low-poly treasure chest, wooden with gold trim"}  # fmt: skip
    assert all(
        c.headers["authorization"] == "Bearer msy-key"
        for c in calls
        if c.url.host == "api.meshy.ai"
    )

    client, calls = meshy_api()
    r = await MeshyProvider("k", None, client, poll_s=0).generate_3d(
        "chest", make_png(8, 8), texture=False
    )
    body = json.loads(next(c.content for c in calls if c.method == "POST"))
    assert (
        body["image_url"].startswith("data:image/png;base64,") and body["should_texture"] is False
    )
    assert (
        body["ai_model"] == "latest" and "texture_prompt" not in body and r["task_id"] == "task-img"
    )


async def test_meshy_errors_are_explained():
    client, _ = meshy_api(fail="The prompt was flagged")
    with pytest.raises(ConnectorError, match="failed: The prompt was flagged"):
        await MeshyProvider("k", None, client, poll_s=0).generate_3d("chest", texture=False)
    client, _ = meshy_api(status_code=402)
    with pytest.raises(ConnectorError, match="402: Insufficient credits"):
        await MeshyProvider("k", None, client, poll_s=0).generate_3d("chest")
    with pytest.raises(ConnectorError, match="not configured"):
        await MeshyProvider(None).generate_3d("chest")


async def test_generate_art_model3d_flow(settings, cfg_dir, tmp_path):
    from mcp.client import Client

    from app.gateway_config import load_config
    from app.main import refresh_tools
    from app.mcp_server import build_mcp_server
    from tests.conftest import build_orch, wait_done
    from tests.test_fal import action

    orch, _ = build_orch(settings, [], gateway_cfg=load_config(cfg_dir))
    assert orch.tools.get("media.generate_3d") is None
    client, calls = meshy_api()
    orch.model3d_provider = MeshyProvider("k", None, client, poll_s=0)  # the key arrived via /keys
    refresh_tools(orch)
    assert orch.tools.get("media.generate_3d") is not None  # no restart needed
    ref = tmp_path / "chest.png"
    ref.write_bytes(make_png(32, 32))
    stop = asyncio.Event()

    async def approve():
        while not stop.is_set():
            for a in orch.pending_approvals():
                orch.decide_approval(a["approval_id"], True, "11")
            await asyncio.sleep(0.01)

    ap = asyncio.create_task(approve())
    async with Client(build_mcp_server(orch)) as c:
        r = (await c.call_tool("generate_art", {
            "prompt": "treasure chest", "purpose": "loot prop in a mobile game",
            "requirements": "stylized, wood and gold, low poly, no text", "output": "model3d",
            "image_path": str(ref), "project": "Coin Game"})).structured_content  # fmt: skip
        task = orch.tasks[r["task_id"]]
        await wait_done(orch, task)
        stop.set()
        await ap
        assert task.state == TaskState.succeeded, task.error
        assert any(
            "image-to-3d" in c.url.path for c in calls if c.method == "POST"
        )  # used the reference
        res = (await c.call_tool("get_task_result", {"task_id": task.task_id})).structured_content
        assert res["review"]["state"] == "pending" and res["models"] == []  # not before review
        assert orch._final_photos(task)  # the preview is what the operator reviews
        await orch.apply_action(action(orch, "art_accept", task.task_id), "11")
        res = (await c.call_tool("get_task_result", {"task_id": task.task_id})).structured_content
        assert Path(res["models"][0]["path"]).read_bytes() == GLB

        long = await c.call_tool("generate_art", {"prompt": "x" * 700, "purpose": "loot prop",
                                                  "requirements": "y" * 200, "output": "model3d"})  # fmt: skip
        assert long.is_error and "Meshy takes 800" in long.content[0].text
        orch.switches.set("generate_art", False)  # /mcp
        off = await c.call_tool("generate_art", {"prompt": "chest", "purpose": "prop",
                                                 "requirements": "low poly wood", "output": "model3d"})  # fmt: skip
        assert off.is_error


def test_meshy_and_fal_switch_apart(settings, cfg_dir):
    from types import SimpleNamespace

    from tests.test_config_gateway import make

    orch, _ = make(settings, cfg_dir, [])
    orch.image_provider, orch.model3d_provider = (
        SimpleNamespace(name="fal"),
        SimpleNamespace(name="meshy"),
    )
    orch.switches.set("art:images", False)  # fal off
    orch.switches.set("art:3d", True)  # Meshy on
    assert orch.model3d_provider is not None and orch.image_provider is None
    orch.switches.set("generate_art", False)  # the tool itself off: both refuse
    assert orch.model3d_provider is None and orch.image_provider is None
    orch.switches.set(
        "art:3d", True
    )  # turning Meshy on turns the tool on again (it was off in prod)
    assert orch.switches.on("generate_art") and orch.model3d_provider is not None


async def test_meshy_makes_images_when_fal_is_off():
    from app.connectors.meshy import MeshyImages

    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if req.method == "POST":
            return httpx.Response(202, json={"result": "img-1"})
        if req.url.host == "assets.meshy.ai":
            return httpx.Response(200, content=make_png(32, 32))
        return httpx.Response(200, json={"id": "img-1", "status": "SUCCEEDED",
                                         "image_urls": ["https://assets.meshy.ai/img-1/0.png"]})  # fmt: skip

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    images = MeshyImages(MeshyProvider("k", client=client, poll_s=0))
    png = await images.generate("a gold coin", "1920x1080", transparent=True)
    assert png.startswith(b"\x89PNG")
    body = json.loads(calls[0].content)
    assert calls[0].url.path == "/openapi/v1/text-to-image"
    assert body == {"ai_model": "nano-banana", "prompt": "a gold coin", "aspect_ratio": "16:9",
                    "remove_background": True}  # fmt: skip


def test_images_come_from_fal_then_meshy(settings, cfg_dir):
    from types import SimpleNamespace

    from app.connectors.meshy import MeshyImages
    from tests.test_config_gateway import make

    orch, _ = make(settings, cfg_dir, [])
    orch.image_provider = SimpleNamespace(name="fal")
    orch.model3d_provider = MeshyProvider("k")
    assert orch.image_provider.name == "fal"
    orch.switches.set("art:images", False)  # fal off: Meshy makes the images
    assert isinstance(orch.image_provider, MeshyImages)
    orch.switches.set("art:3d", False)
    assert orch.image_provider is None
