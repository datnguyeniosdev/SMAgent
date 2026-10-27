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
            ag.install(agent, URL, token=False)

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
    hooks = json.loads((home / ".cursor/hooks.json").read_text())
    assert hooks["version"] == 1 and "cursor-hook" in hooks["hooks"]["sessionStart"][0]["command"]
    assert (
        hooks["hooks"]["stop"][0]["loop_limit"] is None
        and hooks["hooks"]["stop"][0]["timeout"] == 3600
    )
    gem = (home / ".gemini/GEMINI.md").read_text()
    assert (
        gem.startswith("my own notes") and gem.count(ag.START) == 1 and 'agent="antigravity"' in gem
    )
    assert "wait --agent antigravity" in gem  # its conversations wake themselves (agentapi)
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
    assert "hooks in" in out and "opencode: MCP in" in out
    main(["setup", "--agents", "cursor", "--print-rules"])
    assert 'agent="cursor"' in capsys.readouterr().out
    main(["setup", "--agents", "cursor,opencode", "--remove"])
    assert not ag.configured("cursor") and not ag.configured("opencode")
    assert "cursor-hook" not in (home / ".cursor/hooks.json").read_text()  # our hooks removed


def test_cursor_with_mcp_only_is_not_configured(home):
    (home / ".cursor").mkdir()
    (home / ".cursor/mcp.json").write_text(json.dumps({"mcpServers": {ag.SERVER: {"url": URL}}}))
    assert not ag.configured("cursor")
    ag.install("cursor", URL, token=False)
    assert ag.configured("cursor")
    assert "cursor-hook" in (home / ".cursor/hooks.json").read_text()


def test_token_header_names_the_variable_never_the_secret(home):
    for agent in ("opencode", "codex", "antigravity", "cursor"):
        ag.install(agent, URL, token=True)
    oc = json.loads((home / ".config/opencode/opencode.json").read_text())["mcp"]["agent-gateway"]
    assert oc["headers"] == {"Authorization": "Bearer {env:AGW_API_TOKEN}"}
    codex = tomllib.loads((home / ".codex/config.toml").read_text())["mcp_servers"]["agent-gateway"]
    assert codex["bearer_token_env_var"] == "AGW_API_TOKEN"
    for f in (".gemini/config/mcp_config.json", ".cursor/mcp.json"):
        assert "Bearer ${" in (home / f).read_text()


def test_codex_gets_the_claude_skills_as_links(home):
    for name in ("agent-gateway", "game-review"):
        (home / ".claude/skills" / name).mkdir(parents=True)
        (home / ".claude/skills" / name / "SKILL.md").write_text("---\nname: x\n---\n")
    (home / ".claude/skills/synced").mkdir()  # not a skill: no SKILL.md
    (home / ".codex/skills/mine").mkdir(parents=True)  # the user's own skill stays
    out = ag.install("codex", URL, token=False)
    assert "skills (agent-gateway, game-review)" in out
    links = home / ".codex/skills"
    assert (links / "game-review").is_symlink() and not (links / "synced").exists()
    ag.remove("codex")
    assert sorted(p.name for p in links.iterdir()) == ["mine"]


def test_codex_app_tables_inside_the_markers_survive(home):
    """The Codex app rewrites config.toml and can leave its own tables between our markers."""
    (home / ".codex").mkdir()
    rewritten = (CODEX + f"\n{ag.TOML_START}\n[mcp_servers.agent-gateway]\nurl = \"{URL}\"\n\n"
                 '[mcp_servers.node_repl]\ncommand = "node_repl"\n\n[desktop]\nmode = "steer"\n'
                 f"{ag.TOML_END}")  # fmt: skip
    (home / ".codex/config.toml").write_text(rewritten)
    ag.install("codex", URL)
    cfg = tomllib.loads((home / ".codex/config.toml").read_text())
    assert (
        cfg["mcp_servers"]["node_repl"]["command"] == "node_repl"
        and cfg["desktop"]["mode"] == "steer"
    )
    assert cfg["mcp_servers"]["agent-gateway"]["default_tools_approval_mode"] == "approve"
    ag.remove("codex")
    cfg = tomllib.loads((home / ".codex/config.toml").read_text())
    assert "agent-gateway" not in cfg["mcp_servers"] and cfg["desktop"]["mode"] == "steer"
