"""Meshy 3D generation (https://docs.meshy.ai, checked 2026-10-22). Paid: every task uses credits.

text  -> POST /openapi/v2/text-to-3d {mode: preview, prompt} (mesh), then, when textured,
         POST /openapi/v2/text-to-3d {mode: refine, preview_task_id, texture_prompt}
image -> POST /openapi/v1/image-to-3d {image_url: data URI, should_texture, texture_prompt}
each  -> GET <same path>/<id> until status SUCCEEDED | FAILED | CANCELED; then model_urls[<format>]
         and thumbnail_url are downloaded. Failed tasks are refunded by Meshy.
"""

from __future__ import annotations

import asyncio
import base64

import httpx

from app.connectors.base import ConnectorError

BASE = "https://api.meshy.ai"
FORMATS = ("glb", "fbx", "obj", "usdz", "stl")


class MeshyProvider:
    name = "meshy"
    verification = "mocked"  # implemented from the public docs; not run against a paid key here

    def __init__(self, api_key: str | None, model: str | None = None, client: httpx.AsyncClient | None = None,
                 timeout_s: float = 1200, poll_s: float = 5, base_url: str = BASE):  # fmt: skip
        self.api_key = api_key
        self.model = model or "latest"
        self.available = bool(api_key)
        self.client = client or httpx.AsyncClient(timeout=120)
        self.timeout_s, self.poll_s, self.base = timeout_s, poll_s, base_url.rstrip("/")

    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    async def health_status(self) -> tuple[str, str]:
        if not self.available:
            return "unavailable", "MESHY_API_KEY is not set"
        return "validated", f"MESHY_API_KEY set, model {self.model}; generation not yet proven"

    async def _post(self, path: str, body: dict) -> str:
        try:
            r = await self.client.post(self.base + path, json=body, headers=self._h())
        except httpx.HTTPError as e:
            raise ConnectorError(f"meshy: {e}", "timeout", retryable=True) from e
        if r.status_code >= 400:
            raise ConnectorError(f"meshy {r.status_code}: {_detail(r)}", _category(r.status_code))
        return r.json()["result"]

    async def _wait(self, path: str, task_id: str) -> dict:
        loop = asyncio.get_running_loop()
        end = loop.time() + self.timeout_s
        while True:
            try:
                r = await self.client.get(f"{self.base}{path}/{task_id}", headers=self._h())
                r.raise_for_status()
                task = r.json()
            except httpx.HTTPError as e:  # submitted: it may still finish and be charged
                raise ConnectorError(
                    f"meshy task {task_id}: {e}", "provider", outcome="unknown"
                ) from e
            status = task.get("status")
            if status == "SUCCEEDED":
                return task
            if status in ("FAILED", "CANCELED"):
                msg = (task.get("task_error") or {}).get("message") or status.lower()
                raise ConnectorError(f"meshy task {task_id} {status.lower()}: {msg}", "provider")
            if loop.time() > end:
                raise ConnectorError(f"meshy task {task_id} still {status} after {self.timeout_s:.0f}s",
                                     "timeout", outcome="unknown")  # fmt: skip
            await asyncio.sleep(self.poll_s)

    async def generate_3d(self, prompt: str, image: bytes | None = None, image_mime: str = "image/png",
                          texture: bool = True, fmt: str = "glb") -> dict:  # fmt: skip
        """-> {model: bytes, format, thumbnail: bytes | None, task_id, credits}."""
        if not self.available:
            raise ConnectorError("meshy is not configured (MESHY_API_KEY)", "unavailable")
        if fmt not in FORMATS:
            raise ConnectorError(f"format must be one of {', '.join(FORMATS)}", "unsupported")
        common = {"ai_model": self.model, "target_formats": [fmt]}
        if image is not None:
            path = "/openapi/v1/image-to-3d"
            uri = f"data:{image_mime};base64,{base64.b64encode(image).decode()}"
            body = {**common, "image_url": uri, "should_texture": texture}
            if texture and prompt:
                body["texture_prompt"] = prompt[:800]
            task = await self._wait(path, await self._post(path, body))
        else:
            path = "/openapi/v2/text-to-3d"
            task = await self._wait(
                path, await self._post(path, {**common, "mode": "preview", "prompt": prompt[:800]})
            )
            if texture:
                refine = {"mode": "refine", "preview_task_id": task["id"], "target_formats": [fmt],
                          "texture_prompt": prompt[:800]}  # fmt: skip
                task = await self._wait(path, await self._post(path, refine))
        url = (task.get("model_urls") or {}).get(fmt)
        if not url:
            raise ConnectorError(
                f"meshy task {task['id']} has no {fmt} file", "provider", outcome="unknown"
            )
        model = await self._download(url, task["id"])
        thumb = (
            await self._download(task["thumbnail_url"], task["id"])
            if task.get("thumbnail_url")
            else None
        )
        return {"model": model, "format": fmt, "thumbnail": thumb, "task_id": task["id"],
                "credits": task.get("consumed_credits")}  # fmt: skip

    async def _download(self, url: str, task_id: str) -> bytes:
        try:
            r = await self.client.get(url, timeout=300)
            r.raise_for_status()
            return r.content
        except httpx.HTTPError as e:  # generated and charged, but the file did not arrive
            raise ConnectorError(f"meshy task {task_id}: download failed ({e}); it is in your Meshy account",
                                 "provider", outcome="unknown") from e  # fmt: skip


RATIOS = {"1:1": 1.0, "16:9": 16 / 9, "9:16": 9 / 16, "4:3": 4 / 3, "3:4": 3 / 4}


class MeshyImages:
    """Meshy text-to-image as an image provider for generate_art (when fal is off):
    POST /openapi/v1/text-to-image {ai_model, prompt, aspect_ratio, remove_background} -> image_urls[0].
    Meshy returns its own size; the nearest supported aspect ratio is asked for."""

    name = "meshy"
    verification = "mocked"
    exact_size = False

    def __init__(self, meshy: MeshyProvider, model: str | None = None):
        self.meshy, self.model = meshy, model or "nano-banana"

    async def health_status(self) -> tuple[str, str]:
        return await self.meshy.health_status()

    async def generate(self, prompt: str, size: str, transparent: bool) -> bytes:
        if not self.meshy.available:
            raise ConnectorError("meshy is not configured (MESHY_API_KEY)", "unavailable")
        w, _, h = size.partition("x")
        want = int(w) / int(h) if w.isdigit() and h.isdigit() and int(h) else 1.0
        ratio = min(RATIOS, key=lambda k: abs(RATIOS[k] - want))
        path = "/openapi/v1/text-to-image"
        body = {"ai_model": self.model, "prompt": prompt, "aspect_ratio": ratio,
                "remove_background": transparent}  # fmt: skip
        task = await self.meshy._wait(path, await self.meshy._post(path, body))
        urls = task.get("image_urls") or []
        if not urls:
            raise ConnectorError(
                f"meshy task {task['id']} has no image", "provider", outcome="unknown"
            )
        return await self.meshy._download(urls[0], task["id"])


def _detail(r: httpx.Response) -> str:
    try:
        body = r.json()
        return str(body.get("message") or body.get("detail") or body)[:300]
    except ValueError:
        return r.text[:300]


def _category(status: int) -> str:
    return {401: "auth", 403: "auth", 402: "provider", 429: "rate_limit"}.get(status, "provider")
