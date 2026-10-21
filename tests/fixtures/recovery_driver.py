"""Runs a real gateway process for the recovery test. phase=crash kills itself mid-workflow."""

import asyncio
import json
import os
import sys
from pathlib import Path

from app.config import Settings
from app.intake import TaskRequest
from app.main import build


async def main(phase: str, data_dir: str):
    s = Settings(
        _env_file=None,
        data_dir=Path(data_dir),
        plugins=["tests.fixtures.slow_plugin"],
        config_dir=None,
    )
    o = build(s)
    marker = Path(os.environ["MARKER_DIR"])
    if phase == "crash":
        await o.start()
        a = await o.submit_request(
            TaskRequest(objective="idem", domain="slowtest", task_type="idem")
        )
        b = await o.submit_request(
            TaskRequest(objective="side", domain="slowtest", task_type="side")
        )
        for _ in range(200):
            if (marker / "idem.log").exists() and (marker / "side.log").exists():
                break
            await asyncio.sleep(0.05)
        print(json.dumps({"idem": a.task_id, "side": b.task_id}), flush=True)
        os._exit(9)  # hard crash: no cleanup, no cancellation handlers
    report = await o.recover()
    for t in list(o._running.values()):
        await asyncio.wait_for(asyncio.shield(t), 30)
    out = {"recovery": report, "tasks": {}}
    for tid, t in o.tasks.items():
        if t.run_id:
            out["tasks"][tid] = {
                "state": t.state,
                "steps": {r.step_id: r.state for r in o.load_steps(t.run_id).values()},
            }
    print(json.dumps(out, default=str), flush=True)


asyncio.run(main(sys.argv[1], sys.argv[2]))
