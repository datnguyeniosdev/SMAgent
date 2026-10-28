"""`agw mcp-stdio`: the agent-gateway MCP tools over stdio, for Claude Code sessions. Its HTTP MCP
client does not reconnect when the gateway restarts (a CLI session loses the tools until /mcp);
this proxy lives as long as the session and opens a new connection whenever the old one broke, so
a gateway restart is only a pause for the agent."""

from __future__ import annotations

import logging
import time
from contextlib import AsyncExitStack

import anyio
from mcp.client import Client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

log = logging.getLogger(__name__)
WAIT_S = 120  # a restart takes ~20 s; longer, the agent hears the error
INSTRUCTIONS = (
    "Local orchestration gateway. Use delegate_task to start work, get_task_status / get_task_result to "
    "follow it, submit_report to report results with screenshots, and request_human_input / "
    "get_human_response to ask the operator via Telegram."
)


class Backend:
    """The gateway's HTTP MCP endpoint, (re)connected on demand. One task (run) owns the connection
    (anyio scopes must close where they opened); tool calls queue for it, one at a time."""

    def __init__(self, url: str):
        self.url, self.client, self.stack = url, None, None
        self.send, self.recv = anyio.create_memory_object_stream(100)

    async def _close(self) -> None:
        stack, self.client, self.stack = self.stack, None, None
        if stack is not None:
            try:
                await stack.aclose()
            except Exception as e:  # noqa: BLE001 - the old connection is already broken
                log.debug("closing a broken connection: %s", type(e).__name__)

    async def _call(self, fn):
        """fn(client): retried on a fresh connection while the gateway is down or restarting.
        ponytail: a call the gateway ran just before it went down may run twice; tools that matter
        (reports, questions) are cheap to repeat, and a lost one would be worse."""
        deadline = time.monotonic() + WAIT_S
        while True:
            try:
                if self.client is None:
                    stack = AsyncExitStack()
                    self.client = await stack.enter_async_context(Client(self.url))
                    self.stack = stack
                return await fn(self.client)
            except Exception as e:  # noqa: BLE001 - connection gone (restart): reconnect and retry
                await self._close()
                if time.monotonic() > deadline:
                    raise
                log.info("gateway unreachable (%s): retrying", type(e).__name__)
                await anyio.sleep(2)

    async def run(self) -> None:
        try:
            async for fn, box, done in self.recv:
                try:
                    box["result"] = await self._call(fn)
                except Exception as e:  # noqa: BLE001 - handed to the caller
                    box["error"] = e
                done.set()
        finally:
            await self._close()

    async def call(self, fn):
        box, done = {}, anyio.Event()
        await self.send.send((fn, box, done))
        await done.wait()
        if "error" in box:
            raise box["error"]
        return box["result"]


def build(backend: Backend) -> Server:
    async def list_tools(ctx, params):
        return await backend.call(lambda c: c.list_tools())

    async def call_tool(ctx, params):
        return await backend.call(lambda c: c.call_tool(params.name, params.arguments or {}))

    return Server("agent-gateway", instructions=INSTRUCTIONS, on_list_tools=list_tools,
                  on_call_tool=call_tool)  # fmt: skip


async def serve(url: str) -> None:
    backend = Backend(url)
    server = build(backend)
    async with stdio_server() as (read, write), anyio.create_task_group() as tg:
        tg.start_soon(backend.run)
        await server.run(read, write, server.create_initialization_options())
        tg.cancel_scope.cancel()
