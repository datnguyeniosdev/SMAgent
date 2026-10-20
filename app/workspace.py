"""Isolated git worktrees per task. One writer per worktree; never merges."""

import asyncio
from dataclasses import dataclass
from pathlib import Path


class WorkspaceError(Exception):
    pass


@dataclass
class Workspace:
    task_id: str
    path: Path
    branch: str
    base_commit: str


async def git(cwd: Path, *args: str, check: bool = True) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if check and proc.returncode != 0:
        raise WorkspaceError(f"git {args[0]} failed: {err.decode(errors='replace')[-300:]}")
    return out.decode(errors="replace")


class WorkspaceManager:
    def __init__(self, project_root: Path | None, worktrees_dir: Path):
        self.project_root = project_root
        self.worktrees_dir = worktrees_dir
        self._writers: dict[Path, str] = {}  # worktree path -> owning task
        self._all: dict[str, Workspace] = {}

    @property
    def enabled(self) -> bool:
        return self.project_root is not None

    async def create(self, task_id: str) -> Workspace:
        if self.project_root is None:
            raise WorkspaceError("WORKSPACE_ROOT is not configured")
        if not task_id.replace("_", "").isalnum():
            raise WorkspaceError("invalid task id")
        top = (await git(self.project_root, "rev-parse", "--show-toplevel")).strip()
        if Path(top).resolve() != self.project_root:
            raise WorkspaceError("WORKSPACE_ROOT must be the top level of a git repository")
        base = (await git(self.project_root, "rev-parse", "HEAD")).strip()
        path = (self.worktrees_dir / task_id).resolve()
        if not path.is_relative_to(self.worktrees_dir.resolve()):
            raise WorkspaceError("worktree path escapes worktrees dir")
        branch = f"agw/{task_id}"
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        await git(self.project_root, "worktree", "add", "-b", branch, str(path), base)
        ws = Workspace(task_id, path, branch, base)
        self._all[task_id] = ws
        return ws

    def get(self, task_id: str) -> Workspace | None:
        return self._all.get(task_id)

    def acquire_write(self, ws: Workspace, owner: str) -> None:
        holder = self._writers.get(ws.path)
        if holder is not None and holder != owner:
            raise WorkspaceError(f"workspace {ws.task_id} is being written by {holder}")
        self._writers[ws.path] = owner

    def release_write(self, ws: Workspace, owner: str) -> None:
        if self._writers.get(ws.path) == owner:
            del self._writers[ws.path]

    def in_use(self, ws: Workspace) -> bool:
        return ws.path in self._writers

    async def diff(self, ws: Workspace) -> tuple[str, list[str]]:
        """Full diff against the base commit, including untracked files."""
        await git(ws.path, "add", "--intent-to-add", "--all")
        diff = await git(ws.path, "diff", ws.base_commit)
        names = await git(ws.path, "diff", "--name-only", ws.base_commit)
        return diff, [n for n in names.splitlines() if n]

    async def remove(self, task_id: str) -> None:
        """Remove a worktree. Fails (keeps data) if it has uncommitted changes or is in use."""
        ws = self._all.get(task_id)
        if ws is None:
            return
        if self.in_use(ws):
            raise WorkspaceError("workspace in use")
        await git(self.project_root, "worktree", "remove", str(ws.path))  # no --force
        del self._all[task_id]
