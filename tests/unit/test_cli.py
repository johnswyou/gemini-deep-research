"""Smoke tests for the top-level Typer application."""

from __future__ import annotations

from typer.testing import CliRunner

from gdr import __version__
from gdr.cli import app

runner = CliRunner()


def test_version_flag_prints_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_short_version_flag_prints_version() -> None:
    result = runner.invoke(app, ["-V"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_help_shows_app_name() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "gdr" in result.output.lower()


def test_invoked_without_args_shows_help_and_exits_nonzero() -> None:
    # Typer is configured with `no_args_is_help=True`, which prints help and
    # exits with a non-zero code — that's the normal no-command behavior.
    result = runner.invoke(app, [])
    assert result.exit_code != 0
    assert "gdr" in result.output.lower()
