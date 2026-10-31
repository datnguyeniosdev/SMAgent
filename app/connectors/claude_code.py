"""Claude Code CLI runtime via documented headless mode (`claude -p --output-format json`).

Verified against Claude Code 2.1.71 on this machine: JSON result schema
(type/subtype/is_error/result/session_id/total_cost_usd/usage). Note that an
auth failure is reported as subtype "success" with is_error=true, so is_error is
the authoritative failure signal.
"""

import asyncio
import json
import os
import signal

from app.config import child_env
from app.connectors.base import AgentRequest, AgentResult, Cap, ConnectorError
from app.logs import redact

_READ_ONLY_TOOLS = "Read,Grep,Glob"


class ClaudeCodeConnector:
    name = "claude_code"
    kind = "runtime"  # an agent runtime, not a model API
    caps = frozenset(
        {
            Cap.text,
            Cap.image_input,  # via Read tool on --add-dir paths (documented, not smoke-tested)
            Cap.file_input,
            Cap.structured_output,
            Cap.tool_calling,
            Cap.workspace_read,
            Cap.workspace_write,
            Cap.terminal,
            Cap.session_resume,
            Cap.cancel,
        }
    )
    verification = "partially_verified"

    def __init__(
        self,
        binary: str = "claude",
        model: str | None = None,
        allowed_tools: str = "",
        mcp_config: str | None = None,
        max_budget_usd: float | None = None,
        timeout_s: float = 1800,
        enabled: bool = True,
    ):
        self.binary = binary
        self.model = model
        self.allowed_tools = allowed_tools
        self.mcp_config = mcp_config
        self.max_budget_usd = max_budget_usd
        self.timeout_s = timeout_s
        self.available = enabled
        self._procs: dict[str, asyncio.subprocess.Process] = {}

    def build_argv(self, req: AgentRequest) -> list[str]:
        prompt = req.prompt
        argv = [self.binary, "-p", "--output-format", "json"]
        if req.read_only:
            argv += ["--permission-mode", "dontAsk", "--tools", _READ_ONLY_TOOLS]
        else:
            argv += ["--permission-mode", "acceptEdits"]
            if self.allowed_tools:
                argv += ["--allowedTools", self.allowed_tools]
        if req.resume_session:
            argv += ["--resume", req.resume_session]
        if self.model:
            argv += ["--model", self.model]
        if self.mcp_config:
            argv += ["--mcp-config", self.mcp_config]
        if self.max_budget_usd:
            argv += ["--max-budget-usd", str(self.max_budget_usd)]
        if req.system:
            argv += ["--append-system-prompt", req.system]
        if req.json_schema:
            argv += ["--json-schema", json.dumps(req.json_schema)]
        if req.attachments:
            dirs = sorted({str(a.storage_path.parent) for a, _ in req.attachments})
            for d in dirs:
                argv += ["--add-dir", d]
            listing = "\n".join(
                f"- {a.filename} ({a.mime_type}): {a.storage_path}" for a, _ in req.attachments
            )
            prompt += f"\n\nAttached files (read them with the Read tool):\n{listing}"
        return argv + ["--", prompt]

    async def execute(self, req: AgentRequest) -> AgentResult:
        if req.workspace is None:
            raise ConnectorError("claude_code requires a workspace directory", "unsupported")
        env = child_env()
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.build_argv(req),
                cwd=req.workspace,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except FileNotFoundError as e:
            raise ConnectorError(f"claude binary not found: {self.binary}", "unavailable") from e
        self._procs[req.task_id] = proc
        try:
            out, err = await asyncio.wait_for(proc.communicate(), self.timeout_s)
        except TimeoutError as e:
            await self.cancel(req.task_id)
            outcome = "failed" if req.read_only else "unknown"
            raise ConnectorError("claude_code timed out", "timeout", outcome=outcome) from e
        except asyncio.CancelledError:
            await self.cancel(req.task_id)
            raise
        finally:
            self._procs.pop(req.task_id, None)
        return self.parse(out, err, proc.returncode)

    def parse(self, out: bytes, err: bytes, returncode: int | None) -> AgentResult:
        try:
            data = json.loads(out)
        except json.JSONDecodeError as e:
            tail = redact(err.decode(errors="replace")[-500:])
            raise ConnectorError(f"claude_code exited {returncode}: {tail}") from e
        if data.get("is_error") or returncode != 0:
            text = redact(str(data.get("result", "")))[:500]
            category = "auth" if ("authenticat" in text.lower() or " 401 " in text) else "provider"
            raise ConnectorError(f"claude_code error ({data.get('subtype')}): {text}", category)
        usage = data.get("usage") or {}
        models = list((data.get("modelUsage") or {}).keys())
        return AgentResult(
            text=str(data.get("result", "")),
            provider=self.name,
            model=models[0] if models else self.model,
            session_id=data.get("session_id"),
            structured=data.get("structured_output"),
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cost_usd=data.get("total_cost_usd"),  # client-side estimate per docs
        )

    async def health_status(self) -> tuple[str, str]:
        """Free check: binary runs and reports a version. Auth is only proven by a real run."""
        if not self.available:
            return "unavailable", "CLAUDE_CODE_ENABLED is false"
        env = child_env()
        try:
            proc = await asyncio.create_subprocess_exec(
                self.binary,
                "--version",
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), 20)
        except (FileNotFoundError, TimeoutError) as e:
            return "unavailable", f"{self.binary} --version failed: {type(e).__name__}"
        if proc.returncode != 0:
            return "unavailable", f"{self.binary} --version exited {proc.returncode}"
        return "validated", f"{out.decode().strip()} (binary ok; auth not yet proven)"

    async def cancel(self, task_id: str) -> bool:
        proc = self._procs.get(task_id)
        if proc is None or proc.returncode is not None:
            return False
        # SIGINT lets the CLI end the turn; escalate to SIGTERM on the group.
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                return True
            try:
                await asyncio.wait_for(proc.wait(), 5)
                return True
            except TimeoutError:
                continue
        os.killpg(proc.pid, signal.SIGKILL)
        return True
