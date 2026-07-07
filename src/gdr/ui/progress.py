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
    STATUS_FAILED,
    TERMINAL_STATUSES,
)
from gdr.errors import (
    NetworkError,
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
RetryFn = Callable[[int, Exception], None]

# Transient-failure budget: a single blip must not kill a 30-minute run,
# but a hard-down network should surface within ~a minute.
MAX_CONSECUTIVE_POLL_FAILURES = 5
_RETRY_BACKOFF_CAP_SECONDS = 30.0


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
    on_transient_error: RetryFn | None = None,
    clock: ClockFn = time.monotonic,
    sleep: SleepFn = time.sleep,
) -> Any:
    """Poll ``get(id=...)`` until the interaction reaches a terminal status.

    Transient ``get`` failures (network blips, 5xx) are retried with
    backoff — a research task runs for up to an hour and a single dropped
    request must not abandon it. After ``MAX_CONSECUTIVE_POLL_FAILURES``
    consecutive failures we give up with :class:`NetworkError`; the task
    keeps running server-side and the message says how to reattach.

    Raises:
        ResearchFailedError: terminal status is ``failed``.
        ResearchCancelledError: terminal status is ``cancelled``.
        ResearchTimedOutError: elapsed time exceeds ``timeout_seconds``.
        NetworkError: polling itself kept failing.

    ``on_tick`` is called after each successful poll with
    ``(elapsed_seconds, status)``; ``on_transient_error`` after each failed
    one with ``(consecutive_failures, exception)``. ``clock`` and ``sleep``
    are injectable for testability.
    """
    start = clock()
    consecutive_failures = 0

    while True:
        try:
            interaction = get(id=interaction_id)
        except Exception as exc:
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_POLL_FAILURES:
                raise NetworkError(
                    f"Polling failed {consecutive_failures} times in a row; last error: {exc}. "
                    f"The research may still be running server-side — check on it later "
                    f"with `gdr status {interaction_id}` or `gdr resume {interaction_id}`."
                ) from exc
            if on_transient_error is not None:
                on_transient_error(consecutive_failures, exc)
            sleep(
                min(
                    POLL_INITIAL_SECONDS * (2 ** (consecutive_failures - 1)),
                    _RETRY_BACKOFF_CAP_SECONDS,
                )
            )
            continue

        consecutive_failures = 0
        status = getattr(interaction, "status", None) or (
            interaction.get("status") if isinstance(interaction, dict) else None
        )
        elapsed = int(clock() - start)
        if on_tick is not None:
            on_tick(elapsed, str(status))

        if status == STATUS_FAILED:
            raise ResearchFailedError(f"Research failed (interaction id: {interaction_id}).")
        if status == STATUS_CANCELLED:
            raise ResearchCancelledError(f"Research cancelled (interaction id: {interaction_id}).")
        if status in TERMINAL_STATUSES:
            # completed, plus any other terminal state (e.g. `incomplete`)
            # the caller inspects and reports on.
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

    The status line shows elapsed time, current status, and the
    interaction id (so the user can copy it for ``gdr resume`` before
    detaching). On non-TTY stdout, Rich falls back to plain output that
    avoids cursor-manipulation that would trash log files.
    """
    con = console if console is not None else Console()
    preface = f"{query[:80]}… " if len(query) > 80 else f"{query} " if query else ""

    with con.status(f"[bold]Researching {preface}(00:00)", spinner="dots") as status:

        def _tick(elapsed: int, status_str: str) -> None:
            status.update(
                f"[bold]Researching {preface}([cyan]{format_elapsed(elapsed)}[/cyan], "
                f"[magenta]{status_str}[/magenta])  id=[dim]{interaction_id}[/dim]"
            )

        def _on_transient_error(failures: int, exc: Exception) -> None:
            con.print(
                f"[yellow]Poll attempt failed "
                f"({failures}/{MAX_CONSECUTIVE_POLL_FAILURES}): {exc}. Retrying…[/yellow]"
            )

        return poll_until_complete(
            get,
            interaction_id,
            timeout_seconds=timeout_seconds,
            on_tick=_tick,
            on_transient_error=_on_transient_error,
        )
