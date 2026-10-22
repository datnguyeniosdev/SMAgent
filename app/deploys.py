"""Project deploys started from Telegram (/deploy): the project's own deploy script
(4_delivery/deploy/deploy.sh <uat|prod> [--dry-run]) run in a separate deploy worktree checked out at
the chosen branch. The project's working tree, and any uncommitted work in it, is never touched; the
worktree is reused, so the engine's import cache survives between deploys. Gitignored settings the script
needs (4_delivery/deploy/.env.deploy) are linked from the project. One deploy per project at a time;
the outcome (exit code) is reported with report_deployment and the full log is kept under var/deploys.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

from app.repo_setup import DEPLOY_SCRIPT

ENVS = ("uat", "prod")
TIMEOUT_S = 3 * 3600  # a cold engine import plus two platform builds can take long
LINKED = ("4_delivery/deploy/.env.deploy",)  # gitignored, needed by the script
_running: set[str] = set()


def deployable(root: Path) -> list[Path]:
    from app.launcher import project_dirs

    return [d for d in project_dirs(root) if (d / DEPLOY_SCRIPT).is_file()]


def _git(repo: Path, *args: str, timeout: int = 120) -> str:
    r = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout
    )
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[-300:] or f"git {args[0]} failed")
    return r.stdout.strip()


def branches(repo: Path, limit: int = 12) -> list[str]:
    """Local and remote branches, most recently committed first."""
    out = _git(repo, "for-each-ref", "--sort=-committerdate", "--format=%(refname:short)",
               "refs/heads", "refs/remotes")  # fmt: skip
    remotes = set(_git(repo, "remote").split())  # origin/HEAD is listed as plain "origin"
    names = [b for b in out.splitlines() if b and b not in remotes and not b.endswith("/HEAD")]
    main = [
        b
        for b in ("main", "master", "develop", "origin/main", "origin/master", "origin/develop")
        if b in names
    ]
    return list(dict.fromkeys(main + names))[:limit]


def worktree_dir(repo: Path) -> Path:
    return repo.parent / ".agw-deploy" / repo.name


def prepare(repo: Path, branch: str) -> tuple[Path, str]:
    """The deploy worktree, checked out (detached) at branch -> (folder, short commit)."""
    if branch not in branches(repo, limit=500):
        raise ValueError(f"unknown branch {branch!r}")
    if _git(repo, "remote"):
        _git(repo, "fetch", "--prune", timeout=300)
    wt = worktree_dir(repo)
    if not (wt / ".git").exists():
        wt.parent.mkdir(parents=True, exist_ok=True)
        _git(repo, "worktree", "add", "--detach", str(wt), branch, timeout=600)
    else:
        _git(wt, "checkout", "--detach", "--force", branch, timeout=600)
        _git(wt, "clean", "-fd")  # untracked leftovers go; ignored caches (engine imports) stay
    for rel in LINKED:
        src, dst = repo / rel, wt / rel
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.symlink_to(src)
    return wt, _git(wt, "rev-parse", "--short", "HEAD")


def busy(repo: Path) -> bool:
    return str(repo) in _running


async def run(repo: Path, env: str, branch: str, dry_run: bool, log_dir: Path) -> dict:
    """Run the deploy -> {status, commit, log, error, seconds}. status: succeeded | dry_run | failed."""
    if env not in ENVS:
        raise ValueError(f"environment must be one of {ENVS}")
    if busy(repo):
        raise ValueError(f"{repo.name} is already deploying")
    _running.add(str(repo))
    loop = asyncio.get_running_loop()
    start = loop.time()
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / f"{repo.name}-{env}-{int(start)}.log"
    commit, rc, error = "", 1, ""
    try:
        try:
            wt, commit = await asyncio.to_thread(prepare, repo, branch)
        except (RuntimeError, ValueError, subprocess.TimeoutExpired) as e:
            log.write_text(f"preparing the deploy worktree failed: {e}\n")
            return {"status": "failed", "commit": "", "log": log, "error": f"checkout failed: {e}",
                    "seconds": int(loop.time() - start)}  # fmt: skip
        args = [DEPLOY_SCRIPT, env, *(["--dry-run"] if dry_run else [])]
        environ = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        with log.open("wb") as out:
            proc = await asyncio.create_subprocess_exec(
                "bash", *args, cwd=wt, stdout=out, stderr=subprocess.STDOUT, env=environ
            )
            try:
                rc = await asyncio.wait_for(proc.wait(), TIMEOUT_S)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                error = f"timed out after {TIMEOUT_S // 60} min"
        if rc != 0 and not error:
            lines = [ln for ln in log.read_text(errors="replace").splitlines() if ln.strip()]
            error = (lines[-1] if lines else f"exit code {rc}")[:300]
        status = ("dry_run" if dry_run else "succeeded") if rc == 0 else "failed"
        return {"status": status, "commit": commit, "log": log, "error": error,
                "seconds": int(loop.time() - start)}  # fmt: skip
    finally:
        _running.discard(str(repo))
