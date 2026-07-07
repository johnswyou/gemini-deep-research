"""Shared helpers used by multiple command modules.

The Phase-7 history/resume/follow-up/cancel commands each need to: load
config, build an SDK client, and/or look up an interaction record in the
local store. Duplicating that across six modules would quietly drift, so
the helpers live here behind a small surface.

Keep this module *small*. It's for boring plumbing, not domain logic —
anything interesting belongs in ``core/``.
"""

from __future__ import annotations

import functools
import os
import re
import sys
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypeVar

import typer
from rich.console import Console

from gdr.config import Config, load_config
from gdr.core.client import GdrClient
from gdr.core.models import Record
from gdr.core.normalize import get_field
from gdr.core.persistence import JsonlStore, Store
from gdr.errors import ConfigError, GdrError

_UTC = timezone.utc

_F = TypeVar("_F", bound=Callable[..., Any])


# ---------------------------------------------------------------------------
# Error boundary
# ---------------------------------------------------------------------------


def friendly_errors(fn: _F) -> _F:
    """Outer error boundary for command entry points.

    Converts any uncaught :class:`GdrError` into the documented exit code
    with a one-line message instead of a traceback. Apply to every Typer
    command function; commands may still catch specific errors earlier to
    add context — this is the net underneath.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except GdrError as exc:
            Console(stderr=True).print(f"[red]Error:[/red] {exc}")
            raise typer.Exit(code=exc.exit_code) from exc

    return wrapper  # type: ignore[return-value]


def stdout_is_tty() -> bool:
    """True when stdout is an interactive terminal.

    Shared default for ``--stream`` and other TTY-only niceties so every
    command answers the question the same way.
    """
    isatty = getattr(sys.stdout, "isatty", None)
    return bool(isatty()) if callable(isatty) else False


# ---------------------------------------------------------------------------
# Client construction
# ---------------------------------------------------------------------------


def build_client(
    console: Console,
    *,
    api_key: str | None,
    config: Config,
) -> GdrClient:
    """Build a :class:`GdrClient` with a consistent error path.

    The resolution order matches ``gdr research``: CLI flag → env var →
    config. Any :class:`ConfigError` is printed and turned into the
    documented exit code so every command behaves the same way for
    auth/config problems.
    """
    resolved_key = api_key or os.environ.get("GEMINI_API_KEY") or config.api_key
    try:
        return GdrClient(api_key=resolved_key)
    except ConfigError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=exc.exit_code) from exc


def load_cfg(config_path: Path | None) -> Config:
    """Thin wrapper kept so command modules don't all import
    :func:`gdr.config.load_config` for a single line of code."""
    return load_config(path=config_path)


# ---------------------------------------------------------------------------
# Store access
# ---------------------------------------------------------------------------


def open_store() -> Store:
    """Open the default JsonlStore. Split out for test injection."""
    return JsonlStore.open()


def lookup_record(store: Store, interaction_id: str) -> Record | None:
    """Find a record by exact id. Returns None when missing."""
    return store.find_by_id(interaction_id)


# ---------------------------------------------------------------------------
# --since DATE parsing
# ---------------------------------------------------------------------------

_RELATIVE_RE = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$", re.IGNORECASE)
_UNIT_TO_SECONDS: dict[str, int] = {
    "s": 1,
    "m": 60,
    "h": 60 * 60,
    "d": 60 * 60 * 24,
    "w": 60 * 60 * 24 * 7,
}


def parse_since(value: str, *, now: datetime | None = None) -> datetime:
    """Turn a user-supplied ``--since`` value into a UTC-aware datetime.

    Accepted forms:

    * ``7d`` / ``24h`` / ``90m`` / ``2w`` — relative durations from now.
    * ``2026-04-22`` — midnight UTC on the given date.
    * ``2026-04-22T14:30:00Z`` — full ISO 8601 timestamp.

    The ``now`` parameter is for deterministic tests — production code
    leaves it unset and lets the function read the wallclock.
    """
    anchor = now if now is not None else datetime.now(_UTC)
    stripped = value.strip()
    if not stripped:
        raise ConfigError("--since cannot be empty.")

    match = _RELATIVE_RE.match(stripped)
    if match:
        count = int(match.group(1))
        unit = match.group(2).lower()
        delta = timedelta(seconds=count * _UNIT_TO_SECONDS[unit])
        return anchor - delta

    # ISO / date-only. Python's fromisoformat handles both since 3.11+ and
    # accepts 'YYYY-MM-DD' on 3.10. We also accept the trailing 'Z'.
    iso_value = stripped.replace("Z", "+00:00") if stripped.endswith("Z") else stripped
    try:
        parsed = datetime.fromisoformat(iso_value)
    except ValueError as exc:
        raise ConfigError(
            f"--since {value!r} is not a recognized date. "
            f"Use '7d', 'YYYY-MM-DD', or full ISO 8601 (e.g. 2026-04-22T14:30:00Z)."
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_UTC)
    return parsed


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

# Canonical attribute-then-key lookup, re-exported under the historical
# command-layer name. The implementation lives in core/normalize.py.
get_attr_or_key = get_field
