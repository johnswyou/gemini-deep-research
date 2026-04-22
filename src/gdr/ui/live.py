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
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from rich.console import Console

from gdr.core.streaming import StreamAggregator, StreamEvent
from gdr.errors import StreamError
from gdr.ui.progress import format_elapsed


@dataclass
class LiveStreamResult:
    """Summary returned to the command after the stream ends.

    ``interaction_id`` is the authoritative id captured on ``interaction.start``;
    callers use it to re-fetch the canonical outputs via
    ``client.interactions.get(id=...)``.

    ``completed_cleanly`` is ``True`` only when an ``interaction.complete``
    event arrived. A disconnect that kills the iterator leaves it ``False``
    and the caller should fall through to polling.
    """

    interaction_id: str | None
    status: str | None
    completed_cleanly: bool


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


def stream_with_live_ui(
    stream: Iterable[Any],
    *,
    console: Console | None = None,
    query: str = "",
    on_disconnect: DisconnectCallback | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> LiveStreamResult:
    """Drive a streaming interaction through the renderer + status UI.

    The passed ``stream`` is iterated exactly once. Per-event exceptions
    are not caught — only a bulk iteration failure (e.g. the underlying
    HTTP connection dies) is treated as a disconnect.

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

        def refresh_status() -> None:
            status_widget.update(renderer.render_status_line())

        # The StreamAggregator is push-based; we nudge status updates after
        # each event so the timer keeps moving even during quiet periods.
        iterator = iter(stream)
        while True:
            try:
                event = next(iterator)
            except StopIteration:
                break
            except StreamError:
                raise
            except Exception as exc:
                # Defensive — httpx, IOError, or any SDK-surfaced transport
                # exception is treated uniformly as a disconnect. The caller
                # polls for the authoritative result via .get(id).
                if on_disconnect is not None:
                    on_disconnect(exc)
                renderer.finish()
                return LiveStreamResult(
                    interaction_id=agg.interaction_id,
                    status=agg.status,
                    completed_cleanly=False,
                )
            agg.feed(event)
            refresh_status()

    renderer.finish()
    con.print()  # one blank line after the stream, before the "Done" panel
    return LiveStreamResult(
        interaction_id=agg.interaction_id,
        status=agg.status,
        completed_cleanly=agg.snapshot().completed_cleanly,
    )
