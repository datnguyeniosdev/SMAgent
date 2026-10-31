"""Project deploys started from Telegram (/deploy): the project's own deploy script
(4_delivery/deploy/deploy.sh <uat|prod> [--dry-run]) run in a separate deploy worktree checked out at
the chosen branch. The project's working tree, and any uncommitted work in it, is never touched; the
worktree is reused, so the engine's import cache survives between deploys. Gitignored settings the script
needs (4_delivery/deploy/.env.deploy) are linked from the project. One deploy per project at a time;
the outcome (exit code) is reported with report_deployment and the full log is kept under var/deploys.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import subprocess
from pathlib import Path

from app.repo_setup import DEPLOY_SCRIPT

ENVS = ("uat", "prod")
# What /deploy offers: target -> (environment, extra deploy.sh flags, label). The flags are the
# contract the project scripts follow; a script without iOS store support fails on them, visibly.
TARGETS = {
    "uat": ("uat", (), "UAT · Firebase"),
    "uat-tf": ("uat", ("--testflight",), "UAT · TestFlight"),
    "prod": ("prod", (), "PROD · App Store upload"),
    "prod-submit": ("prod", ("--submit",), "PROD · App Store + review"),
}
TIMEOUT_S = 3 * 3600  # a cold engine import plus two platform builds can take long
LINKED = ("4_delivery/deploy/.env.deploy",)  # gitignored, needed by the script
_running: set[str] = set()
_procs: dict[str, asyncio.subprocess.Process] = {}  # repo -> its deploy.sh, for /deploy stop


def deploy_env(environ: dict | None = None) -> dict:
    """The shell environment without the gateway's own secrets: every variable its .env sets, every
    key saved with /keys or named by the provider config, and AGW_* / TELEGRAM_*. The project's
    deploy script gets what it needs from its own .env.deploy or the operator's shell."""
    from dotenv import dotenv_values

    from app.config import child_env, env_file

    env = child_env(environ)
    drop = set(dotenv_values(env_file()))
    try:
        from app.config import get_settings
        from app.main import load_gateway_config

        cfg = load_gateway_config(get_settings())
        for p in cfg.providers.values() if cfg else []:
            drop |= {p.api_key_env, p.model_env} - {None}
    except Exception as e:  # noqa: BLE001 - without a config the prefix and .env rules still apply
        logging.getLogger(__name__).debug("provider config not read for the deploy env: %s", e)
    return {
        k: v for k, v in env.items() if k not in drop and not k.startswith(("AGW_", "TELEGRAM_"))
    }


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


def lock_file(repo: Path) -> Path:
    """Holds the running deploy.sh's pid, so a deploy left running across a gateway restart still
    blocks a second one in the same worktree."""
    return worktree_dir(repo).parent / f"{repo.name}.pid"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def orphan_pid(repo: Path) -> int | None:
    try:
        pid = int(lock_file(repo).read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if _alive(pid) else None


def busy(repo: Path) -> bool:
    return str(repo) in _running or orphan_pid(repo) is not None


def stop(repo: Path) -> bool:
    """/deploy stop: end the project's deploy (this gateway's, or one left from before a restart)."""
    proc = _procs.get(str(repo))
    pid = proc.pid if proc is not None else orphan_pid(repo)
    if pid is None:
        return False
    try:
        os.killpg(pid, signal.SIGTERM)  # the script and the builds it started
    except (ProcessLookupError, PermissionError):
        return False
    if proc is None:
        lock_file(repo).unlink(missing_ok=True)
    return True


async def run(repo: Path, env: str, branch: str, dry_run: bool, log_dir: Path) -> dict:
    """Run the deploy -> {status, commit, log, error, seconds}. status: succeeded | dry_run | failed."""
    if env not in TARGETS:
        raise ValueError(f"target must be one of {tuple(TARGETS)}")
    env_name, flags, _ = TARGETS[env]
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
        args = [DEPLOY_SCRIPT, env_name, *flags, *(["--dry-run"] if dry_run else [])]
        environ = deploy_env()
        with log.open("wb") as out:
            proc = await asyncio.create_subprocess_exec(
                "bash", *args, cwd=wt, stdout=out, stderr=subprocess.STDOUT, env=environ,
                start_new_session=True,  # its own process group: stop/timeout end the builds too
            )  # fmt: skip
            _procs[str(repo)] = proc
            lock_file(repo).write_text(str(proc.pid))
            try:
                rc = await asyncio.wait_for(proc.wait(), TIMEOUT_S)
            except (TimeoutError, asyncio.CancelledError) as e:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGTERM)
                await proc.wait()
                if isinstance(e, asyncio.CancelledError):
                    raise  # the gateway is stopping: the caller reports "interrupted"
                error = f"timed out after {TIMEOUT_S // 60} min"
            if rc in (-signal.SIGTERM, 128 + signal.SIGTERM) and not error:
                error = "stopped from Telegram (/deploy stop)"
        if rc != 0 and not error:
            lines = [ln for ln in log.read_text(errors="replace").splitlines() if ln.strip()]
            error = (lines[-1] if lines else f"exit code {rc}")[:300]
        status = ("dry_run" if dry_run else "succeeded") if rc == 0 else "failed"
        return {"status": status, "commit": commit, "log": log, "error": error,
                "seconds": int(loop.time() - start)}  # fmt: skip
    finally:
        _running.discard(str(repo))
        _procs.pop(str(repo), None)
        lock_file(repo).unlink(missing_ok=True)
