"""Tests for ``gdr.commands._common`` — shared helpers.

The helpers themselves are mostly thin wrappers; what earns a test is
the date parsing (``parse_since``), which has real edge cases around
relative durations and ISO formats.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from gdr.commands._common import parse_since
from gdr.errors import ConfigError

_UTC = timezone.utc
_NOW = datetime(2026, 4, 22, 12, 0, 0, tzinfo=_UTC)


class TestParseSinceRelative:
    def test_seconds(self) -> None:
        result = parse_since("30s", now=_NOW)
        assert (_NOW - result).total_seconds() == 30

    def test_minutes(self) -> None:
        result = parse_since("15m", now=_NOW)
        assert (_NOW - result).total_seconds() == 15 * 60

    def test_hours(self) -> None:
        result = parse_since("24h", now=_NOW)
        assert (_NOW - result).total_seconds() == 24 * 3600

    def test_days(self) -> None:
        result = parse_since("7d", now=_NOW)
        assert (_NOW - result).total_seconds() == 7 * 86400

    def test_weeks(self) -> None:
        result = parse_since("2w", now=_NOW)
        assert (_NOW - result).total_seconds() == 14 * 86400

    def test_upper_case_unit(self) -> None:
        assert parse_since("2D", now=_NOW) == parse_since("2d", now=_NOW)


class TestParseSinceAbsolute:
    def test_date_only(self) -> None:
        result = parse_since("2026-04-20", now=_NOW)
        assert result == datetime(2026, 4, 20, 0, 0, tzinfo=_UTC)

    def test_iso_with_z(self) -> None:
        result = parse_since("2026-04-22T09:30:00Z", now=_NOW)
        assert result == datetime(2026, 4, 22, 9, 30, tzinfo=_UTC)

    def test_iso_with_offset(self) -> None:
        result = parse_since("2026-04-22T10:30:00+01:00", now=_NOW)
        # Normalize to UTC for comparison.
        assert result.utcoffset() is not None
        assert result.astimezone(_UTC) == datetime(2026, 4, 22, 9, 30, tzinfo=_UTC)

    def test_naive_datetime_gets_utc_tz(self) -> None:
        result = parse_since("2026-04-22T09:30:00", now=_NOW)
        assert result.tzinfo == _UTC


class TestParseSinceErrors:
    def test_empty_string_rejected(self) -> None:
        with pytest.raises(ConfigError, match="cannot be empty"):
            parse_since("   ")

    def test_garbage_rejected(self) -> None:
        with pytest.raises(ConfigError, match="not a recognized date"):
            parse_since("not-a-date")

    def test_unknown_unit_rejected(self) -> None:
        with pytest.raises(ConfigError):
            parse_since("5y")
