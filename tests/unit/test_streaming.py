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
    snapshot_outputs,
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
# Current Interactions API schema
# ---------------------------------------------------------------------------


class TestCurrentSchema:
    def test_interaction_id_captured_on_created(self) -> None:
        agg, _ = _collect("current_schema_happy_path.jsonl")
        assert agg.interaction_id == "int-current-001"

    def test_status_updates_and_completion_are_applied(self) -> None:
        agg, events = _collect("current_schema_happy_path.jsonl")
        assert agg.status == "completed"
        assert any(e.kind == "status" and e.status == "in_progress" for e in events)
        assert agg.snapshot().completed_cleanly is True

    def test_step_deltas_are_collected(self) -> None:
        agg, events = _collect("current_schema_happy_path.jsonl")
        snap = agg.snapshot()
        assert snap.thoughts == ["Reviewing current sources."]
        assert "# Current Report" in snap.text
        assert "step events" in snap.text
        assert [e.kind for e in events].count("text_delta") == 2

    def test_thought_signatures_are_ignored_without_dropping_thought_text(self) -> None:
        agg, events = _collect("current_schema_happy_path.jsonl")
        assert agg.snapshot().thoughts == ["Reviewing current sources."]
        assert not any(e.text == "opaque-signature" for e in events)


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
        # This is the whole point of capturing on interaction.created/start — the
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
            "current_schema_happy_path.jsonl",
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


# ---------------------------------------------------------------------------
# Image flush on completion (2026-07 review: snapshot is an artifact source)
# ---------------------------------------------------------------------------


class TestImageFlushOnComplete:
    def test_image_chunks_survive_missing_step_stop(self) -> None:
        agg = StreamAggregator()
        agg.feed({"event_type": "step.start", "index": 0, "step": {"type": "image"}})
        agg.feed(
            {"event_type": "step.delta", "index": 0, "delta": {"type": "image", "data": "aGk="}}
        )
        # No step.stop for index 0 — straight to completion.
        agg.feed(
            {
                "event_type": "interaction.completed",
                "interaction": {"id": "int-img-1", "status": "completed"},
            }
        )
        snapshot = agg.snapshot()
        assert snapshot.completed_cleanly is True
        assert snapshot.images == ["aGk="]


# ---------------------------------------------------------------------------
# Current-schema edge fixtures (parity with the legacy-schema goldens)
# ---------------------------------------------------------------------------


class TestCurrentSchemaEdgeFixtures:
    def test_error_event_raises_stream_error(self) -> None:
        events = _load_fixture("current_schema_mid_stream_error.jsonl")
        agg = StreamAggregator()
        with pytest.raises(StreamError) as excinfo:
            for event in events:
                agg.feed(event)
        assert "RATE_LIMITED" in str(excinfo.value)
        assert "Quota exceeded" in str(excinfo.value)
        # Everything before the error was still aggregated.
        assert agg.snapshot().text == "Starting the research..."

    def test_out_of_order_delta_is_not_dropped(self) -> None:
        agg, _events = _collect("current_schema_out_of_order.jsonl")
        snapshot = agg.snapshot()
        assert snapshot.completed_cleanly is True
        assert snapshot.text == (
            "Delta arrived without a preceding step.start. Subsequent chunks still append."
        )

    def test_disconnect_keeps_partial_state_without_clean_completion(self) -> None:
        agg, _events = _collect("current_schema_disconnect_mid_text.jsonl")
        snapshot = agg.snapshot()
        assert snapshot.completed_cleanly is False
        assert snapshot.interaction_id == "int-cur-disc-003"
        assert snapshot.text == "# Partial report that gets cut off mid-sentence"
        assert snapshot.thoughts == ["Reading source material."]
        # The resume point for a reconnect is the last event seen.
        assert agg.last_event_id == "evt-007"


# ---------------------------------------------------------------------------
# Streamed image MIME capture
# ---------------------------------------------------------------------------


class TestImageMimeCapture:
    def test_mime_from_delta_reaches_snapshot_outputs(self) -> None:
        agg = StreamAggregator()
        agg.feed({"event_type": "step.start", "index": 0, "step": {"type": "image"}})
        agg.feed(
            {
                "event_type": "step.delta",
                "index": 0,
                "delta": {"type": "image", "data": "aGk=", "mime_type": "image/jpeg"},
            }
        )
        agg.feed({"event_type": "step.stop", "index": 0})
        agg.feed(
            {
                "event_type": "interaction.completed",
                "interaction": {"id": "int-mime-1", "status": "completed"},
            }
        )
        outputs = snapshot_outputs(agg.snapshot())
        images = [o for o in outputs if o["type"] == "image"]
        assert images == [{"type": "image", "data": "aGk=", "mime_type": "image/jpeg"}]

    def test_missing_mime_falls_back_to_png(self) -> None:
        agg = StreamAggregator()
        agg.feed(
            {"event_type": "step.delta", "index": 0, "delta": {"type": "image", "data": "aGk="}}
        )
        agg.feed({"event_type": "step.stop", "index": 0})
        agg.feed(
            {
                "event_type": "interaction.completed",
                "interaction": {"id": "int-mime-2", "status": "completed"},
            }
        )
        outputs = snapshot_outputs(agg.snapshot())
        images = [o for o in outputs if o["type"] == "image"]
        assert images == [{"type": "image", "data": "aGk=", "mime_type": "image/png"}]
