"""review_project: runs the game-review skill's run.sh in the project's folder, in the background."""

import asyncio

from mcp.client import Client

from app.mcp_server import build_mcp_server
from tests.test_config_gateway import gateway, make

A = "2048 Shooter (K05)"
FAKE = """#!/usr/bin/env bash
echo "args: $*" > args.txt
env > env.txt
mkdir -p 4_delivery/uat/reviews
echo "# QA parity review" > 4_delivery/uat/reviews/r.md
echo "[ok] $PWD/4_delivery/uat/reviews/r.md"
echo "[skip] droid not installed"
"""


async def test_review_runs_the_skill_and_reports(settings, cfg_dir, tmp_path, monkeypatch):
    repo = tmp_path / "k05"
    script = repo / ".factory/skills/game-review/run.sh"
    script.parent.mkdir(parents=True)
    script.write_text(FAKE)
    script.chmod(0o755)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:secret")
    orch, _ = make(settings, cfg_dir, [])
    gw = gateway(orch)
    sid = orch.channel.pull(A, None, cwd=str(repo))[0].session_id
    async with Client(build_mcp_server(orch)) as c:
        bad = await c.call_tool("review_project", {"project": A, "mode": "vibes"})
        assert bad.is_error and "benchmark" in bad.content[0].text
        r = (
            await c.call_tool(
                "review_project",
                {"project": A, "mode": "parity", "agent": "all", "session_id": sid},
            )  # fmt: skip
        ).structured_content
        assert r["state"] == "running"
        again = await c.call_tool("review_project", {"project": A, "mode": "parity"})
        assert again.is_error and "still running" in again.content[0].text
        for _ in range(100):
            done = (
                await c.call_tool("get_review", {"review_id": r["review_id"]})
            ).structured_content
            if done["state"] != "running":
                break
            await asyncio.sleep(0.05)
    assert done["state"] == "done" and done["files"][0].endswith("r.md")
    assert done["problems"] == ["droid not installed"]
    (got,) = orch.channel.pull(A, sid, str(repo), 5)[1]  # the asker is woken with the result
    assert got["kind"] == "answer" and r["review_id"] in got["text"] and "r.md" in got["text"]
    assert (repo / "args.txt").read_text().strip() == "args: --role qa --mode parity --agent all"
    assert "TELEGRAM_BOT_TOKEN" not in (repo / "env.txt").read_text()  # no gateway secrets
    sent = [kw["text"] for _, kw, _ in gw.bot.sent]
    assert (
        any("Review started" in t for t in sent)
        and "Review done" in sent[-1]
        and "r.md" in sent[-1]
    )
