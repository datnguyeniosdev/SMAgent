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
    assert [d for _, d in buttons(answers[-1][1])] == ["d:e:0:uat", "d:e:0:prod"]
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
