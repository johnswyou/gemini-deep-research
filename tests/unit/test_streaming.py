"""Golden-file tests for the SSE stream aggregator.

Regression protection on ``core/streaming.py`` — the module with the
highest churn risk, since the exact event shape emitted by the SDK is
controlled outside gdr. Fixtures live in ``tests/fixtures/streams/*.jsonl``
and can be updated (carefully) when the documented event model changes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gdr.core.streaming import (
    StreamAggregator,
    StreamEvent,
)
from gdr.errors import StreamError

FIXTURES = Path(__file__).parent.parent / "fixtures" / "streams"


def _load_fixture(name: str) -> list[dict[str, Any]]:
    path = FIXTURES / name
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped:
                continue
            events.append(json.loads(stripped))
    return events


def _collect(fixture: str) -> tuple[StreamAggregator, list[StreamEvent]]:
    emitted: list[StreamEvent] = []
    agg = StreamAggregator(on_event=emitted.append)
    for ev in _load_fixture(fixture):
        agg.feed(ev)
    return agg, emitted


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_interaction_id_captured_on_start(self) -> None:
        agg, _ = _collect("happy_path.jsonl")
        assert agg.interaction_id == "int-happy-001"

    def test_status_reflects_final_state(self) -> None:
        agg, _ = _collect("happy_path.jsonl")
        assert agg.status == "completed"

    def test_snapshot_reports_clean_completion(self) -> None:
        agg, _ = _collect("happy_path.jsonl")
        snap = agg.snapshot()
        assert snap.completed_cleanly is True

    def test_text_chunks_are_concatenated(self) -> None:
        agg, _ = _collect("happy_path.jsonl")
        snap = agg.snapshot()
        assert "# Research Report" in snap.text
        assert "custom ASIC" in snap.text

    def test_thoughts_collected(self) -> None:
        agg, _ = _collect("happy_path.jsonl")
        snap = agg.snapshot()
        assert snap.thoughts == ["Let me start by searching for recent papers."]

    def test_emission_sequence(self) -> None:
        _, events = _collect("happy_path.jsonl")
        kinds = [e.kind for e in events]
        # Start + content_start + thought + content_stop + content_start + 3x text_delta + content_stop + complete
        assert kinds[0] == "start"
        assert kinds[-1] == "complete"
        assert kinds.count("thought") == 1
        assert kinds.count("text_delta") == 3


# ---------------------------------------------------------------------------
# Mid-stream error
# ---------------------------------------------------------------------------


class TestMidStreamError:
    def test_error_event_raises_stream_error(self) -> None:
        agg = StreamAggregator()
        events = _load_fixture("mid_stream_error.jsonl")
        with pytest.raises(StreamError) as excinfo:
            for ev in events:
                agg.feed(ev)
        # Message should carry both the code and the human-readable message.
        assert "RATE_LIMITED" in str(excinfo.value)
        assert "Quota exceeded" in str(excinfo.value)

    def test_error_does_not_discard_partial_text(self) -> None:
        # The caller is responsible for ignoring the partial snapshot on
        # error; we just check that what we already saw is preserved.
        emitted: list[StreamEvent] = []
        agg = StreamAggregator(on_event=emitted.append)
        events = _load_fixture("mid_stream_error.jsonl")
        with pytest.raises(StreamError):
            for ev in events:
                agg.feed(ev)
        snap = agg.snapshot()
        assert snap.text == "Starting the research..."
        assert snap.completed_cleanly is False


# ---------------------------------------------------------------------------
# Out of order (defensive)
# ---------------------------------------------------------------------------


class TestOutOfOrder:
    def test_delta_before_start_creates_builder_and_buffers(self) -> None:
        agg, _events = _collect("out_of_order.jsonl")
        snap = agg.snapshot()
        # Both deltas' text should have made it into the snapshot even
        # though the first arrived before content.start.
        assert "Delta arrived without a preceding content.start." in snap.text
        assert "Subsequent chunks still append." in snap.text

    def test_no_data_loss_on_out_of_order_events(self) -> None:
        _agg, events = _collect("out_of_order.jsonl")
        text_events = [e for e in events if e.kind == "text_delta"]
        assert len(text_events) == 2


# ---------------------------------------------------------------------------
# Disconnect mid-text
# ---------------------------------------------------------------------------


class TestDisconnectMidText:
    def test_snapshot_marks_not_completed_cleanly(self) -> None:
        agg, _ = _collect("disconnect_mid_text.jsonl")
        assert agg.snapshot().completed_cleanly is False

    def test_interaction_id_still_captured(self) -> None:
        agg, _ = _collect("disconnect_mid_text.jsonl")
        # This is the whole point of capturing on interaction.start — the
        # caller needs the id to poll for the authoritative result.
        assert agg.interaction_id == "int-disc-004"

    def test_partial_text_buffered_but_flagged_incomplete(self) -> None:
        agg, _ = _collect("disconnect_mid_text.jsonl")
        snap = agg.snapshot()
        # We have a partial text view, but completed_cleanly is False so
        # the caller knows to prefer the polled result.
        assert "Partial report" in snap.text
        assert snap.completed_cleanly is False


# ---------------------------------------------------------------------------
# Interleaved thought / image / text
# ---------------------------------------------------------------------------


class TestInterleaved:
    def test_multiple_thoughts_preserved_in_order(self) -> None:
        agg, _ = _collect("thought_image_text.jsonl")
        assert agg.snapshot().thoughts == [
            "Analyzing market share data.",
            "Finalizing report.",
        ]

    def test_image_chunks_concatenated(self) -> None:
        agg, _ = _collect("thought_image_text.jsonl")
        images = agg.snapshot().images
        assert len(images) == 1
        assert images[0] == "iVBORw0KGgoAAAAN" + "SUhEUgAAAAEAAAAB"

    def test_text_spans_image_interruption(self) -> None:
        agg, _ = _collect("thought_image_text.jsonl")
        snap = agg.snapshot()
        # Both pre- and post-image text chunks should be in the buffer, in
        # arrival order.
        assert "Intro paragraph." in snap.text
        assert "more text after the image." in snap.text
        assert snap.text.index("Intro paragraph") < snap.text.index("after the image")

    def test_image_emission_carries_data(self) -> None:
        _, emissions = _collect("thought_image_text.jsonl")
        image_emissions = [e for e in emissions if e.kind == "image"]
        assert len(image_emissions) == 2
        assert image_emissions[0].image_data == "iVBORw0KGgoAAAAN"


# ---------------------------------------------------------------------------
# Callback invariants
# ---------------------------------------------------------------------------


class TestEmissionInvariants:
    def test_every_fixture_produces_a_start_event_first(self) -> None:
        for fixture in (
            "happy_path.jsonl",
            "out_of_order.jsonl",
            "disconnect_mid_text.jsonl",
            "thought_image_text.jsonl",
        ):
            _, events = _collect(fixture)
            assert events, f"no emissions for {fixture}"
            assert events[0].kind == "start"

    def test_unknown_event_type_is_silently_ignored(self) -> None:
        agg = StreamAggregator()
        # Should not raise.
        agg.feed({"event_type": "some.future.event.type", "payload": {}})
        assert agg.interaction_id is None
