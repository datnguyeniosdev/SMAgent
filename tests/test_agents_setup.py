"""Connecting other coding agents: each one's documented config shape, idempotent, reversible."""

import json
import tomllib
from pathlib import Path

import pytest

from app import agents_setup as ag

URL = "http://127.0.0.1:8000/mcp"
CODEX = """model_provider = "ZAI"
model = "glm-5.3"

[model_providers.ZAI]
name = "ZAI"

[projects."/Users/me/Working/MiniGame2048"]
trust_level = "trusted"
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def test_each_agent_gets_its_documented_shape_and_removal_restores(home):
    (home / ".config/opencode").mkdir(parents=True)
    (home / ".config/opencode/opencode.json").write_text(json.dumps({"theme": "dark"}))
    (home / ".codex").mkdir()
    (home / ".codex/config.toml").write_text(CODEX)
    (home / ".gemini").mkdir()
    (home / ".gemini/GEMINI.md").write_text("my own notes\n")
    assert ag.detected() == ["opencode", "codex", "antigravity"]

    for _ in range(2):  # idempotent
        for agent in ("opencode", "codex", "antigravity", "cursor"):
            ag.install(agent, URL)

    oc = json.loads((home / ".config/opencode/opencode.json").read_text())
    assert oc["theme"] == "dark" and oc["mcp"]["agent-gateway"] == {
        "type": "remote",
        "url": URL,
        "enabled": True,
    }
    codex = (home / ".codex/config.toml").read_text()
    parsed = tomllib.loads(codex)
    assert (
        parsed["model_provider"] == "ZAI" and parsed["mcp_servers"]["agent-gateway"]["url"] == URL
    )
    assert codex.count(ag.TOML_START) == 1
    agy = json.loads((home / ".gemini/config/mcp_config.json").read_text())
    assert agy["mcpServers"]["agent-gateway"] == {"serverUrl": URL}  # not url/httpUrl
    cur = json.loads((home / ".cursor/mcp.json").read_text())
    assert cur["mcpServers"]["agent-gateway"] == {"url": URL}
    gem = (home / ".gemini/GEMINI.md").read_text()
    assert (
        gem.startswith("my own notes") and gem.count(ag.START) == 1 and 'agent="antigravity"' in gem
    )
    assert 'agent="codex"' in (home / ".codex/AGENTS.md").read_text()
    assert (home / ".codex/config.toml.agw-backup").read_text() == CODEX  # backup of the original
    assert all(ag.configured(a) for a in ag.AGENTS)

    for agent in ag.AGENTS:
        ag.remove(agent)
    assert (home / ".codex/config.toml").read_text().strip() == CODEX.strip()
    assert json.loads((home / ".config/opencode/opencode.json").read_text())["mcp"] == {}
    assert (home / ".gemini/GEMINI.md").read_text().strip() == "my own notes"
    assert not any(ag.configured(a) for a in ag.AGENTS)


def test_codex_keeps_an_existing_unmanaged_entry(home):
    (home / ".codex").mkdir()
    own = CODEX + '\n[mcp_servers.agent-gateway]\nurl = "http://elsewhere/mcp"\n'
    (home / ".codex/config.toml").write_text(own)
    assert "left unchanged" in ag.install("codex", URL)
    assert (home / ".codex/config.toml").read_text() == own


def test_names_and_cli(home, capsys):
    from app.cli import main

    assert ag.resolve("gemini,claude,opencode") == ["antigravity", "claude", "opencode"]
    assert ag.resolve("all") == list(ag.AGENTS)
    with pytest.raises(ValueError, match="unknown agent"):
        ag.resolve("notepad")
    assert main(["setup", "--agents", "cursor,opencode"]) == 0
    out = capsys.readouterr().out
    assert "--print-rules" in out and "opencode: MCP in" in out
    main(["setup", "--agents", "cursor", "--print-rules"])
    assert 'agent="cursor"' in capsys.readouterr().out
    main(["setup", "--agents", "cursor,opencode", "--remove"])
    assert not ag.configured("cursor") and not ag.configured("opencode")
