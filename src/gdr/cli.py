"""Typer app root for the gdr CLI.

This module wires up the top-level application and registers subcommands.
Subcommands live in `gdr.commands.*` and are added here as each phase lands.
"""

from __future__ import annotations

import typer

from gdr import __version__
from gdr.commands import plan, research
from gdr.constants import APP_DESCRIPTION, APP_NAME

app = typer.Typer(
    name=APP_NAME,
    help=APP_DESCRIPTION,
    no_args_is_help=True,
    add_completion=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"{APP_NAME} {__version__}")
        raise typer.Exit


@app.callback()
def main(
    _version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show the installed gdr version and exit.",
    ),
) -> None:
    """Gemini Deep Research from your terminal.

    Run `gdr research <query>` to start. See `gdr --help` for the full command
    list once more commands are registered in later phases.
    """


# Subcommands — each module exposes a `run` function or a Typer sub-app.
app.command(name="research", help="Run a Deep Research task and save artifacts to disk.")(
    research.run
)
app.add_typer(plan.app, name="plan")
