"""Typed exceptions for gdr, mapped to documented process exit codes.

Keeping the mapping centralized here lets the CLI convert any exception into a
deterministic exit code at the outer boundary, which in turn lets users script
against `gdr` with confidence.
"""

from __future__ import annotations

# Exit code convention:
#   0   ok (no exception)
#   1   research failed
#   2   research cancelled
#   3   research timed out (60 min cap)
#   4   auth / config / validation problem
#   5   network error after retries exhausted
#   130 user interrupt (raised by Typer / signal handler, not by us)


class GdrError(Exception):
    """Base for all errors raised by gdr."""

    exit_code: int = 1


class ResearchFailedError(GdrError):
    """The Deep Research task reported status=failed."""

    exit_code = 1


class ResearchCancelledError(GdrError):
    """The Deep Research task reported status=cancelled."""

    exit_code = 2


class ResearchTimedOutError(GdrError):
    """The research task exceeded the documented 60-minute cap."""

    exit_code = 3


class ConfigError(GdrError):
    """Auth, config, or validation problem (bad API key, malformed TOML, etc)."""

    exit_code = 4


class NetworkError(GdrError):
    """Network failure after retries exhausted."""

    exit_code = 5


class StreamError(GdrError):
    """An error event arrived over the streaming connection."""

    exit_code = 1
