import json
import subprocess
from pathlib import Path

from app import global_setup, projects


def test_install_is_idempotent_and_remove_restores_user_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    settings = tmp_path / ".claude/settings.json"
    settings.parent.mkdir(parents=True)
    mine = {"model": "opus", "permissions": {"allow": ["Bash(ls:*)"]},
            "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}}  # fmt: skip
    settings.write_text(json.dumps(mine))

    for _ in range(2):  # running it again changes nothing
        global_setup.install(register_mcp=False)
    data = json.loads(settings.read_text())
    assert data["model"] == "opus" and data["permissions"]["allow"][0] == "Bash(ls:*)"
    assert len(data["permissions"]["allow"]) == 1 + len(global_setup.permission_rules())
    cmds = [h["hooks"][0]["command"] for h in data["hooks"]["SessionStart"]]
    assert cmds == ["echo mine", global_setup.hook_command()]
    assert json.loads((tmp_path / ".claude/settings.json.agw-backup").read_text()) == mine
    skill = global_setup.skill_file().read_text()
    assert skill.startswith("---\nname: agent-gateway\n") and "generate_art" in skill
    assert "Installed for every git repo" in skill and "repo-setup <this repo>" not in skill
    assert global_setup.installed()

    global_setup.remove(register_mcp=False)
    assert json.loads(settings.read_text()) == mine and not global_setup.installed()


def test_hook_and_shim_follow_the_folder(tmp_path, monkeypatch, capsys):
    from app.channel_shim import Shim
    from app.cli import main

    root = tmp_path / "Working"
    (root / "colorever").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root / "colorever")], check=True)
    (root / "colorever/README.md").write_text(
        "# T\n\n| Game code | `GMA` |\n| Game name | `Gem Mosaic` |\n"
    )
    (root / "notes").mkdir()  # not a git repo
    monkeypatch.setattr(projects, "projects_root", lambda: root)

    monkeypatch.chdir(root / "notes")
    assert (
        main(["hook-session-start"]) == 0 and capsys.readouterr().out == ""
    )  # silent outside repos
    monkeypatch.chdir(root / "colorever")
    (root / "colorever/3_dev").mkdir()
    monkeypatch.chdir(root / "colorever/3_dev")  # any sub-folder belongs to the repo
    assert main(["hook-session-start"]) == 0
    out = capsys.readouterr().out
    assert "Gem Mosaic (GMA)" in out and "CronCreate" in out and "agent-gateway skill" in out

    started = []
    monkeypatch.setattr(
        Shim, "serve", lambda self, stdin=None: started.append((self.project, self.enabled))
    )
    monkeypatch.setenv("AGW_CHANNEL", "1")
    from app.channel_shim import main as shim_main

    shim_main(None)
    monkeypatch.chdir(root / "notes")
    shim_main(None)
    assert started == [("Gem Mosaic (GMA)", True), ("unconnected", False)]


def test_each_claude_account_is_its_own_folder(tmp_path, monkeypatch):
    from app import global_setup

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "work"))
    assert global_setup.skill_file() == tmp_path / "work/skills/agent-gateway/SKILL.md"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert global_setup.claude_home().name == ".claude"
