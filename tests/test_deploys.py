"""/deploy: a project's deploy script run in a separate worktree at the chosen branch."""

import asyncio
import subprocess

from app import deploys
from tests.test_config_gateway import STRANGER, gateway, make
from tests.test_telegram_panels import buttons, press, tg

SCRIPT = """#!/usr/bin/env bash
echo "deploy $* from $(git rev-parse --abbrev-ref HEAD 2>/dev/null) $(cat version.txt)"
[ -f 4_delivery/deploy/.env.deploy ] && echo "env file linked"
[ "$(cat version.txt)" = "broken" ] && { echo "error: build failed at step 3"; exit 2; }
exit 0
"""


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def game(root):
    repo = root / "CoinGame"
    (repo / "4_delivery/deploy").mkdir(parents=True)
    (repo / "4_delivery/deploy/deploy.sh").write_text(SCRIPT)
    (repo / "README.md").write_text("# Coin Game\n")
    (repo / "version.txt").write_text("v1")
    (repo / ".gitignore").write_text(".env.deploy\n")
    git(repo.parent, "init", "-q", "-b", "main", str(repo))
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "v1")
    git(repo, "checkout", "-qb", "feat/broken")
    (repo / "version.txt").write_text("broken")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "broken")
    git(repo, "checkout", "-q", "main")
    (repo / "4_delivery/deploy/.env.deploy").write_text("SECRET=1\n")  # gitignored settings
    (repo / "wip.txt").write_text("uncommitted work")  # must survive every deploy
    return repo


async def test_runs_in_its_own_worktree_and_reports_exit_code(tmp_path):
    repo = game(tmp_path)
    assert deploys.deployable(tmp_path) == [repo]
    assert deploys.branches(repo)[:2] == ["main", "feat/broken"]
    ok = await deploys.run(repo, "uat", "main", True, tmp_path / "logs")
    text = ok["log"].read_text()
    assert (
        ok["status"] == "dry_run"
        and "deploy uat --dry-run" in text
        and "v1" in text
        and "env file linked" in text
    )
    bad = await deploys.run(repo, "prod", "feat/broken", False, tmp_path / "logs")
    assert bad["status"] == "failed" and bad["error"] == "error: build failed at step 3"
    assert (tmp_path / ".agw-deploy/CoinGame/.git").exists()
    assert (repo / "wip.txt").exists()  # the project's own tree is untouched
    assert subprocess.run(["git", "-C", str(repo), "branch", "--show-current"], capture_output=True,
                          text=True).stdout.strip() == "main"  # fmt: skip
    unknown = await deploys.run(repo, "uat", "no-such-branch", True, tmp_path / "logs")
    assert unknown["status"] == "failed" and "unknown branch" in unknown["error"]


async def test_telegram_deploy_flow(settings, cfg_dir, tmp_path, monkeypatch):
    game(tmp_path)
    monkeypatch.setattr("app.projects.projects_root", lambda: tmp_path)
    orch, _ = make(settings, cfg_dir, [])
    gw = gateway(orch)
    reports = []

    async def report(d):
        reports.append(d)
        return d

    monkeypatch.setattr(orch.collab, "report_deployment", report)
    answers, alerts = [], []
    await gw.on_deploy(tg(answers))
    assert [d for _, d in buttons(answers[-1][1])] == ["d:p:0"]
    await gw.on_deploy_button(press("d:p:0", answers, alerts))
    assert [d for _, d in buttons(answers[-1][1])] == [
        "d:e:0:uat",
        "d:e:0:uat-tf",
        "d:e:0:prod",
        "d:e:0:prod-submit",
    ]
    await gw.on_deploy_button(press("d:e:0:prod", answers, alerts))
    assert buttons(answers[-1][1])[0] == ("main", "d:b:0:prod:0")
    await gw.on_deploy_button(press("d:b:0:prod:0", answers, alerts))
    await gw.on_deploy_button(press("d:m:0:prod:0:real", answers, alerts))
    confirm, kb = answers[-1]
    assert "PRODUCTION" in confirm and "deploy.sh prod</code>" in confirm
    go = buttons(kb)[0][1]
    await gw.on_deploy_button(press(go, answers, alerts, user=STRANGER))
    assert alerts[-1] == "Not authorized." and not reports
    await gw.on_deploy_button(press(go, answers, alerts))
    assert "Deploy started" in answers[-1][0] and reports[0].status == "started"
    for _ in range(100):
        if len(reports) == 2:
            break
        await asyncio.sleep(0.1)
    assert (
        reports[1].status == "succeeded" and reports[1].environment == "prod" and reports[1].commit
    )
    await gw.on_deploy_button(press(go, answers, alerts))  # a used confirmation cannot run twice
    assert "expired" in answers[-1][0]


async def test_store_targets_pass_their_flags(tmp_path, monkeypatch):
    """/deploy targets: UAT TestFlight and App Store review add the flags the project scripts follow."""
    import subprocess as sp

    from app import deploys

    repo = tmp_path / "game"
    (repo / "4_delivery/deploy").mkdir(parents=True)
    (repo / "4_delivery/deploy/deploy.sh").write_text('#!/usr/bin/env bash\necho "args: $*"\n')
    sp.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    sp.run(["git", "-C", str(repo), "add", "-A"], check=True)
    sp.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"],
        check=True,
    )
    for target, expected in (
        ("uat-tf", "uat --testflight --dry-run"),
        ("prod-submit", "prod --submit --dry-run"),
    ):
        r = await deploys.run(repo, target, "main", True, tmp_path / "logs")
        assert r["status"] == "dry_run" and f"args: {expected}" in r["log"].read_text()


async def test_typed_deploy_command_goes_to_confirmation(settings, cfg_dir, tmp_path, monkeypatch):
    from types import SimpleNamespace

    game(tmp_path)
    monkeypatch.setattr("app.projects.projects_root", lambda: tmp_path)
    orch, _ = make(settings, cfg_dir, [])
    gw = gateway(orch)
    answers = []
    await gw.on_deploy(tg(answers), SimpleNamespace(args="coin testflight main dry"))
    confirm, kb = answers[-1]
    assert (
        "UAT · TestFlight" in confirm and "deploy.sh uat --testflight --dry-run</code>" in confirm
    )
    assert buttons(kb)[0][1].startswith("d:go:")
    await gw.on_deploy(tg(answers), SimpleNamespace(args="coin review main real"))
    assert "deploy.sh prod --submit</code>" in answers[-1][0] and "PRODUCTION" in answers[-1][0]
    await gw.on_deploy(tg(answers), SimpleNamespace(args="coin moon"))
    assert "Unknown target" in answers[-1][0]


async def test_typed_deploy_asks_what_is_missing(settings, cfg_dir, tmp_path, monkeypatch):
    from types import SimpleNamespace

    game(tmp_path)
    monkeypatch.setattr("app.projects.projects_root", lambda: tmp_path)
    orch, _ = make(settings, cfg_dir, [])
    gw = gateway(orch)
    answers = []
    await gw.on_deploy(
        tg(answers), SimpleNamespace(args="coin")
    )  # target buttons, short, 2 per row
    assert [b for b, _ in buttons(answers[-1][1])] == [
        "🧪 Firebase",
        "✈️ TestFlight",
        "🌍 App Store",
        "🏁 Review",
    ]
    await gw.on_deploy(tg(answers), SimpleNamespace(args="coin appstore"))  # branch buttons
    assert buttons(answers[-1][1])[0] == ("main", "d:b:0:prod:0")
    await gw.on_deploy(tg(answers), SimpleNamespace(args="coin appstore main"))  # dry run or real
    assert [d for _, d in buttons(answers[-1][1])] == ["d:m:0:prod:0:dry", "d:m:0:prod:0:real"]
