"""Hosted model APIs over httpx.

ChatCompletionsProvider speaks the Chat Completions wire format, which both
OpenAI and DeepSeek (https://api.deepseek.com/chat/completions) document.
OpenAIImageProvider calls POST /v1/images/generations (b64_json response).
Neither has been exercised against a live key in this environment.
"""

import asyncio
import base64
import json

import httpx

from app.connectors.base import (
    AgentRequest,
    AgentResult,
    Cap,
    ConnectorError,
    UnsupportedCapability,
)
from app.logs import redact

_TEXT_FILE_LIMIT = 200_000
_sleep = asyncio.sleep  # patched in tests


async def post_json(
    client: httpx.AsyncClient, url: str, payload: dict, api_key: str, retries: int = 3
) -> dict:
    """POST with bounded retries on 429/5xx/timeouts. Never logs the key."""
    for attempt in range(retries + 1):
        try:
            resp = await client.post(
                url, json=payload, headers={"Authorization": f"Bearer {api_key}"}
            )
        except httpx.TimeoutException as e:
            if attempt == retries:
                raise ConnectorError("request timed out", "timeout", outcome="unknown") from e
            await _sleep(2**attempt)
            continue
        except httpx.HTTPError as e:
            raise ConnectorError(f"network error: {type(e).__name__}", "network") from e
        if resp.status_code in (401, 403):
            raise ConnectorError(f"authentication rejected ({resp.status_code})", "auth")
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == retries:
                cat = "rate_limit" if resp.status_code == 429 else "provider"
                raise ConnectorError(f"HTTP {resp.status_code} after retries", cat, retryable=True)
            try:
                delay = float(resp.headers.get("retry-after", 2**attempt))
            except ValueError:
                delay = 2**attempt
            await _sleep(min(delay, 60))
            continue
        if resp.status_code >= 400:
            raise ConnectorError(f"HTTP {resp.status_code}: {redact(resp.text[:300])}", "provider")
        return resp.json()
    raise AssertionError("unreachable")


class ChatCompletionsProvider:
    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str | None,
        model: str | None,
        caps: frozenset[Cap],
        json_schema_mode: bool,
        timeout_s: float = 120,
        client: httpx.AsyncClient | None = None,
    ):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.caps = caps
        self.json_schema_mode = json_schema_mode  # False -> json_object + schema in prompt
        self.available = bool(api_key and model)
        self.verification = "mocked"
        self.client = client or httpx.AsyncClient(timeout=timeout_s)

    def build_payload(self, req: AgentRequest) -> dict:
        parts: list[dict] = [{"type": "text", "text": req.prompt}]
        for att, data in req.attachments:
            if att.is_image:
                if Cap.image_input not in self.caps:
                    raise UnsupportedCapability(f"{self.name} does not accept image input")
                b64 = base64.b64encode(data).decode()
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{att.mime_type};base64,{b64}"},
                    }
                )
            elif att.mime_type.startswith("text/") or att.mime_type == "application/json":
                if len(data) > _TEXT_FILE_LIMIT:
                    raise UnsupportedCapability(f"{att.filename} too large to inline as text")
                parts.append(
                    {"type": "text", "text": f"--- {att.filename} ---\n{data.decode('utf-8')}"}
                )
            else:
                raise UnsupportedCapability(f"{self.name} cannot accept {att.mime_type} files")
        messages = []
        system = req.system or ""
        payload: dict = {"model": self.model}
        if req.json_schema:
            if self.json_schema_mode:
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "result", "schema": req.json_schema},
                }
            else:
                payload["response_format"] = {"type": "json_object"}
                system += (
                    f"\nRespond only with JSON matching this schema:\n{json.dumps(req.json_schema)}"
                )
        if system.strip():
            messages.append({"role": "system", "content": system.strip()})
        messages.append({"role": "user", "content": parts})
        payload["messages"] = messages
        if req.tools:
            if Cap.tool_calling not in self.caps:
                raise UnsupportedCapability(f"{self.name} does not support tool calling")
            payload["tools"] = req.tools
        return payload

    async def execute(self, req: AgentRequest) -> AgentResult:
        if not self.available:
            raise ConnectorError(f"{self.name} is not configured", "unavailable")
        if req.resume_session:
            raise UnsupportedCapability(f"{self.name} has no server-side sessions")
        if not req.read_only:
            raise UnsupportedCapability(f"{self.name} cannot write to a workspace")
        data = await post_json(
            self.client, f"{self.base_url}/chat/completions", self.build_payload(req), self.api_key
        )
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as e:
            raise ConnectorError("malformed completion response", "provider") from e
        text = msg.get("content") or ""
        structured = None
        if req.json_schema:
            try:
                structured = json.loads(text)
            except json.JSONDecodeError as e:
                raise ConnectorError("model returned invalid JSON", "provider") from e
        usage = data.get("usage") or {}
        return AgentResult(
            text=text,
            provider=self.name,
            model=data.get("model", self.model),
            structured=structured,
            tool_calls=msg.get("tool_calls") or [],
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
        )

    async def cancel(self, task_id: str) -> bool:
        return False  # no server-side cancel; the orchestrator aborts the client request


class OpenAIImageProvider:
    name = "openai_images"
    verification = "mocked"

    def __init__(self, api_key: str, model: str, base_url: str, client: httpx.AsyncClient):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.client = client

    async def generate(self, prompt: str, size: str, transparent: bool) -> bytes:
        payload = {"model": self.model, "prompt": prompt, "size": size, "output_format": "png"}
        if transparent:
            payload["background"] = "transparent"
        data = await post_json(
            self.client, f"{self.base_url}/images/generations", payload, self.api_key
        )
        try:
            return base64.b64decode(data["data"][0]["b64_json"])
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise ConnectorError("malformed image response", "provider") from e
