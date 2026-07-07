"""Opt-in live smoke test against the real Gemini Interactions API.

Skipped unless ``RUN_LIVE_TESTS=1`` (see conftest.py). Requires
``GEMINI_API_KEY``. Deliberately uses a cheap plain model — NOT a Deep
Research agent — so a run costs a fraction of a cent and finishes in
seconds, while still proving the production-facing seams the mocked
suite cannot:

* ``interactions.create()`` accepts our kwargs live (``store=True``,
  ``background=False`` — plain models reject background interactions —
  ``model=``, plain-string input);
* ``interactions.get()`` polling reaches a terminal status;
* the response adapter extracts text from whatever shape the live API
  actually returns (the empty-``outputs`` question behind v0.1.2).

A full Deep Research validation (real `gdr research`, `--model`
follow-up, stream reconnect) is a manual checklist — see the release
runbook — since those runs cost dollars and minutes.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gdr.core.client import GdrClient
from gdr.core.models import RunContext
from gdr.core.normalize import interaction_id_of, normalized_outputs
from gdr.core.rendering import build_report_text
from gdr.core.requests import build_create_kwargs
from gdr.core.security import SecurityPolicy
from gdr.ui.progress import poll_until_complete

# Cheap, fast, always-on model for smoke purposes.
_SMOKE_MODEL = "gemini-2.5-flash-lite"


@pytest.mark.live
def test_live_model_interaction_round_trip(tmp_path: Path) -> None:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        pytest.skip("GEMINI_API_KEY not set")

    client = GdrClient(api_key=api_key)

    ctx = RunContext(
        query="Reply with the single word: pong",
        agent=_SMOKE_MODEL,
        model=_SMOKE_MODEL,
        output_dir=tmp_path,
        stream=False,
    )
    kwargs, stripped = build_create_kwargs(ctx, SecurityPolicy(output_root=tmp_path))
    assert stripped == []
    # The exact kwargs gdr would send for a --model follow-up, minus
    # previous_interaction_id.
    created = client.interactions.create(**kwargs)
    interaction_id = interaction_id_of(created)
    assert interaction_id, "live create() returned no interaction id"

    interaction = poll_until_complete(
        client.interactions.get,
        interaction_id,
        timeout_seconds=180,
    )

    # The adapter must find renderable text in the LIVE response shape.
    outputs = normalized_outputs(interaction)
    assert outputs, (
        "live terminal interaction produced no normalized outputs — "
        "the response shape may have drifted; inspect the raw interaction"
    )
    assert "pong" in build_report_text(interaction).lower()
