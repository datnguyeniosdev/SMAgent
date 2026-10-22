"""dev/uat run beside prod: own .env.<env>, port and data; agent configs and prod's bot stay untouched."""

import pytest

from app import agents_setup, config, global_setup
from app.cli import main


@pytest.fixture
def gw(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGW_ENV", "prod")  # undone after the test, also when main() changed it
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=prod-token\nTELEGRAM_ALLOWED_USER_IDS=42\n")
    return tmp_path


def test_dev_setup_creates_its_own_env_without_touching_agents(gw, monkeypatch, capsys):
    prod = (gw / ".env").read_text()
    calls = []
    monkeypatch.setattr(global_setup, "_claude", lambda *a: calls.append(a))
    assert main(["--env", "dev", "setup"]) == 0
    out = capsys.readouterr().out
    assert "agw --env dev dev" in out and "http://127.0.0.1:8001" in out
    env = (gw / ".env.dev").read_text()
    assert (
        "DATA_DIR=var/dev" in env and "TELEGRAM_ALLOWED_USER_IDS=42" in env and "API_TOKEN=" in env
    )
    assert (gw / ".env").read_text() == prod and calls == []  # prod and Claude's MCP untouched
    s = config.load_settings()
    assert (
        str(s.data_dir).endswith("var/dev") and s.telegram_bot_token is None
    )  # prod's bot not inherited
    assert main(["--env", "dev", "setup", "--agents", "codex"]) == 1
    with pytest.raises(ValueError, match="prod-only"):
        agents_setup.install("codex")
    with pytest.raises(ValueError, match="prod-only"):
        global_setup.install(register_mcp=False)


def test_dev_refuses_prods_bot(gw, monkeypatch):
    monkeypatch.setenv("AGW_ENV", "uat")
    (gw / ".env.uat").write_text(
        "TELEGRAM_BOT_TOKEN=prod-token\nTELEGRAM_CONTROL_CHAT_ID=-1\nTELEGRAM_REPORT_CHANNEL_ID=-1\nTELEGRAM_ALLOWED_USER_IDS=42\n"
    )
    with pytest.raises(ValueError, match="own bot"):
        config.load_settings()
    monkeypatch.setenv("AGW_ENV", "staging")
    with pytest.raises(ValueError, match="AGW_ENV"):
        config.env_name()
