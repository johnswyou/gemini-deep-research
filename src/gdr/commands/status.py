"""``gdr status <id>`` — quick one-shot status check on an interaction.

Calls ``client.interactions.get(id=...)`` once and prints:

* Current ``status`` (completed / failed / cancelled / in_progress)
* Elapsed time if the local store has a ``created_at`` for this id
* Token usage if the API returned a ``usage`` object
* The most recent thought summary, when present, so the user can tell
  roughly where the agent is in its run

Useful when you detached from a streaming run and want a pulse without
re-attaching.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from gdr.commands._common import (
    build_client,
    colored_status,
    friendly_errors,
    get_attr_or_key,
    load_cfg,
    lookup_record,
    open_store,
)
from gdr.constants import TERMINAL_STATUSES
from gdr.core.models import Record
from gdr.core.normalize import normalized_outputs
from gdr.errors import NetworkError
from gdr.ui.progress import format_elapsed

_UTC = timezone.utc


@friendly_errors
def run(
    interaction_id: str = typer.Argument(..., help="Interaction id to query."),
    api_key: str | None = typer.Option(
        None, "--api-key", help="Override the API key for this run only."
    ),
    config_path: Path | None = typer.Option(
        None, "--config", help="Path to an alternate config TOML."
    ),
) -> None:
    """Print the current status of an interaction."""
    console = Console()
    config = load_cfg(config_path)
    client = build_client(console, api_key=api_key, config=config)

    try:
        interaction = client.interactions.get(id=interaction_id)
    except Exception as exc:
        raise NetworkError(f"Failed to fetch interaction {interaction_id}: {exc}") from exc

    status = str(get_attr_or_key(interaction, "status") or "unknown")
    console.print(f"[bold]Status:[/bold] {colored_status(status)}")
    console.print(f"[bold]ID:[/bold]     {interaction_id}")

    # Timing — best-effort, only when we have a local record. The SDK does
    # not expose a started_at on the interaction object itself.
    store = open_store()
    record = lookup_record(store, interaction_id)
    if record is not None:
        _print_timing(console, record, status=status)

    _print_usage(console, interaction)
    _print_last_thought(console, interaction)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _print_timing(console: Console, record: Record, *, status: str) -> None:
    """Elapsed wallclock for a running interaction; run duration once done.

    ``now - created_at`` on a terminal run is the record's *age* (a run
    that finished in ten minutes yesterday would show 20+ hours), so
    terminal statuses report the recorded duration instead.
    """
    anchor = (
        record.created_at if record.created_at.tzinfo else record.created_at.replace(tzinfo=_UTC)
    )
    if status in TERMINAL_STATUSES and record.finished_at is not None:
        end = (
            record.finished_at
            if record.finished_at.tzinfo
            else record.finished_at.replace(tzinfo=_UTC)
        )
        console.print(f"[bold]Duration:[/bold] {format_elapsed((end - anchor).total_seconds())}")
        return
    elapsed = (datetime.now(_UTC) - anchor).total_seconds()
    console.print(f"[bold]Elapsed:[/bold] {format_elapsed(elapsed)}")


def _print_usage(console: Console, interaction: Any) -> None:
    usage = get_attr_or_key(interaction, "usage")
    if usage is None:
        return
    total = get_attr_or_key(usage, "total_tokens")
    if total is None:
        return
    console.print(f"[bold]Tokens:[/bold]  {total}")


def _print_last_thought(console: Console, interaction: Any) -> None:
    last_thought: str | None = None
    for output in normalized_outputs(interaction):
        if output["type"] == "thought" and output.get("text"):
            last_thought = str(output["text"])
    if last_thought:
        console.print(f"[bold]Thought:[/bold] [dim]{last_thought}[/dim]")
