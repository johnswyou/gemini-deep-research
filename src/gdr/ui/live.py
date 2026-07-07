"""Live terminal rendering for streaming Deep Research runs.

Pattern
-------

We use Rich's ``Status`` widget (a single animated line that can be updated
in place) rather than its heavier ``Live`` primitive. Rationale:

* Thoughts and text chunks should *persist* above the status line so the
  user can scroll back and read them. Rich's ``Live`` wants to own the
  whole viewport, which is wrong for streamed text.
* ``Status`` sits at the bottom, updates in place, and plays nicely with
  ``console.print`` — prints scroll above the status line without flicker.

Non-TTY stdout (pipes, redirects, CI logs) degrades gracefully: Rich
auto-disables the animation and prints updates as flat log lines.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from rich.console import Console

from gdr.core.streaming import StreamAggregator, StreamEvent, snapshot_outputs
from gdr.ui.progress import format_elapsed


@dataclass
class LiveStreamResult:
    """Summary returned to the command after the stream ends.

    ``interaction_id`` is the authoritative id captured on ``interaction.created``;
    callers use it to re-fetch the canonical outputs via
    ``client.interactions.get(id=...)``.

    ``completed_cleanly`` is ``True`` only when an ``interaction.completed``
    event arrived. A disconnect that kills the iterator leaves it ``False``
    and the caller should fall through to polling.

    ``interrupted`` is ``True`` when the user hit Ctrl+C while the stream
    was being consumed. The caller should stop (printing a resume hint),
    not fall through to polling.
    """

    interaction_id: str | None
    status: str | None
    completed_cleanly: bool
    streamed_outputs: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    interrupted: bool = False
    # Usage reported by the completion event (fallback when the terminal
    # fetch omits usage).
    total_tokens: int | None = None


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


def _trim(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _status_line(*, elapsed: int, status: str, interaction_id: str | None, query: str) -> str:
    pre = _trim(query, 60) + " " if query else ""
    status_bit = f"[magenta]{status}[/magenta]"
    tail = f"id=[dim]{interaction_id}[/dim]" if interaction_id else ""
    return (
        f"[bold]Researching[/bold] {pre}"
        f"([cyan]{format_elapsed(elapsed)}[/cyan], {status_bit}) {tail}".rstrip()
    )


class LiveRenderer:
    """Translates :class:`StreamEvent` emissions into Rich console output.

    Kept as its own class (rather than a function closure) so tests can
    construct one with a fake Console and drive it directly without
    hitting the Rich Status machinery.
    """

    def __init__(
        self,
        *,
        console: Console,
        query: str = "",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._console = console
        self._query = query
        self._clock = clock
        self._start = clock()
        self._status: str = "starting"
        self._interaction_id: str | None = None
        # Text deltas arrive as small chunks. We buffer then flush when we
        # see a newline or when the buffer gets large, to avoid
        # one-character-per-line output.
        self._text_buffer: list[str] = []

    # -- pure event handling ------------------------------------------

    def handle(self, event: StreamEvent) -> None:
        if event.kind == "start":
            self._interaction_id = event.interaction_id
            self._status = event.status or "in_progress"
        elif event.kind == "status":
            self._interaction_id = event.interaction_id or self._interaction_id
            self._status = event.status or self._status
        elif event.kind == "thought":
            self._flush_text()
            self._console.print(f"[dim italic]» {event.text}[/dim italic]")
        elif event.kind == "text_delta":
            self._text_buffer.append(event.text)
            # Flush on explicit newline to keep paragraphs intact.
            if "\n" in event.text:
                self._flush_text()
        elif event.kind == "image":
            # Base64 chunks are often many KB; don't print them. Show a
            # succinct marker instead.
            self._flush_text()
            self._console.print(
                f"[green]⬢ received image chunk ({len(event.image_data)} bytes)[/green]"
            )
        elif event.kind == "complete":
            self._flush_text()
            self._status = event.status or self._status
        # content_start / content_stop are not rendered; they're invisible
        # to the user and only matter for the aggregator's bookkeeping.

    # -- flush + status accessors -------------------------------------

    def finish(self) -> None:
        """Write any remaining buffered text to the console."""
        self._flush_text()

    def render_status_line(self) -> str:
        elapsed = int(self._clock() - self._start)
        return _status_line(
            elapsed=elapsed,
            status=self._status,
            interaction_id=self._interaction_id,
            query=self._query,
        )

    def _flush_text(self) -> None:
        if not self._text_buffer:
            return
        text = "".join(self._text_buffer)
        self._text_buffer.clear()
        # `highlight=False` + `markup=False` keep streamed text verbatim:
        # Rich won't try to interpret a stray `[` as markup or invent colors.
        self._console.print(text, end="", highlight=False, markup=False, soft_wrap=True)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


DisconnectCallback = Callable[[Exception], None]
# (interaction_id, last_event_id) -> a fresh SSE event iterable resuming
# after last_event_id. Raising falls back to the disconnect path.
ReconnectFn = Callable[[str, str | None], Iterable[Any]]

# How many times a dropped stream is re-attached before giving up and
# falling back to polling. Streams drop after ~600s server-side, so a
# long Max run can legitimately need a few reconnects.
MAX_STREAM_RECONNECTS = 3


def stream_with_live_ui(
    stream: Iterable[Any],
    *,
    console: Console | None = None,
    query: str = "",
    on_disconnect: DisconnectCallback | None = None,
    reconnect: ReconnectFn | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> LiveStreamResult:
    """Drive a streaming interaction through the renderer + status UI.

    Per-event exceptions are not caught — only a bulk iteration failure
    (e.g. the underlying HTTP connection dies) is treated as a
    disconnect. When ``reconnect`` is provided and the interaction id is
    known, a dropped stream is re-attached (resuming after the last seen
    ``event_id``) up to ``MAX_STREAM_RECONNECTS`` times before falling
    back to the disconnect path, so partial output isn't thrown away
    over a transient blip.

    Returns a :class:`LiveStreamResult` with everything the caller needs
    to continue (via polling fallback if necessary). Raises
    :class:`StreamError` if the API sent an explicit error event; these
    surface to the user unaltered since they typically indicate a
    configuration or quota problem that polling won't fix.
    """
    con = console if console is not None else Console()
    renderer = LiveRenderer(console=con, query=query, clock=clock)
    agg = StreamAggregator(on_event=renderer.handle)

    with con.status(renderer.render_status_line(), spinner="dots") as status_widget:
        reconnects_used = 0

        def refresh_status() -> None:
            status_widget.update(renderer.render_status_line())

        def _try_reconnect(exc: Exception) -> Iterator[Any] | None:
            """Re-attach to the stream, or None to fall back to polling.

            Any failure — the SDK not supporting ``last_event_id``, the
            call erroring, or the result not being iterable — degrades
            gracefully to the disconnect path.
            """
            nonlocal reconnects_used
            if reconnect is None or agg.interaction_id is None:
                return None
            if reconnects_used >= MAX_STREAM_RECONNECTS:
                return None
            reconnects_used += 1
            con.print(
                f"[yellow]Stream dropped ({type(exc).__name__}); reconnecting "
                f"({reconnects_used}/{MAX_STREAM_RECONNECTS})…[/yellow]"
            )
            try:
                return iter(reconnect(agg.interaction_id, agg.last_event_id))
            except Exception:
                return None

        # The StreamAggregator is push-based; we nudge status updates after
        # each event so the timer keeps moving during active streaming.
        iterator = iter(stream)
        while True:
            try:
                event = next(iterator)
            except StopIteration:
                break
            except KeyboardInterrupt:
                # The user asked to stop. Hand back whatever we know (most
                # importantly the interaction id, so the caller can print a
                # resume hint) instead of unwinding with a traceback.
                renderer.finish()
                return LiveStreamResult(
                    interaction_id=agg.interaction_id,
                    status=agg.status,
                    completed_cleanly=False,
                    interrupted=True,
                )
            except Exception as exc:
                # httpx, IOError, or any SDK-surfaced transport exception is
                # treated uniformly as a disconnect: re-attach if we can,
                # otherwise let the caller poll for the authoritative result.
                new_stream = _try_reconnect(exc)
                if new_stream is not None:
                    iterator = new_stream
                    continue
                if on_disconnect is not None:
                    on_disconnect(exc)
                renderer.finish()
                snapshot = agg.snapshot()
                return LiveStreamResult(
                    interaction_id=agg.interaction_id,
                    status=agg.status,
                    completed_cleanly=False,
                    streamed_outputs=tuple(snapshot_outputs(snapshot)),
                    total_tokens=snapshot.total_tokens,
                )
            agg.feed(event)
            refresh_status()

    renderer.finish()
    con.print()  # one blank line after the stream, before the "Done" panel
    snapshot = agg.snapshot()
    return LiveStreamResult(
        interaction_id=agg.interaction_id,
        status=agg.status,
        completed_cleanly=snapshot.completed_cleanly,
        streamed_outputs=tuple(snapshot_outputs(snapshot)),
        total_tokens=snapshot.total_tokens,
    )
