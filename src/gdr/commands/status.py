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

from gdr.commands._common import build_client, get_attr_or_key, load_cfg, lookup_record, open_store
from gdr.ui.progress import format_elapsed

_UTC = timezone.utc


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
        console.print(f"[red]Failed to fetch interaction {interaction_id}:[/red] {exc}")
        raise typer.Exit(code=5) from exc

    status = str(get_attr_or_key(interaction, "status") or "unknown")
    console.print(f"[bold]Status:[/bold] {_colored_status(status)}")
    console.print(f"[bold]ID:[/bold]     {interaction_id}")

    # Elapsed time — best-effort, only when we have a started_at in the
    # local store. The SDK does not expose a started_at on the interaction
    # object itself.
    store = open_store()
    record = lookup_record(store, interaction_id)
    if record is not None:
        _print_elapsed(console, record.created_at)

    _print_usage(console, interaction)
    _print_last_thought(console, interaction)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _colored_status(status: str) -> str:
    palette = {
        "completed": "green",
        "failed": "red",
        "cancelled": "yellow",
        "in_progress": "blue",
    }
    color = palette.get(status, "white")
    return f"[{color}]{status}[/{color}]"


def _print_elapsed(console: Console, created_at: datetime) -> None:
    now = datetime.now(_UTC)
    anchor = created_at if created_at.tzinfo else created_at.replace(tzinfo=_UTC)
    elapsed = (now - anchor).total_seconds()
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
    outputs = get_attr_or_key(interaction, "outputs") or []
    last_thought = None
    for output in outputs:
        if get_attr_or_key(output, "type") in {"thought", "thought_summary"}:
            last_thought = get_attr_or_key(output, "summary") or get_attr_or_key(output, "text")
    if last_thought:
        console.print(f"[bold]Thought:[/bold] [dim]{last_thought}[/dim]")
