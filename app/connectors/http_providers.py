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
    client: httpx.AsyncClient,
    url: str,
    payload: dict,
    api_key: str,
    retries: int = 3,
    headers: dict | None = None,
) -> dict:
    """POST with bounded retries on 429/5xx/timeouts. Never logs the key."""
    for attempt in range(retries + 1):
        try:
            resp = await client.post(
                url, json=payload, headers=headers or {"Authorization": f"Bearer {api_key}"}
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
        msg, data = await self.chat(self.build_payload(req))
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

    async def chat(self, payload: dict) -> tuple[dict, dict]:
        """One Chat Completions round trip. Returns (assistant message, raw response)."""
        if not self.available:
            raise ConnectorError(f"{self.name} is not configured", "unavailable")
        payload = {"model": self.model, **payload}
        data = await post_json(
            self.client, f"{self.base_url}/chat/completions", payload, self.api_key
        )
        try:
            return data["choices"][0]["message"], data
        except (KeyError, IndexError, TypeError) as e:
            raise ConnectorError("malformed completion response", "provider") from e

    async def health(self) -> tuple[bool, str]:
        """Free authenticated check: GET /models (no tokens consumed)."""
        if not self.available:
            return False, "not configured (missing API key or model)"
        try:
            resp = await self.client.get(
                f"{self.base_url}/models", headers={"Authorization": f"Bearer {self.api_key}"}
            )
        except httpx.HTTPError as e:
            return False, f"network error: {type(e).__name__}"
        if resp.status_code != 200:
            return False, f"GET /models -> HTTP {resp.status_code}"
        return True, "GET /models -> 200"

    async def health_status(self) -> tuple[str, str]:
        ok, detail = await self.health()
        if ok:
            return "validated", f"{detail} (credentials accepted; model call not yet proven)"
        return "unavailable", detail

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

    async def health_status(self) -> tuple[str, str]:
        try:
            resp = await self.client.get(
                f"{self.base_url}/models", headers={"Authorization": f"Bearer {self.api_key}"}
            )
        except httpx.HTTPError as e:
            return "unavailable", f"network error: {type(e).__name__}"
        if resp.status_code != 200:
            return "unavailable", f"GET /models -> HTTP {resp.status_code}"
        return "validated", "credentials accepted (GET /models 200); generation not yet proven"

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


class GeminiProvider:
    """Gemini API `models/{model}:generateContent` over REST (no SDK dependency).

    Implemented from the public API reference (contents/parts with inline_data images,
    generationConfig.responseMimeType, candidates[].content.parts[].text, usageMetadata).
    Not exercised against a live key in this environment.
    """

    verification = "mocked"

    def __init__(
        self,
        name: str,
        api_key: str | None,
        model: str | None,
        caps: frozenset[Cap],
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        timeout_s: float = 120,
        client: httpx.AsyncClient | None = None,
    ):
        self.name = name
        self.api_key = api_key
        self.model = model
        self.caps = caps
        self.base_url = base_url.rstrip("/")
        self.available = bool(api_key and model)
        self.client = client or httpx.AsyncClient(timeout=timeout_s)

    def _headers(self) -> dict:
        return {"x-goog-api-key": self.api_key or "", "Content-Type": "application/json"}

    def build_payload(self, req: AgentRequest) -> dict:
        prompt = req.prompt
        if req.json_schema:
            prompt += (
                "\n\nRespond only with JSON that validates against this JSON Schema:\n"
                + json.dumps(req.json_schema)
            )
        parts: list[dict] = [{"text": prompt}]
        for att, data in req.attachments:
            if att.is_image:
                if Cap.image_input not in self.caps:
                    raise UnsupportedCapability(f"{self.name} does not accept image input")
                parts.append(
                    {
                        "inline_data": {
                            "mime_type": att.mime_type,
                            "data": base64.b64encode(data).decode(),
                        }
                    }
                )
            elif att.mime_type.startswith("text/") or att.mime_type == "application/json":
                if len(data) > _TEXT_FILE_LIMIT:
                    raise UnsupportedCapability(f"{att.filename} too large to inline as text")
                parts.append({"text": f"--- {att.filename} ---\n{data.decode('utf-8')}"})
            else:
                raise UnsupportedCapability(f"{self.name} cannot accept {att.mime_type} files")
        payload: dict = {"contents": [{"role": "user", "parts": parts}]}
        if req.system:
            payload["systemInstruction"] = {"parts": [{"text": req.system}]}
        if req.json_schema:
            payload["generationConfig"] = {"responseMimeType": "application/json"}
        return payload

    async def execute(self, req: AgentRequest) -> AgentResult:
        if not self.available:
            raise ConnectorError(f"{self.name} is not configured", "unavailable")
        if req.resume_session or not req.read_only:
            raise UnsupportedCapability(
                f"{self.name} is a model API: no sessions or workspace writes"
            )
        if req.tools:
            raise UnsupportedCapability(
                f"{self.name}: tool calling is not wired for Gemini in this gateway"
            )
        data = await post_json(
            self.client,
            f"{self.base_url}/models/{self.model}:generateContent",
            self.build_payload(req),
            self.api_key or "",
            headers=self._headers(),
        )
        cands = data.get("candidates") or []
        if not cands:
            reason = (data.get("promptFeedback") or {}).get("blockReason", "no candidates")
            raise ConnectorError(f"gemini returned no candidates ({reason})", "provider")
        text = "".join(p.get("text", "") for p in (cands[0].get("content") or {}).get("parts", []))
        structured = None
        if req.json_schema:
            try:
                structured = json.loads(text)
            except json.JSONDecodeError as e:
                raise ConnectorError("model returned invalid JSON", "provider") from e
        usage = data.get("usageMetadata") or {}
        return AgentResult(
            text=text,
            provider=self.name,
            model=data.get("modelVersion", self.model),
            structured=structured,
            input_tokens=usage.get("promptTokenCount"),
            output_tokens=usage.get("candidatesTokenCount"),
        )

    async def health_status(self) -> tuple[str, str]:
        if not self.available:
            return "unavailable", "not configured (missing API key or model)"
        try:
            resp = await self.client.get(
                f"{self.base_url}/models/{self.model}", headers=self._headers()
            )
        except httpx.HTTPError as e:
            return "unavailable", f"network error: {type(e).__name__}"
        if resp.status_code != 200:
            return "unavailable", f"GET models/{self.model} -> HTTP {resp.status_code}"
        return "validated", "model metadata readable (key accepted); generation not yet proven"

    async def cancel(self, task_id: str) -> bool:
        return False
