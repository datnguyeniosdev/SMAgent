"""Runs a real gateway process on the fake game. phase=crash kills itself while the runtime step runs
(inventory, evidence and coverage already checkpointed); phase=recover resumes and reports."""

import asyncio
import json
import os
import sys
from pathlib import Path

from app.config import Settings
from app.intake import TaskRequest
from app.main import build
from app.plugins import game_research


async def main(phase: str, data_dir: str, game: str):
    marker = Path(os.environ["MARKER_DIR"])
    if phase == "crash":
        real = game_research.GameResearchPlugin.op_runtime_plan

        async def slow(self, ctx, inputs):
            (marker / "runtime.log").open("a").write("x")
            await asyncio.sleep(30)
            return await real(self, ctx, inputs)

        game_research.GameResearchPlugin.op_runtime_plan = slow
    s = Settings(
        _env_file=None, data_dir=Path(data_dir), workspace_root=Path(game), config_dir=None
    )
    o = build(s)
    if phase == "crash":
        await o.start()
        t = await o.submit_request(
            TaskRequest(objective="research", domain="game_research", task_type="research")
        )
        for _ in range(400):
            done = (
                {r.step_id: r.state.value for r in o.load_steps(t.run_id).values()}
                if t.run_id
                else {}
            )
            ready = all(done.get(x) == "succeeded" for x in ("inventory", "evidence", "coverage"))
            if ready and (marker / "runtime.log").exists():
                break
            await asyncio.sleep(0.05)
        print(json.dumps({"task": t.task_id, "steps": done}), flush=True)
        os._exit(9)  # hard crash mid-step: no cleanup
    report = await o.recover()
    for t in list(o._running.values()):
        await asyncio.wait_for(asyncio.shield(t), 30)
    out = {"recovery": report, "tasks": {}}
    for tid, t in o.tasks.items():
        if not t.run_id:
            continue
        recs = o.load_steps(t.run_id).values()
        verified = []
        for a in o.artifacts.for_task(tid):
            o.artifacts.read(a)  # raises if any artifact is damaged
            verified.append(a.filename)
        out["tasks"][tid] = {
            "state": t.state,
            "steps": {r.step_id: r.state.value for r in recs},
            "attempts": {r.step_id: len(r.attempts) for r in recs},
            "outputs": {r.step_id: r.output for r in recs},
            "artifacts": sorted(verified),
        }
    print(json.dumps(out, default=str), flush=True)


asyncio.run(main(*sys.argv[1:4]))
