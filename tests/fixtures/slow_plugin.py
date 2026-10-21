"""Test-only plugin used by the crash/restart recovery test."""

import asyncio
import os
from pathlib import Path

from app.engine import StepOutput
from app.plugins.base import OperationSpec, PlanResult, PluginManifest
from app.workflow import StepDef, WorkflowDef


class SlowPlugin:
    manifest = PluginManifest(
        id="slowtest",
        name="Slow test",
        version="0.0.1",
        description="recovery fixture",
        operations=[
            OperationSpec(task_type="idem", description="idempotent slow step"),
            OperationSpec(task_type="side", description="non-idempotent side effect"),
        ],
    )

    def classify(self, req, attachments):
        return None

    def plan(self, req, task_type, env):
        steps = [
            StepDef(id="first", executor="plugin", ref="slowtest.quick"),
            StepDef(
                id="main", executor="plugin", ref=f"slowtest.{task_type}", depends_on=["first"]
            ),
        ]
        return PlanResult(WorkflowDef(id=f"slowtest.{task_type}", domain="slowtest", steps=steps))

    def tools(self, services):
        return []

    def idempotent_operations(self):
        return {"quick", "idem"}

    def operations(self):
        return {"quick": self.quick, "idem": self.idem, "side": self.side}

    def validators(self):
        return {}

    async def quick(self, ctx, inputs):
        Path(os.environ["MARKER_DIR"], "quick.log").open("a").write("x\n")
        return StepOutput({"ok": True})

    async def idem(self, ctx, inputs):
        Path(os.environ["MARKER_DIR"], "idem.log").open("a").write("x\n")
        await asyncio.sleep(float(os.environ.get("SLOW_S", "0")))
        return StepOutput({"ok": True})

    async def side(self, ctx, inputs):
        Path(os.environ["MARKER_DIR"], "side.log").open("a").write("charged\n")
        await asyncio.sleep(float(os.environ.get("SLOW_S", "0")))
        return StepOutput({"ok": True})


def create_plugin():
    return SlowPlugin()
