"""Real smoke test of the Claude Code connector (makes a real, small model call).

Usage: uv run python -m scripts.smoke_claude_code
Run it outside a Claude Code session: the CLI refuses to start nested.
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

from app.connectors.base import AgentRequest, ConnectorError
from app.connectors.claude_code import ClaudeCodeConnector


async def main() -> int:
    conn = ClaudeCodeConnector(timeout_s=180)
    with tempfile.TemporaryDirectory() as d:
        Path(d, "hello.txt").write_text("the secret word is pelican\n")
        req = AgentRequest(
            "smoke",
            "Read hello.txt and return the secret word.",
            workspace=Path(d),
            json_schema={
                "type": "object",
                "properties": {"word": {"type": "string"}},
                "required": ["word"],
            },
        )
        try:
            r = await conn.execute(req)
        except ConnectorError as e:
            print(json.dumps({"ok": False, "category": e.category, "error": str(e)}))
            return 1
        print(
            json.dumps(
                {
                    "ok": True,
                    "structured": r.structured,
                    "session": r.session_id,
                    "model": r.model,
                    "cost_usd_estimate": r.cost_usd,
                }
            )
        )
        return 0 if (r.structured or {}).get("word") == "pelican" else 1


sys.exit(asyncio.run(main()))
