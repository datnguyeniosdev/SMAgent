import json

from app.repo_setup import END, START, setup_repo

URL = "http://127.0.0.1:8000/mcp"


def test_repo_setup_is_idempotent_and_replaces_legacy(tmp_path):
    repo = tmp_path / "game"
    (repo / "4_delivery/deploy").mkdir(parents=True)
    (repo / "4_delivery/deploy/deploy.sh").write_text("#!/bin/sh\n")
    (repo / "README.md").write_text("# T\n\n## Game Identity\n\n| Field | Value |\n|---|---|\n"
                                    "| Game code | `K05` |\n| Game name | `2048 Shooter` |\n")  # fmt: skip
    (repo / "CLAUDE.md").write_text(
        "# CLAUDE.md\n\nKeep me.\n\n## Deploy notification (agent-gateway MCP)\n\nold deploy text\n\n"
        "## Other rules\n\nKeep me too.\n\n## Task report and Telegram control (agent-gateway MCP)\n\nold\n"
    )
    (repo / ".mcp.json").write_text(json.dumps({"mcpServers": {"unity": {"type": "http"}}}))
    r = setup_repo(repo, URL)
    assert (
        r["project"] == "2048 Shooter (K05)" and r["claude_md"] == "added" and r["deploy_section"]
    )
    text = (repo / "CLAUDE.md").read_text()
    assert "Keep me." in text and "## Other rules\n\nKeep me too." in text
    assert "old deploy text" not in text and "\nold\n" not in text  # legacy sections replaced
    assert (
        text.count(START) == 1 and "`report_deployment`" in text and "`2048 Shooter (K05)`" in text
    )
    assert setup_repo(repo, URL)["claude_md"] == "updated"
    assert (repo / "CLAUDE.md").read_text() == text  # second run changes nothing
    mcp = json.loads((repo / ".mcp.json").read_text())["mcpServers"]
    assert (
        "unity" in mcp
        and mcp["agent-gateway"]["headers"]["Authorization"] == "Bearer ${AGW_API_TOKEN}"
    )
    local = json.loads((repo / ".claude/settings.local.json").read_text())
    assert local["enabledMcpjsonServers"] == ["agent-gateway"]
    assert len(local["permissions"]["allow"]) == len(set(local["permissions"]["allow"])) == 10


def test_repo_setup_creates_claude_md_importing_agents(tmp_path):
    repo = tmp_path / "unity"
    (repo / ".claude").mkdir(parents=True)
    (repo / ".claude/AGENTS.md").write_text("# rules\n")
    (repo / "README.md").write_text("# Neon Ball Shooter\n")
    r = setup_repo(repo, URL)
    text = (repo / "CLAUDE.md").read_text()
    assert (
        r["claude_md"] == "created" and "@.claude/AGENTS.md" in text and text.rstrip().endswith(END)
    )
    assert "report_deployment" not in text  # no deploy script, no deploy section
    assert "`Neon Ball Shooter`" in text


def test_repo_setup_without_token_writes_no_header(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    setup_repo(repo, URL, "P", token=False)
    server = json.loads((repo / ".mcp.json").read_text())["mcpServers"]["agent-gateway"]
    assert server == {"type": "http", "url": URL}


def test_detect_project_ignores_notes_after_the_name(tmp_path):
    from app.repo_setup import detect_project

    (tmp_path / "README.md").write_text(
        "# KTS Game Repo Template\n\n| Game code | `TCN` |\n"
        '| Game name | `Coinlets: Stack & Merge` (was "Tini Coin") |\n'
    )
    assert detect_project(tmp_path) == "Coinlets: Stack & Merge (TCN)"
    (tmp_path / "README.md").write_text("# T\n\n| Game name | `<GAME NAME>` |\n")
    assert detect_project(tmp_path) == "T"  # placeholder -> README title
