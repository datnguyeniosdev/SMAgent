"""Independent reviews and research with the game-review skill (run.sh), in the project's folder.

Each run is a fresh read-only agent CLI (claude, droid, codex, gemini, opencode) that writes Markdown
reports into the project (`4_delivery/uat/reviews` by default). The gateway starts it in the background
without its own secrets, tells the operator on Telegram, and keeps the result for get_review."""

from __future__ import annotations

import asyncio
import logging
import re
from html import escape
from pathlib import Path

from app.models import new_id, now

log = logging.getLogger(__name__)

ROLES = ("qa", "po", "both")
MODES = ("benchmark", "parity", "playtest", "ideation")
AGENTS = ("claude", "droid", "codex", "gemini", "opencode", "all")
TIMEOUT_S = 45 * 60


class ReviewError(ValueError):
    pass


def find_script(repo: Path) -> Path | None:
    """The project's own copy first, then the user's installed skill."""
    for base in (repo / ".factory/skills", repo / ".claude/skills", Path.home() / ".factory/skills",
                 Path.home() / ".claude/skills"):  # fmt: skip
        if (script := base / "game-review/run.sh").is_file():
            return script
    return None


class ReviewService:
    def __init__(self, svc):
        self.svc = svc
        self._jobs: dict[str, asyncio.Task] = {}

    def get(self, review_id: str) -> dict | None:
        r = self.svc.store.get("review", review_id)
        if (
            r and r["state"] == "running" and review_id not in self._jobs
        ):  # the gateway restarted under it
            r.update(
                state="failed",
                files=[],
                problems=["the gateway restarted during the review; run it again"],
            )
            self._save(r)
        return r

    def _save(self, r: dict) -> dict:
        self.svc.store.put("review", r["review_id"], r, state=r["state"])
        return r

    def start(self, project: str, mode: str, role: str = "qa", agent: str = "claude", model: str = "",
              evidence: str = "", ref: str = "") -> dict:  # fmt: skip
        hub = self.svc.channel
        name = hub.resolve_project(project) or project
        folder = hub.project_dir(name)
        if folder is None or not Path(folder).is_dir():
            raise ReviewError(
                f"no folder known for project {project!r}: open a session there first (see list_agents)"
            )
        if mode not in MODES or role not in ROLES or agent not in AGENTS:
            raise ReviewError(
                f"mode: {'|'.join(MODES)}; role: {'|'.join(ROLES)}; agent: {'|'.join(AGENTS)}"
            )
        if model and not re.fullmatch(r"[\w.\-/:@]+", model):
            raise ReviewError("model: a model id, e.g. gpt-5.6-sol")
        if evidence and not Path(evidence).is_dir():
            raise ReviewError(f"evidence must be an existing folder of screenshots: {evidence}")
        script = find_script(Path(folder))
        if script is None:
            raise ReviewError(
                "the game-review skill is not installed (~/.claude/skills/game-review/run.sh)"
            )
        busy = [r for r in self.svc.store.list("review", states=["running"])
                if r["folder"] == folder and r["review_id"] in self._jobs]  # fmt: skip
        if busy:
            raise ReviewError(
                f"{busy[0]['review_id']} is still running for this project; wait for it"
            )
        argv = [str(script), "--role", role, "--mode", mode, "--agent", agent]
        for flag, value in (
            ("--model", model),
            ("--evidence", evidence),
            ("--ref", ref.strip()[:500]),
        ):
            if value:
                argv += [flag, value]
        r = self._save({
            "review_id": new_id("rev"), "project": name, "folder": folder, "mode": mode, "role": role,
            "agent": agent, "model": model, "state": "running", "started_at": now().isoformat(), "argv": argv,
        })  # fmt: skip
        self._jobs[r["review_id"]] = asyncio.get_running_loop().create_task(self._run(r))
        self.svc.store.event("review.started", review_id=r["review_id"], project=name, mode=mode)
        return r

    async def _run(self, r: dict) -> None:
        from app.deploys import deploy_env

        what = f"{r['role']}/{r['mode']} on {escape(r['agent'])}"
        await self._tell(
            f"🔎 <b>Review started</b> · {escape(r['project'])}\n{what} · <code>{r['review_id']}</code>"
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *r["argv"], cwd=r["folder"], env=deploy_env(), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )  # fmt: skip
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), TIMEOUT_S)
            except TimeoutError:
                proc.kill()
                out = b"[fail] timed out"
            text = out.decode(errors="replace")
            files = re.findall(r"^\[ok\] (.+\.md)$", text, re.M)
            failed = re.findall(r"^\[(?:fail|skip)\] (.+)$", text, re.M)
            r.update(state="done" if files else "failed", files=files, problems=failed[:10],
                     output=text[-2000:], finished_at=now().isoformat())  # fmt: skip
        except Exception as e:  # noqa: BLE001 - a review failure is reported, never raised into the loop
            log.exception("review %s failed", r["review_id"])
            r.update(
                state="failed", files=[], problems=[str(e)[:300]], finished_at=now().isoformat()
            )
        self._save(r)
        self._jobs.pop(r["review_id"], None)
        self.svc.store.event("review.finished", review_id=r["review_id"], state=r["state"])
        names = (
            "\n".join(f"• <code>{escape(Path(f).name)}</code>" for f in r["files"]) or "no report"
        )
        problems = "\n".join(f"⚠️ {escape(p)}" for p in r.get("problems", []))
        icon = "✅" if r["state"] == "done" else "❌"
        await self._tell(f"{icon} <b>Review {r['state']}</b> · {escape(r['project'])} · <code>{r['review_id']}</code>\n"
                         f"{names}" + (f"\n{problems}" if problems else ""))  # fmt: skip

    async def _tell(self, html: str) -> None:
        fn = getattr(getattr(self.svc, "notifier", None), "send_control", None)
        if fn is None:
            return
        try:
            await fn(html)
        except Exception:  # noqa: BLE001 - the review runs whether or not Telegram took the notice
            log.exception("review notice failed")
