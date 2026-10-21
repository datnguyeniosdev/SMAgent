"""fal.ai image/design provider over the documented queue HTTP API (no SDK).

From https://fal.ai/docs (queue + authentication pages, FLUX schnell API page):
  POST https://queue.fal.run/{model_id}   Authorization: Key $FAL_KEY   body: model input JSON
    -> {request_id, status_url, response_url, cancel_url, queue_position}
  GET  status_url   -> {status: IN_QUEUE | IN_PROGRESS | COMPLETED, error?, error_type?}
  GET  response_url -> model output, e.g. {images: [{url, width, height, content_type}], seed, ...}
  PUT  cancel_url   -> 200 OK | 202 CANCELLATION_REQUESTED | 400 ALREADY_COMPLETED
Status/result/cancel URLs are taken from the submit response (never rebuilt).

The generated image is downloaded only from https fal hosts and must be a recognised image;
dimensions/alpha are not assumed - the media gate validates them afterwards.
"""

from __future__ import annotations

import asyncio
import time
from urllib.parse import urlparse

import httpx

from app.assets import inspect_image
from app.connectors.base import ConnectorError

QUEUE = "https://queue.fal.run"
RESULT_HOSTS = ("fal.media", "fal.run", "fal.ai")  # allowed download hosts (and subdomains)


def _host_ok(url: str) -> bool:
    u = urlparse(url)
    host = (u.hostname or "").lower()
    return u.scheme == "https" and any(host == h or host.endswith("." + h) for h in RESULT_HOSTS)


class FalImageProvider:
    name = "fal"
    verification = "mocked"  # becomes evidence-backed after a real run (see capability registry)

    def __init__(
        self,
        api_key: str | None,
        model: str = "fal-ai/flux/schnell",
        timeout_s: float = 300,
        poll_s: float = 1.0,
        extra_input: dict | None = None,
        client: httpx.AsyncClient | None = None,
    ):
        self.api_key = api_key
        self.model = model
        self.timeout_s = timeout_s
        self.poll_s = poll_s
        self.extra_input = extra_input or {}
        self.available = bool(api_key and model)
        self.client = client or httpx.AsyncClient(timeout=60)
        self.last: dict = {}  # evidence of the last request (request id, timings) without secrets

    def _headers(self) -> dict:
        return {"Authorization": f"Key {self.api_key}", "Content-Type": "application/json"}

    async def _call(self, method: str, url: str, **kw) -> httpx.Response:
        try:
            r = await self.client.request(method, url, headers=self._headers(), **kw)
        except httpx.TimeoutException as e:
            raise ConnectorError("fal: request timed out", "timeout", retryable=True) from e
        except httpx.HTTPError as e:
            raise ConnectorError(
                f"fal: network error {type(e).__name__}", "network", retryable=True
            ) from e
        if r.status_code in (401, 403):
            raise ConnectorError(f"fal rejected the API key (HTTP {r.status_code})", "auth")
        if r.status_code == 429:
            raise ConnectorError("fal rate limit", "rate_limit", retryable=True)
        if r.status_code >= 400:
            raise ConnectorError(f"fal HTTP {r.status_code}: {r.text[:200]}", "provider")
        return r

    async def health_status(self) -> tuple[str, str]:
        if not self.available:
            return "unavailable", "FAL_KEY is not set"
        return "validated", f"FAL_KEY set, model {self.model}; generation not yet proven"

    async def generate(self, prompt: str, size: str, transparent: bool) -> bytes:
        if not self.available:
            raise ConnectorError("fal is not configured (FAL_KEY)", "unavailable")
        w, _, h = size.partition("x")
        body = {"prompt": prompt, "num_images": 1, "output_format": "png", **self.extra_input}
        if w.isdigit() and h.isdigit():
            body["image_size"] = {"width": int(w), "height": int(h)}
        sub = (await self._call("POST", f"{QUEUE}/{self.model}", json=body)).json()
        status_url, response_url, cancel_url = (
            sub.get("status_url"),
            sub.get("response_url"),
            sub.get("cancel_url"),
        )
        if not (status_url and response_url):
            raise ConnectorError("fal: submit response lacks status/response urls", "provider")
        self.last = {"request_id": sub.get("request_id"), "model": self.model}
        deadline = time.monotonic() + self.timeout_s
        try:
            while True:
                st = (await self._call("GET", status_url)).json()
                if st.get("status") == "COMPLETED":
                    if st.get("error"):
                        raise ConnectorError(f"fal generation failed: {st.get('error_type') or ''} {st['error']}"[:300],
                                             "provider")  # fmt: skip
                    self.last["inference_time"] = (st.get("metrics") or {}).get("inference_time")
                    break
                if time.monotonic() > deadline:
                    await self._cancel(cancel_url)
                    raise ConnectorError("fal: generation timed out", "timeout", outcome="unknown")
                await asyncio.sleep(self.poll_s)
        except asyncio.CancelledError:
            await self._cancel(cancel_url)
            raise
        out = (await self._call("GET", response_url)).json()
        images = out.get("images") or []
        if not images or not images[0].get("url"):
            raise ConnectorError("fal: result contains no image", "provider")
        url = images[0]["url"]
        if url.startswith("data:"):
            raise ConnectorError(
                "fal: data-URI results are not accepted (sync_mode off)", "provider"
            )
        if not _host_ok(url):
            raise ConnectorError(
                f"fal: refusing to download from untrusted host {urlparse(url).hostname}",
                "forbidden",
            )
        try:
            img = await self.client.get(url, timeout=60)
        except httpx.HTTPError as e:
            raise ConnectorError(
                f"fal: image download failed {type(e).__name__}", "network", retryable=True
            ) from e
        if img.status_code != 200 or len(img.content) > 50 * 1024 * 1024:
            raise ConnectorError(f"fal: image download HTTP {img.status_code}", "provider")
        if inspect_image(img.content).format == "unknown":
            raise ConnectorError(
                "fal: downloaded bytes are not a recognised image", "invalid_output"
            )
        self.last.update(width=images[0].get("width"), height=images[0].get("height"), seed=out.get("seed"),
                         nsfw=out.get("has_nsfw_concepts"))  # fmt: skip
        return img.content

    async def _cancel(self, cancel_url: str | None) -> None:
        if not cancel_url:
            return
        try:
            await self.client.put(cancel_url, headers=self._headers(), timeout=10)
        except httpx.HTTPError:
            pass
