"""Shared pytest fixtures for the gdr test suite.

Live tests are gated by the `live` marker and the `RUN_LIVE_TESTS` env var — see
the `markers` section of `pyproject.toml`. As more modules land, fixtures for
the fake SDK client, config overrides, and SSE event streams will live here.
"""

from __future__ import annotations
