"""``gdr follow-up <id> <question>`` — ask a follow-up question.

Delegates to :func:`gdr.commands.research.execute_research` with
``previous_interaction_id=<id>`` so the new interaction inherits the
prior run's context. The follow-up question becomes both the display
query (for the output directory slug) and the on-wire ``input``.

Unlike ``gdr research --plan``, follow-ups don't go through the
planning loop — you've presumably already seen the plan (or the output)
of the parent run, and want an incremental answer.
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer
from rich.console import Console

from gdr.commands._common import load_cfg
from gdr.commands.research import execute_research


def run(
    interaction_id: str = typer.Argument(
        ..., help="Interaction id to use as the parent of the follow-up."
    ),
    question: str = typer.Argument(..., help="The follow-up question."),
    use_max: bool = typer.Option(
        False,
        "--max",
        help="Use Deep Research Max for the follow-up (higher quality, higher cost).",
    ),
    stream: bool | None = typer.Option(
        None,
        "--stream/--no-stream",
        help="Stream live output. Defaults to on when stdout is a TTY.",
    ),
    output: Path | None = typer.Option(
        None, "--output", "-o", help="Exact output directory for the follow-up run."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the request JSON and exit without calling the API."
    ),
    api_key: str | None = typer.Option(
        None, "--api-key", help="Override the API key for this run only."
    ),
    no_confirm: bool = typer.Option(
        False, "--no-confirm", help="Skip the Max cost-confirmation prompt."
    ),
    config_path: Path | None = typer.Option(
        None, "--config", help="Path to an alternate config TOML."
    ),
) -> None:
    """Ask a follow-up question using a prior interaction as context."""
    console = Console()
    config = load_cfg(config_path)
    use_stream = stream if stream is not None else _stdout_is_tty()

    execute_research(
        config=config,
        display_query=question,
        use_max=use_max,
        use_stream=use_stream,
        output=output,
        api_key=api_key,
        no_confirm=no_confirm,
        console=console,
        dry_run=dry_run,
        previous_interaction_id=interaction_id,
        api_input=question,
    )


def _stdout_is_tty() -> bool:
    isatty = getattr(sys.stdout, "isatty", None)
    return bool(isatty()) if callable(isatty) else False
