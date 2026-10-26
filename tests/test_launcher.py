import subprocess
from types import SimpleNamespace

from app import launcher
from app.launcher import find_projects, is_connected, open_project, terminal_command


def repos(root, *names):
    for n in names:
        (root / n).mkdir()
        subprocess.run(["git", "init", "-q", str(root / n)], check=True)
    (root / "notes").mkdir()  # not a git repo: never offered


def test_find_projects(tmp_path):
    repos(tmp_path, "k09-coin-sort-fun", "MiniGame2048", "ios-quotes", "ios-user-app")
    names = lambda q: [d.name for d in find_projects(tmp_path, q)]  # noqa: E731
    assert names("k09-coin-soft-fun") == ["k09-coin-sort-fun"]  # typo
    assert names("k09") == ["k09-coin-sort-fun"] and names("2048") == ["MiniGame2048"]
    assert (
        names("ios") == ["ios-quotes", "ios-user-app"] and names("notes") == [] and names("") == []
    )
    (tmp_path / "mario").mkdir()  # a new project from the template, before `git init`
    (tmp_path / "mario/AGENTS.md").write_text("# Agents\n")
    assert names("mario") == ["mario"]


def test_open_project_connects_once_and_avoids_duplicates(tmp_path, monkeypatch):
    monkeypatch.setattr("app.global_setup.installed", lambda: False)  # not the real home's install
    repos(tmp_path, "k09-coin-sort-fun")
    repo = tmp_path / "k09-coin-sort-fun"
    launched = []
    status, hits = open_project(tmp_path, "k09", launch=launched.append, token=False)
    assert status == "opened_connected" and launched == [repo] and is_connected(repo)
    assert (
        open_project(tmp_path, "k09", [str(repo) + "/"], launch=launched.append)[0]
        == "already_open"
    )
    assert (
        open_project(tmp_path, "k09", [str(repo)], new=True, launch=launched.append)[0] == "opened"
    )
    assert open_project(tmp_path, "zzz", launch=launched.append)[0] == "none" and len(launched) == 2
    assert terminal_command(repo).startswith(f"cd {repo} && ") and terminal_command(repo).endswith(
        " session"
    )


async def test_telegram_open(settings, cfg_dir, tmp_path, monkeypatch):
    monkeypatch.setattr("app.global_setup.installed", lambda: False)
    from tests.test_config_gateway import CONTROL, USER, gateway, make

    root = tmp_path / "work"
    root.mkdir()
    repos(root, "k09-coin-sort-fun", "ios-quotes", "ios-user-app")
    orch, _ = make(settings, cfg_dir, [])
    orch.s.projects_root = root
    gw = gateway(orch)
    launched = []
    monkeypatch.setattr(launcher, "open_in_terminal", launched.append)
    answers = []

    async def answer(t):
        answers.append(t)

    m = SimpleNamespace(
        chat=SimpleNamespace(id=CONTROL), from_user=SimpleNamespace(id=USER), answer=answer
    )
    await gw.on_open(m, SimpleNamespace(args="k09-coin-soft-fun"))
    assert launched == [root / "k09-coin-sort-fun"] and answers[-1].startswith("🖥 <b>MỞ PHIÊN</b>")
    assert "connected the repo" in answers[-1]
    await gw.on_open(m, SimpleNamespace(args="ios"))
    assert "Several folders match" in answers[-1] and "/open ios-quotes" in answers[-1]
    orch.channel.connect("cc_x", "PocketSort", str(root / "k09-coin-sort-fun"))
    await gw.on_open(m, SimpleNamespace(args="k09"))
    assert "already open (s1)" in answers[-1] and len(launched) == 1
    await gw.on_open(m, SimpleNamespace(args="k09 new"))
    assert len(launched) == 2


async def test_close_then_open_again(settings, cfg_dir, tmp_path, monkeypatch):
    """/open k09 -> /close its terminal session -> /open k09 opens again (its loop died with it)."""
    import subprocess as sp
    import sys

    from tests.test_config_gateway import CONTROL, USER, gateway, make

    root = tmp_path / "work"
    root.mkdir()
    repos(root, "k09-coin-sort-fun")
    k09 = str(root / "k09-coin-sort-fun")
    orch, _ = make(settings, cfg_dir, [])
    orch.s.projects_root = root
    gw = gateway(orch)
    launched = []
    monkeypatch.setattr(launcher, "open_in_terminal", launched.append)
    answers = []

    async def answer(t):
        answers.append(t)

    m = SimpleNamespace(
        chat=SimpleNamespace(id=CONTROL), from_user=SimpleNamespace(id=USER), answer=answer
    )
    claude = sp.Popen([sys.executable, "-c", "import time; time.sleep(30)", "claude"])
    try:
        push = orch.channel.connect("cc_k", "PocketSort", k09, blocked=True, pid=claude.pid)
        pull, _, _ = orch.channel.pull("PocketSort", None, k09)  # its loop
        await gw.on_open(m, SimpleNamespace(args="k09"))
        assert "already open" in answers[-1] and launched == []
        await gw.on_close(m, SimpleNamespace(args=f"s{pull.num}"))  # closing the loop: gone at once
        assert "removed" in answers[-1] and pull.session_id not in orch.channel.sessions
        await gw.on_close(m, SimpleNamespace(args=f"s{push.num}"))
        assert "stopped its Claude Code process" in answers[-1]
        assert orch.channel.live() == []
        await gw.on_open(m, SimpleNamespace(args="k09"))
        assert launched == [root / "k09-coin-sort-fun"]
    finally:
        claude.kill()


async def test_closing_terminal_session_drops_its_loop(settings, cfg_dir):
    import subprocess as sp
    import sys

    from tests.test_config_gateway import make

    orch, _ = make(settings, cfg_dir, [])
    hub = orch.channel
    claude = sp.Popen([sys.executable, "-c", "import time; time.sleep(30)", "claude"])
    try:
        push = hub.connect("cc_k", "PocketSort", "/w/k09", blocked=True, pid=claude.pid)
        hub.pull("PocketSort", None, "/w/k09")
        other, _, _ = hub.pull("Other", None, "/w/other")
        assert hub.close(push) == "stopped its Claude Code process"
        assert [s.session_id for s in hub.live()] == [other.session_id]  # same-folder loop dropped
    finally:
        claude.kill()


async def test_open_by_project_id_and_buttons(settings, cfg_dir, tmp_path, monkeypatch):
    from tests.test_config_gateway import CONTROL, STRANGER, USER, gateway, make

    root = tmp_path / "work"
    root.mkdir()
    repos(root, "k09-coin-sort-fun", "MiniGame2048")
    orch, _ = make(settings, cfg_dir, [])
    orch.s.projects_root = root
    gw = gateway(orch)
    hub = orch.channel
    launched = []
    monkeypatch.setattr(launcher, "open_in_terminal", launched.append)
    answers = []

    async def answer(t, reply_markup=None):
        answers.append((t, reply_markup))

    m = SimpleNamespace(
        chat=SimpleNamespace(id=CONTROL), from_user=SimpleNamespace(id=USER), answer=answer
    )
    s = hub.connect(
        "cc_k", "PocketSort", str(root / "k09-coin-sort-fun")
    )  # earlier session -> p1 + folder
    (tmp_path / "elsewhere").mkdir()
    hub.connect("cc_x", "Elsewhere", str(tmp_path / "elsewhere"))  # p2, folder outside the root
    hub.disconnect(s)

    await gw.on_open(m, SimpleNamespace(args=""))  # menu: one button per project with a folder
    text, kb = answers[-1]
    buttons = [b for row in kb.inline_keyboard for b in row]
    assert "tap a project" in text and [(b.text, b.callback_data) for b in buttons][0] == (
        "p1 · k09-coin-sort-fun",
        "o:1",
    )

    await gw.on_open(m, SimpleNamespace(args="p1"))
    assert launched == [root / "k09-coin-sort-fun"]
    await gw.on_open(m, SimpleNamespace(args="p2"))
    assert "outside" in answers[-1][0] and len(launched) == 1
    await gw.on_open(m, SimpleNamespace(args="p9"))
    assert "Unknown project p9" in answers[-1][0]

    alerts = []

    async def q_answer(text=None, show_alert=False):
        alerts.append(text)

    q = SimpleNamespace(data="o:1", from_user=SimpleNamespace(id=USER), answer=q_answer,
                        message=SimpleNamespace(chat=SimpleNamespace(id=CONTROL), answer=answer))  # fmt: skip
    await gw.on_open_button(q)
    assert alerts[-1] == "Opening…" and len(launched) == 2
    q.from_user = SimpleNamespace(id=STRANGER)
    await gw.on_open_button(q)
    assert alerts[-1] == "Not authorized." and len(launched) == 2


def test_open_skips_repo_setup_when_installed_globally(tmp_path, monkeypatch):
    monkeypatch.setattr("app.global_setup.installed", lambda: True)
    repos(tmp_path, "k09-coin-sort-fun")
    status, _ = open_project(tmp_path, "k09", launch=lambda repo: None)
    assert status == "opened" and not (tmp_path / "k09-coin-sort-fun/.mcp.json").exists()


def test_open_prefers_the_disconnected_app_session(tmp_path, monkeypatch):
    """ball-shot is open in Claude desktop but its waiter stopped: /open points there, no 2nd session."""
    import json
    import time

    root, apps = tmp_path / "Working", tmp_path / "app"
    root.mkdir()
    repos(root, "ball-shot")
    monkeypatch.setattr(launcher, "APP_SESSIONS", apps)
    (apps / "a/b").mkdir(parents=True)
    rec = {"sessionId": "local_1", "cwd": str(root / "ball-shot"), "title": "Ball Shot work",
           "isArchived": False, "lastActivityAt": time.time() * 1000}  # fmt: skip
    (apps / "a/b/local_1.json").write_text(json.dumps(rec))
    opened = []
    launch = opened.append
    status, _ = open_project(root, "ball-shot", [], launch=launch)
    assert status == "open_in_app" and opened == []
    assert launcher.app_sessions(root / "ball-shot")[0]["title"] == "Ball Shot work"
    status, _ = open_project(root, "ball-shot", [], new=True, launch=launch)
    assert status.startswith("opened") and len(opened) == 1  # "new" still opens a Terminal one
    (apps / "a/b/local_1.json").write_text(json.dumps({**rec, "isArchived": True}))
    assert launcher.app_sessions(root / "ball-shot") == []  # archived: not open


def test_project_dir_follows_a_renamed_folder(settings, cfg_dir, tmp_path, monkeypatch):
    """colorever -> ok03-colorever-gem-art-puzzle: /open p# finds the folder by its README name."""
    from app import projects
    from tests.test_config_gateway import make

    orch, _ = make(settings, cfg_dir, [])
    root = tmp_path / "Working"
    root.mkdir()
    repos(root, "ok03-colorever-gem-art-puzzle")
    (root / "ok03-colorever-gem-art-puzzle/README.md").write_text("# Glazed Gems (GMA)\n")
    monkeypatch.setattr(projects, "projects_root", lambda: root)
    s, _, _ = orch.channel.pull("Glazed Gems (GMA)", None, str(root / "colorever"))  # old folder
    orch.channel.disconnect(s)
    assert orch.channel.project_dir("Glazed Gems (GMA)") == str(
        root / "ok03-colorever-gem-art-puzzle"
    )
