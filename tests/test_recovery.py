"""Real process crash + restart against the SQLite store (no mocks)."""

import json
import os
import subprocess
import sys

import pytest


def run(phase, data_dir, marker, slow):
    env = {**os.environ, "MARKER_DIR": str(marker), "SLOW_S": slow, "PYTHONPATH": "."}
    p = subprocess.run(
        [sys.executable, "tests/fixtures/recovery_driver.py", phase, str(data_dir)],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    lines = [ln for ln in p.stdout.splitlines() if ln.startswith("{")]
    return p.returncode, json.loads(lines[-1]) if lines else None, p.stderr


@pytest.mark.timeout(120)  # two real gateway processes
def test_crash_and_restart_recovery(tmp_path):
    marker = tmp_path / "m"
    marker.mkdir()
    code, ids, err = run("crash", tmp_path / "var", marker, slow="30")
    assert code == 9, err[-2000:]
    assert (marker / "side.log").read_text() == "charged\n"
    code, out, err = run("recover", tmp_path / "var", marker, slow="0")
    assert code == 0, err[-2000:]
    idem, side = out["tasks"][ids["idem"]], out["tasks"][ids["side"]]
    # Idempotent in-flight step re-ran and finished; completed step was not repeated.
    assert idem["state"] == "succeeded" and idem["steps"] == {
        "first": "succeeded",
        "main": "succeeded",
    }
    assert (marker / "idem.log").read_text().count("x") == 2
    # Non-idempotent in-flight step is NOT replayed: outcome unknown, side effect happened once.
    assert side["state"] == "unknown" and side["steps"]["main"] == "unknown"
    assert (marker / "side.log").read_text() == "charged\n"
    assert (marker / "quick.log").read_text().count("x") == 2  # one per task, never re-run
    assert {r["action"] for r in out["recovery"]} == {"resumed workflow"}
