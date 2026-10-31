"""Runs only pre-configured QA commands (no shell, no model-supplied commands).

mode "none" is a plain subprocess with a scrubbed environment and timeout: it is
NOT a security sandbox. mode "docker" runs the command in a container with no
network and only the worktree mounted.
"""

import asyncio
import os
import shlex
import shutil
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from app.config import child_env
from app.models import new_id, now

_ENV_KEEP = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")


@dataclass
class QAResult:
    name: str
    command: str
    cwd: str
    status: Literal["passed", "failed", "timed_out", "skipped"]
    exit_code: int | None
    duration_s: float
    stdout: str = ""
    stderr: str = ""
    task_id: str | None = None
    run_id: str = field(default_factory=lambda: new_id("qa"))
    started_at: datetime = field(default_factory=now)
    finished_at: datetime | None = None
    artifact_ids: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    @property
    def timed_out(self) -> bool:
        return self.status == "timed_out"

    @property
    def output_tail(self) -> str:
        return self.stdout + (f"\n[stderr]\n{self.stderr}" if self.stderr else "")


class _Tail:
    def __init__(self, limit: int):
        self.buf = bytearray()
        self.limit = limit

    async def pump(self, stream):
        while chunk := await stream.read(8192):
            self.buf.extend(chunk)
            if len(self.buf) > self.limit:
                del self.buf[: len(self.buf) - self.limit]  # keep the tail

    def text(self) -> str:
        return self.buf.decode(errors="replace")


class QARunner:
    def __init__(
        self,
        commands: dict[str, str],
        timeout_s: float = 900,
        max_output_bytes: int = 64_000,
        sandbox: str = "none",
        docker_image: str | None = None,
        env_passthrough: tuple[str, ...] = (),
    ):
        self.env_keep = _ENV_KEEP + tuple(env_passthrough)
        self.commands = commands
        self.timeout_s = timeout_s
        self.max_output = max_output_bytes
        self.sandbox = sandbox
        self.docker_image = docker_image

    def argv(self, name: str, cwd: Path) -> list[str]:
        if name not in self.commands:
            raise KeyError(f"QA command {name!r} is not configured")
        argv = shlex.split(self.commands[name])
        if self.sandbox == "docker":
            mount = f"{cwd.resolve()}:/work"
            return [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--memory",
                "2g",
                "-v",
                mount,
                "-w",
                "/work",
                self.docker_image,
                *argv,
            ]
        return argv

    async def run(self, name: str, cwd: Path, task_id: str | None = None) -> QAResult:
        argv = self.argv(name, cwd)
        res = QAResult(name, " ".join(argv), str(cwd), "skipped", None, 0.0, task_id=task_id)
        exe = shutil.which(argv[0])
        if exe is None:  # required runtime missing: never report this as passed
            res.stderr = f"executable not found: {argv[0]}"
            res.finished_at = now()
            return res
        env = {k: v for k, v in child_env().items() if k in self.env_keep}
        start = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            exe,
            *argv[1:],
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        out, err = _Tail(self.max_output), _Tail(self.max_output)

        async def collect():
            await asyncio.gather(out.pump(proc.stdout), err.pump(proc.stderr))
            await proc.wait()

        try:
            await asyncio.wait_for(collect(), self.timeout_s)
            res.status = "passed" if proc.returncode == 0 else "failed"
        except asyncio.CancelledError:  # never leave the process running after cancellation
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            raise
        except TimeoutError:
            res.status = "timed_out"
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
        res.exit_code = proc.returncode
        res.stdout, res.stderr = out.text(), err.text()
        res.duration_s = round(time.monotonic() - start, 2)
        res.finished_at = now()
        return res

    async def run_all(self, cwd: Path, task_id: str | None = None) -> list[QAResult]:
        return [await self.run(name, cwd, task_id) for name in self.commands]
