"""Tests for `gdr.ui.progress` — polling loop with injected clock/sleep."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from gdr.errors import (
    NetworkError,
    ResearchCancelledError,
    ResearchFailedError,
    ResearchTimedOutError,
)
from gdr.ui.progress import (
    format_elapsed,
    next_poll_delay,
    poll_until_complete,
)


class FakeClock:
    """Deterministic clock that advances on each read."""

    def __init__(self, step_seconds: float = 3.0) -> None:
        self._now = 0.0
        self._step = step_seconds

    def __call__(self) -> float:
        value = self._now
        self._now += self._step
        return value

    def advance(self, seconds: float) -> None:
        self._now += seconds


class StatusScript:
    """Callable that returns a programmed sequence of statuses."""

    def __init__(self, statuses: list[str]) -> None:
        self._statuses = statuses
        self.calls = 0

    def __call__(self, *, id: str) -> Any:
        idx = min(self.calls, len(self._statuses) - 1)
        self.calls += 1
        return SimpleNamespace(id=id, status=self._statuses[idx])


def _noop_sleep(_s: float) -> None:
    pass


# ---------------------------------------------------------------------------
# Arithmetic helpers
# ---------------------------------------------------------------------------


class TestFormatElapsed:
    def test_short_durations_show_minutes_seconds(self) -> None:
        assert format_elapsed(0) == "00:00"
        assert format_elapsed(65) == "01:05"
        assert format_elapsed(119) == "01:59"

    def test_long_durations_show_hours(self) -> None:
        assert format_elapsed(3600) == "1:00:00"
        assert format_elapsed(3725) == "1:02:05"


class TestNextPollDelay:
    def test_short_elapsed_uses_initial_cadence(self) -> None:
        assert next_poll_delay(0) == 5.0
        assert next_poll_delay(119) == 5.0

    def test_long_elapsed_uses_extended_cadence(self) -> None:
        assert next_poll_delay(120) == 15.0
        assert next_poll_delay(1200) == 15.0


# ---------------------------------------------------------------------------
# poll_until_complete
# ---------------------------------------------------------------------------


class TestPollUntilComplete:
    def test_returns_when_completed_on_first_poll(self) -> None:
        get = StatusScript(["completed"])
        result = poll_until_complete(
            get, "abc", clock=FakeClock(step_seconds=1.0), sleep=_noop_sleep
        )
        assert result.status == "completed"
        assert get.calls == 1

    def test_polls_through_in_progress_to_completed(self) -> None:
        get = StatusScript(["in_progress", "in_progress", "completed"])
        poll_until_complete(get, "abc", clock=FakeClock(step_seconds=1.0), sleep=_noop_sleep)
        assert get.calls == 3

    def test_raises_research_failed(self) -> None:
        get = StatusScript(["failed"])
        with pytest.raises(ResearchFailedError):
            poll_until_complete(get, "abc", clock=FakeClock(), sleep=_noop_sleep)

    def test_raises_research_cancelled(self) -> None:
        get = StatusScript(["cancelled"])
        with pytest.raises(ResearchCancelledError):
            poll_until_complete(get, "abc", clock=FakeClock(), sleep=_noop_sleep)

    def test_raises_timeout_when_clock_exceeds_budget(self) -> None:
        # In-progress forever; clock jumps past the budget quickly.
        get = StatusScript(["in_progress"] * 10)
        clock = FakeClock(step_seconds=100.0)
        with pytest.raises(ResearchTimedOutError):
            poll_until_complete(get, "abc", timeout_seconds=60, clock=clock, sleep=_noop_sleep)

    def test_on_tick_invoked_each_poll(self) -> None:
        ticks: list[tuple[int, str]] = []
        get = StatusScript(["in_progress", "completed"])
        poll_until_complete(
            get,
            "abc",
            on_tick=lambda e, s: ticks.append((e, s)),
            clock=FakeClock(step_seconds=1.0),
            sleep=_noop_sleep,
        )
        # Two polls means two ticks: one for in_progress, one for completed.
        assert len(ticks) == 2
        assert ticks[-1][1] == "completed"

    def test_works_with_dict_shaped_interactions(self) -> None:
        class DictScript:
            def __init__(self) -> None:
                self._seq = [{"status": "in_progress"}, {"status": "completed"}]
                self.calls = 0

            def __call__(self, *, id: str) -> Any:
                value = self._seq[min(self.calls, len(self._seq) - 1)]
                self.calls += 1
                return value

        get = DictScript()
        result = poll_until_complete(
            get, "abc", clock=FakeClock(step_seconds=1.0), sleep=_noop_sleep
        )
        assert result["status"] == "completed"


# ---------------------------------------------------------------------------
# Transient-failure retry (2026-07 review: one blip must not kill a run)
# ---------------------------------------------------------------------------


class TestPollRetry:
    def test_transient_failures_then_success(self) -> None:
        calls = {"n": 0}

        def flaky_get(*, id: str) -> Any:
            calls["n"] += 1
            if calls["n"] <= 2:
                raise ConnectionError("blip")
            return SimpleNamespace(id=id, status="completed")

        retries: list[int] = []
        result = poll_until_complete(
            flaky_get,
            "int-1",
            on_transient_error=lambda n, _exc: retries.append(n),
            clock=FakeClock(),
            sleep=_noop_sleep,
        )
        assert result.status == "completed"
        assert retries == [1, 2]

    def test_consecutive_failures_exhaust_to_network_error(self) -> None:
        def always_fails(*, id: str) -> Any:
            raise ConnectionError("down")

        with pytest.raises(NetworkError) as excinfo:
            poll_until_complete(always_fails, "int-2", clock=FakeClock(), sleep=_noop_sleep)
        message = str(excinfo.value)
        assert "gdr resume int-2" in message
        assert "gdr status int-2" in message

    def test_failure_counter_resets_after_success(self) -> None:
        # 4 failures, one success, 4 more failures: never reaches the cap of 5.
        script = ["fail"] * 4 + ["ok"] + ["fail"] * 4 + ["done"]
        state = {"i": 0}

        def scripted_get(*, id: str) -> Any:
            action = script[min(state["i"], len(script) - 1)]
            state["i"] += 1
            if action == "fail":
                raise ConnectionError("blip")
            if action == "ok":
                return SimpleNamespace(id=id, status="in_progress")
            return SimpleNamespace(id=id, status="completed")

        result = poll_until_complete(scripted_get, "int-3", clock=FakeClock(), sleep=_noop_sleep)
        assert result.status == "completed"

    def test_incomplete_status_is_terminal(self) -> None:
        # `incomplete` (google-genai 1.73 status) must not poll forever.
        get = StatusScript(["in_progress", "incomplete"])
        result = poll_until_complete(get, "int-4", clock=FakeClock(), sleep=_noop_sleep)
        assert result.status == "incomplete"
        assert get.calls == 2
