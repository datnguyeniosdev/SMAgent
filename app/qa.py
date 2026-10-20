"""Runs only pre-configured QA commands (no shell, no model-supplied commands).

mode "none" is a plain subprocess with a scrubbed environment and timeout: it is
NOT a security sandbox. mode "docker" runs the command in a container with no
network and only the worktree mounted.
"""

import asyncio
import os
import shlex
import signal
import time
from dataclasses import dataclass
from pathlib import Path

_ENV_KEEP = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "UNITY_PATH")


@dataclass
class QAResult:
    name: str
    command: str
    exit_code: int | None
    passed: bool
    timed_out: bool
    duration_s: float
    output_tail: str


class QARunner:
    def __init__(
        self,
        commands: dict[str, str],
        timeout_s: float = 900,
        max_output_bytes: int = 64_000,
        sandbox: str = "none",
        docker_image: str | None = None,
    ):
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
                "docker", "run", "--rm", "--network", "none", "--memory", "2g",
                "-v", mount, "-w", "/work", self.docker_image, *argv,
            ]  # fmt: skip
        return argv

    async def run(self, name: str, cwd: Path) -> QAResult:
        argv = self.argv(name, cwd)
        env = {k: v for k, v in os.environ.items() if k in _ENV_KEEP}
        start = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        buf = bytearray()

        async def pump():
            while chunk := await proc.stdout.read(8192):
                buf.extend(chunk)
                if len(buf) > self.max_output:
                    del buf[: len(buf) - self.max_output]  # keep the tail
            await proc.wait()

        timed_out = False
        try:
            await asyncio.wait_for(pump(), self.timeout_s)
        except TimeoutError:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
        return QAResult(
            name=name,
            command=" ".join(argv),
            exit_code=proc.returncode,
            passed=not timed_out and proc.returncode == 0,
            timed_out=timed_out,
            duration_s=round(time.monotonic() - start, 2),
            output_tail=buf.decode(errors="replace"),
        )

    async def run_all(self, cwd: Path) -> list[QAResult]:
        return [await self.run(name, cwd) for name in self.commands]
