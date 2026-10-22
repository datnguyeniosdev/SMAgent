"""`agw setup` dispatch, aliases and hidden internal commands."""

import pytest

from app import cli


def test_setup_dispatch_and_aliases(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(cli, "cmd_init_env", lambda a: calls.append("init-env") or 0)
    monkeypatch.setattr(cli, "cmd_global_setup", lambda a: calls.append(("global", a.remove)) or 0)
    monkeypatch.setattr(cli, "cmd_repo_setup",
                        lambda a: calls.append(("repo", a.repos, a.template, a.project)) or 0)  # fmt: skip
    monkeypatch.setattr(cli, "cmd_mcp_setup", lambda a: calls.append(("mcp", a.scope)) or 0)

    cli.main(["setup"])
    assert calls == ["init-env", ("global", False)]
    calls.clear()
    cli.main(["setup", "--remove"])
    cli.main(["setup", "--repo", "../a", "../b"])
    cli.main(["setup", "--repo", "../tpl", "--template", "--project", "<GAME NAME>"])
    assert calls == [("global", True), ("repo", ["../a", "../b"], False, None),
                     ("repo", ["../tpl"], True, "<GAME NAME>")]  # fmt: skip
    calls.clear()
    for argv in (["init-env"], ["global-setup"], ["global-setup", "--remove"], ["repo-setup", "../x"],
                 ["mcp-setup", "--scope", "user"]):  # fmt: skip
        cli.main(argv)  # older names keep working
    assert calls == ["init-env", ("global", False), ("global", True), ("repo", ["../x"], False, None),
                     ("mcp", "user")]  # fmt: skip


def test_help_lists_everyday_commands_only(capsys):
    import pytest

    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    for shown in ("setup", "dev", "doctor", "open", "session", "serve", "opencode", "validate"):
        assert f"    {shown} " in out
    for hidden in ("init-env", "mcp-setup", "global-setup", "repo-setup", "channel", "hook-session-start",
                   "SUPPRESS"):  # fmt: skip
        assert hidden not in out


def test_stop_keeps_background_starts_off(tmp_path, monkeypatch, capsys):
    """`agw stop` stops this folder's gateway and marks it; the sessions' background health check
    (no terminal) then starts nothing; `agw dev` from a terminal clears the mark and starts."""
    import subprocess
    import sys

    from app import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGW_ENV", "prod")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "var"))
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    gw = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"]
    )  # stands in for uvicorn
    up = {"pid": gw.pid}
    monkeypatch.setattr(cli, "_listener", lambda port: up["pid"] if gw.poll() is None else None)
    monkeypatch.setattr(cli, "_cwd", lambda pid: str(tmp_path))
    assert cli.main(["stop"]) == 0
    gw.wait(5)
    assert gw.returncode is not None and (tmp_path / "var/agw.stopped").exists()
    assert "stay off" in capsys.readouterr().out

    started = []
    monkeypatch.setattr(cli, "_port_open", lambda h, p: False)
    monkeypatch.setattr(
        cli.subprocess,
        "Popen",
        lambda *a, **k: started.append(a) or (_ for _ in ()).throw(SystemExit(0)),
    )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)  # the health check's nohup start
    assert (
        cli.main(["dev"]) == 0 and started == [] and "stopped on purpose" in capsys.readouterr().out
    )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)  # the operator in a terminal
    with pytest.raises(SystemExit):
        cli.main(["dev"])
    assert started and not (tmp_path / "var/agw.stopped").exists()

    monkeypatch.setattr(
        cli, "_cwd", lambda pid: "/somewhere/else"
    )  # never stops another folder's gateway
    up["pid"] = other.pid
    monkeypatch.setattr(cli, "_listener", lambda port: other.pid)
    assert cli.main(["stop"]) == 1 and other.poll() is None
    other.kill()
