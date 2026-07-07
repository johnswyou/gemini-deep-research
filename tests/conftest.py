"""Shared pytest configuration for the gdr test suite.

Live tests (marked ``@pytest.mark.live``) hit the real Gemini API: they
need ``GEMINI_API_KEY`` and spend (a small amount of) quota. They are
skipped unless ``RUN_LIVE_TESTS=1`` is set, so `pytest -q` and CI stay
hermetic by default:

    RUN_LIVE_TESTS=1 GEMINI_API_KEY=... uv run pytest -m live -q
"""

from __future__ import annotations

import os

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("RUN_LIVE_TESTS") == "1":
        return
    skip_live = pytest.mark.skip(
        reason="live API test — set RUN_LIVE_TESTS=1 (and GEMINI_API_KEY) to run"
    )
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)
