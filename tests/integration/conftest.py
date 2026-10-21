"""Opt-in real integration tests. Never run by default; never use mocks.

RUN_INTEGRATION=1 enables free real checks (local MCP servers, CLI versions, Telegram getMe).
RUN_PAID=1 additionally enables calls that may cost money (model/image generation).
Missing configuration -> pytest.skip("NOT CONFIGURED: ...") — never a pass.
"""

import os

import pytest


def pytest_collection_modifyitems(config, items):
    if os.environ.get("RUN_INTEGRATION") == "1":
        return
    skip = pytest.mark.skip(reason="SKIPPED: set RUN_INTEGRATION=1 to run real integration tests")
    for item in items:
        if "integration" in str(item.fspath):
            item.add_marker(skip)


def paid():
    if os.environ.get("RUN_PAID") != "1":
        pytest.skip("SKIPPED: paid call; set RUN_PAID=1")
