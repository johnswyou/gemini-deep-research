"""Tests for `gdr.ui.live`.

Rich's `Console` is exercised with ``record=True`` + a ``StringIO`` file
so assertions check the rendered text without requiring a real terminal.
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from gdr.core.streaming import StreamEvent
from gdr.errors import StreamError
from gdr.ui.live import LiveRenderer, LiveStreamResult, stream_with_live_ui

FIXTURES = Path(__file__).parent.parent / "fixtures" / "streams"


def _fixture_events(name: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with (FIXTURES / name).open("r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if stripped:
                events.append(json.loads(stripped))
    return events


def _capture_console() -> Console:
    """Return a Console that writes to a StringIO and records output."""
    return Console(
        file=io.StringIO(),
        record=True,
        force_terminal=False,  # plain output for stable assertions
        width=120,
    )


class FakeClock:
    def __init__(self) -> None:
        self._now = 0.0

    def __call__(self) -> float:
        value = self._now
        self._now += 1.0
        return value


# ---------------------------------------------------------------------------
# LiveRenderer
# ---------------------------------------------------------------------------


class TestLiveRenderer:
    def test_thought_prints_dim_italic_line(self) -> None:
        con = _capture_console()
        r = LiveRenderer(console=con, query="Q", clock=FakeClock())
        r.handle(StreamEvent(kind="thought", text="Searching."))
        output = con.export_text()
        assert "Searching." in output
        assert "»" in output  # thought prefix

    def test_text_buffers_until_newline(self) -> None:
        con = _capture_console()
        r = LiveRenderer(console=con, query="Q", clock=FakeClock())
        r.handle(StreamEvent(kind="text_delta", text="Hello "))
        # No newline — nothing printed yet.
        assert con.export_text() == ""
        r.handle(StreamEvent(kind="text_delta", text="world!\n"))
        # Newline flushes.
        assert "Hello world!" in con.export_text()

    def test_finish_flushes_remaining_buffer(self) -> None:
        con = _capture_console()
        r = LiveRenderer(console=con, query="Q", clock=FakeClock())
        r.handle(StreamEvent(kind="text_delta", text="Hanging chunk"))
        r.finish()
        assert "Hanging chunk" in con.export_text()

    def test_thought_flushes_pending_text_first(self) -> None:
        con = _capture_console()
        r = LiveRenderer(console=con, query="Q", clock=FakeClock())
        r.handle(StreamEvent(kind="text_delta", text="Writing..."))
        r.handle(StreamEvent(kind="thought", text="Checking sources."))
        output = con.export_text()
        # Text must appear *before* the thought line so ordering is preserved.
        assert output.index("Writing...") < output.index("Checking sources.")

    def test_image_event_prints_size_marker(self) -> None:
        con = _capture_console()
        r = LiveRenderer(console=con, query="Q", clock=FakeClock())
        r.handle(StreamEvent(kind="image", image_data="AAAAAAAA"))
        assert "image chunk" in con.export_text()

    def test_content_start_stop_are_silent(self) -> None:
        con = _capture_console()
        r = LiveRenderer(console=con, query="Q", clock=FakeClock())
        r.handle(StreamEvent(kind="content_start", index=0, content_type="text"))
        r.handle(StreamEvent(kind="content_stop", index=0))
        assert con.export_text() == ""

    def test_status_line_includes_interaction_id_and_elapsed(self) -> None:
        con = _capture_console()
        clock = FakeClock()
        r = LiveRenderer(console=con, query="my research", clock=clock)
        r.handle(StreamEvent(kind="start", interaction_id="int-xyz", status="in_progress"))
        # Advance clock a few times so elapsed is non-zero.
        for _ in range(5):
            clock()
        line = r.render_status_line()
        assert "int-xyz" in line
        assert "Researching" in line
        assert "in_progress" in line

    def test_markup_in_text_is_escaped(self) -> None:
        # If an agent emits `[bold]` in streaming text, Rich must not
        # interpret it as markup and crash on mismatched brackets.
        con = _capture_console()
        r = LiveRenderer(console=con, query="Q", clock=FakeClock())
        r.handle(StreamEvent(kind="text_delta", text="use [brackets] freely\n"))
        out = con.export_text()
        assert "[brackets]" in out


# ---------------------------------------------------------------------------
# stream_with_live_ui
# ---------------------------------------------------------------------------


def _event_iter(events: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    yield from events


class TestStreamWithLiveUI:
    def test_happy_path_returns_clean_completion(self) -> None:
        con = _capture_console()
        result = stream_with_live_ui(
            _event_iter(_fixture_events("happy_path.jsonl")),
            console=con,
            query="test",
            clock=FakeClock(),
        )
        assert isinstance(result, LiveStreamResult)
        assert result.interaction_id == "int-happy-001"
        assert result.status == "completed"
        assert result.completed_cleanly is True

    def test_error_event_raises_stream_error(self) -> None:
        con = _capture_console()
        with pytest.raises(StreamError):
            stream_with_live_ui(
                _event_iter(_fixture_events("mid_stream_error.jsonl")),
                console=con,
                clock=FakeClock(),
            )

    def test_disconnect_invokes_callback_and_returns_id(self) -> None:
        con = _capture_console()
        disconnects: list[Exception] = []

        def flaky_iter() -> Iterator[dict[str, Any]]:
            yield {
                "event_type": "interaction.start",
                "interaction": {"id": "int-flaky-abc", "status": "in_progress"},
            }
            yield {
                "event_type": "content.start",
                "index": 0,
                "content": {"type": "text"},
            }
            raise ConnectionError("simulated TCP drop")

        result = stream_with_live_ui(
            flaky_iter(),
            console=con,
            on_disconnect=disconnects.append,
            clock=FakeClock(),
        )

        assert result.interaction_id == "int-flaky-abc"
        assert result.completed_cleanly is False
        assert len(disconnects) == 1
        assert isinstance(disconnects[0], ConnectionError)

    def test_fixture_with_image_does_not_crash(self) -> None:
        # Belt-and-braces: real-looking events including images shouldn't
        # trip the renderer even when the console is dry.
        con = _capture_console()
        result = stream_with_live_ui(
            _event_iter(_fixture_events("thought_image_text.jsonl")),
            console=con,
            clock=FakeClock(),
        )
        assert result.completed_cleanly is True
        output = con.export_text()
        assert "Analyzing market share data" in output
        assert "image chunk" in output
