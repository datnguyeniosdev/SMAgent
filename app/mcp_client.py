"""MCP client integration (official `mcp` SDK 2.x).

Servers come from MCP_SERVERS. Discovery lists every tool a server exposes, but
only tools named in that server's `allowed_tools` are registered and callable.
Each call opens a fresh session (stdio servers start with cwd = task workspace),
so a lost connection only affects one call.
"""

import asyncio
import logging
import os
from typing import Any

from mcp import Client, StdioServerParameters
from pydantic import BaseModel

from app.builtin_tools import resolve_in_workspace
from app.tools import ToolContext, ToolDefinition, ToolError, ToolOutput, ToolRegistry

log = logging.getLogger(__name__)

_ENV_KEEP = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "USER")


class McpServerConfig(BaseModel):
    name: str
    command: str | None = None  # stdio transport
    args: list[str] = []
    url: str | None = None  # Streamable HTTP transport
    allowed_tools: list[str] = []
    read_only_tools: list[str] = []  # allowlisted tools that need no approval
    path_args: list[str] = ["path", "file_path", "notebook_path"]
    timeout_s: float = 30
    env_passthrough: list[str] = []


class McpDiscovery(BaseModel):
    server: str
    ok: bool
    server_name: str | None = None
    server_version: str | None = None
    tools: list[dict] = []  # name, description, input_schema, annotations
    error: str | None = None


def _transport(cfg: McpServerConfig, cwd: str | None = None):
    if cfg.url:
        return cfg.url
    if not cfg.command:
        raise ValueError(f"MCP server {cfg.name}: command or url required")
    env = {k: v for k, v in os.environ.items() if k in _ENV_KEEP or k in cfg.env_passthrough}
    return StdioServerParameters(command=cfg.command, args=cfg.args, env=env, cwd=cwd)


async def discover(cfg: McpServerConfig) -> McpDiscovery:
    try:
        async with asyncio.timeout(cfg.timeout_s):
            async with Client(_transport(cfg), read_timeout_seconds=cfg.timeout_s) as c:
                listed = await c.list_tools()
                info = c.server_info
                tools = [
                    {
                        "name": t.name,
                        "description": (t.description or "")[:500],
                        "input_schema": t.input_schema or {"type": "object"},
                        "read_only_hint": bool(t.annotations and t.annotations.read_only_hint),
                    }
                    for t in listed.tools
                ]
        return McpDiscovery(
            server=cfg.name,
            ok=True,
            server_name=getattr(info, "name", None),
            server_version=getattr(info, "version", None),
            tools=tools,
        )
    except Exception as e:  # report, never crash startup
        return McpDiscovery(server=cfg.name, ok=False, error=f"{type(e).__name__}: {e}"[:300])


def _leaf(e: BaseException) -> BaseException:
    """anyio wraps errors in ExceptionGroups; report the underlying cause."""
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    return e


def _parse_result(result: Any) -> ToolOutput:
    texts = [getattr(c, "text", None) for c in (getattr(result, "content", None) or [])]
    text = "\n".join(t for t in texts if t)
    if getattr(result, "is_error", False):
        raise ToolError(text[:2000] or "MCP tool reported an error", "mcp_tool_error")
    return ToolOutput(text, getattr(result, "structured_content", None))


def make_handler(cfg: McpServerConfig, tool_name: str):
    async def handler(ctx: ToolContext, args: dict) -> ToolOutput:
        root = resolve_in_workspace(ctx, ".")
        call_args = dict(args)
        for key in cfg.path_args:  # confine path-like arguments to the workspace
            if isinstance(call_args.get(key), str):
                call_args[key] = str(resolve_in_workspace(ctx, call_args[key]))
        try:
            async with Client(
                _transport(cfg, cwd=str(root)), read_timeout_seconds=cfg.timeout_s
            ) as c:
                result = await c.call_tool(tool_name, call_args)
        except ToolError:
            raise
        except Exception as e:
            root_err = _leaf(e)
            category = "mcp_protocol" if isinstance(root_err, RuntimeError) else "mcp_connection"
            raise ToolError(
                f"MCP {cfg.name}.{tool_name} failed: {type(root_err).__name__}: {root_err}"[:500],
                category,
            ) from e
        return _parse_result(result)

    return handler


def register_mcp_tools(
    registry: ToolRegistry, cfg: McpServerConfig, found: McpDiscovery
) -> list[str]:
    """Register allowlisted tools that the server actually exposes. Returns registered names."""
    registered = []
    exposed = {t["name"]: t for t in found.tools}
    for name in cfg.allowed_tools:
        t = exposed.get(name)
        if t is None:
            registry.discovery_notes.append(
                f"mcp.{cfg.name}: allowlisted tool {name!r} not exposed by server"
            )
            continue
        read_only = name in cfg.read_only_tools
        registry.register(
            ToolDefinition(
                name=f"mcp.{cfg.name}.{name}",
                description=t["description"],
                input_schema=t["input_schema"],
                handler=make_handler(cfg, name),
                permissions=frozenset(
                    {"workspace:read"} if read_only else {"workspace:read", "mcp:execute"}
                ),
                risk="read" if read_only else "execute",
                environment=f"mcp:{cfg.name}",
                timeout_s=cfg.timeout_s,
                requires_approval=not read_only,
                side_effects=not read_only,
                source="mcp",
            )
        )
        registered.append(f"mcp.{cfg.name}.{name}")
    for name in exposed:
        if name not in cfg.allowed_tools:
            registry.discovery_notes.append(f"mcp.{cfg.name}.{name}: discovered, not allowlisted")
    return registered
