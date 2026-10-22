"""`agw setup` dispatch, aliases and hidden internal commands."""

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
