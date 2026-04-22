"""Polling UI for long-running research tasks.

Deep Research tasks take 5-60 minutes. During polling we show:

* A live-updating spinner with elapsed wallclock time and current status
* The interaction id (so the user can Ctrl+C and `gdr resume` later)

The actual polling loop is extracted as ``poll_until_complete`` so it can be
exercised by tests with injected ``clock`` and ``sleep`` callables — without
this, we'd be unit-testing ``time.sleep`` itself, which is both slow and
brittle.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Protocol

from rich.console import Console

from gdr.constants import (
    MAX_RESEARCH_SECONDS,
    POLL_EXTENDED_SECONDS,
    POLL_INITIAL_SECONDS,
    POLL_INITIAL_WINDOW_SECONDS,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    TERMINAL_STATUSES,
)
from gdr.errors import (
    ResearchCancelledError,
    ResearchFailedError,
    ResearchTimedOutError,
)

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class _Getter(Protocol):
    def __call__(self, id: str) -> Any: ...


ClockFn = Callable[[], float]
SleepFn = Callable[[float], None]
TickFn = Callable[[int, str], None]


# ---------------------------------------------------------------------------
# Polling math
# ---------------------------------------------------------------------------


def next_poll_delay(elapsed_seconds: float) -> float:
    """Time to wait before the next ``.get(id)`` poll.

    Faster cadence during the first two minutes (5s) so the user sees status
    transitions quickly; slower cadence (15s) thereafter to avoid hammering
    the API on 30-minute Max runs.
    """
    return (
        POLL_INITIAL_SECONDS
        if elapsed_seconds < POLL_INITIAL_WINDOW_SECONDS
        else POLL_EXTENDED_SECONDS
    )


def format_elapsed(seconds: float) -> str:
    mm, ss = divmod(int(seconds), 60)
    if mm >= 60:
        hh, mm = divmod(mm, 60)
        return f"{hh:d}:{mm:02d}:{ss:02d}"
    return f"{mm:02d}:{ss:02d}"


# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------


def poll_until_complete(
    get: _Getter,
    interaction_id: str,
    *,
    timeout_seconds: int = MAX_RESEARCH_SECONDS,
    on_tick: TickFn | None = None,
    clock: ClockFn = time.monotonic,
    sleep: SleepFn = time.sleep,
) -> Any:
    """Poll ``get(id=...)`` until the interaction reaches a terminal status.

    Raises:
        ResearchFailedError: terminal status is ``failed``.
        ResearchCancelledError: terminal status is ``cancelled``.
        ResearchTimedOutError: elapsed time exceeds ``timeout_seconds``.

    ``on_tick`` is called after each poll with ``(elapsed_seconds, status)``
    so the UI layer can update a spinner without this function depending on
    Rich. ``clock`` and ``sleep`` are injectable for testability.
    """
    start = clock()

    while True:
        interaction = get(id=interaction_id)
        status = getattr(interaction, "status", None) or (
            interaction.get("status") if isinstance(interaction, dict) else None
        )
        elapsed = int(clock() - start)
        if on_tick is not None:
            on_tick(elapsed, str(status))

        if status == STATUS_COMPLETED:
            return interaction
        if status == STATUS_FAILED:
            raise ResearchFailedError(f"Research failed (interaction id: {interaction_id}).")
        if status == STATUS_CANCELLED:
            raise ResearchCancelledError(f"Research cancelled (interaction id: {interaction_id}).")
        if status in TERMINAL_STATUSES:  # pragma: no cover — defensive
            return interaction

        if elapsed >= timeout_seconds:
            raise ResearchTimedOutError(
                f"Research timed out after {format_elapsed(elapsed)} "
                f"(interaction id: {interaction_id}). Resume: gdr resume {interaction_id}"
            )

        sleep(next_poll_delay(elapsed))


# ---------------------------------------------------------------------------
# Rich-based facade
# ---------------------------------------------------------------------------


def run_with_live_status(
    get: _Getter,
    interaction_id: str,
    *,
    console: Console | None = None,
    query: str = "",
    timeout_seconds: int = MAX_RESEARCH_SECONDS,
) -> Any:
    """High-level helper: poll with a Rich live status line.

    Writes an interaction-id footer the user can copy for ``gdr resume``.
    On non-TTY stdout, Rich falls back to a plain line-per-update output,
    which is still readable but avoids cursor-manipulation that would
    trash log files.
    """
    con = console if console is not None else Console()
    preface = f"{query[:80]}… " if len(query) > 80 else f"{query} " if query else ""

    with con.status(f"[bold]Researching {preface}(00:00)", spinner="dots") as status:

        def _tick(elapsed: int, status_str: str) -> None:
            status.update(
                f"[bold]Researching {preface}([cyan]{format_elapsed(elapsed)}[/cyan], "
                f"[magenta]{status_str}[/magenta])  id=[dim]{interaction_id}[/dim]"
            )

        return poll_until_complete(
            get,
            interaction_id,
            timeout_seconds=timeout_seconds,
            on_tick=_tick,
        )
