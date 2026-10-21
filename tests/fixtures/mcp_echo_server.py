"""Minimal real MCP stdio server used by the MCP client tests (official SDK MCPServer)."""

import os

from mcp.server.mcpserver import MCPServer

server = MCPServer("echo-fixture")


@server.tool()
def echo(text: str) -> str:
    """Echo text back."""
    return f"echo: {text}"


@server.tool()
def read_len(path: str) -> str:
    """Return the size of a file (path must be inside the workspace)."""
    return str(os.path.getsize(path))


@server.tool()
def flaky(fail_times: int = 2) -> str:
    """Fails the first `fail_times` calls (counter file in the working directory), then succeeds."""
    counter = "flaky_counter.txt"
    n = int(open(counter).read()) + 1 if os.path.exists(counter) else 1
    with open(counter, "w") as f:
        f.write(str(n))
    if n <= fail_times:
        raise RuntimeError(f"transient failure {n}")
    return f"ok after {n} calls"


@server.tool()
def explode() -> str:
    """Always fails."""
    raise ValueError("kaboom")


if __name__ == "__main__":
    server.run("stdio")
