"""``gdr ls`` — list recent interactions from the local store.

Pure read command — no API calls. Reads the append-only ``JsonlStore``
and pretty-prints a Rich table.

``--status`` accepts any Deep Research status value (``completed`` /
``failed`` / ``cancelled`` / ``in_progress``). Unknown values simply
match nothing, which is the same behavior as filtering by an unused tag
elsewhere in Unix tooling.

``--since`` accepts relative durations (``7d``, ``24h``), dates
(``YYYY-MM-DD``), or full ISO 8601 timestamps — see
:func:`gdr.commands._common.parse_since`.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from gdr.commands._common import open_store, parse_since
from gdr.core.models import Record
from gdr.errors import ConfigError

# Truncate long queries so the table still fits a normal terminal.
_MAX_QUERY_CHARS = 60
_DEFAULT_LIMIT = 20


def run(
    limit: int = typer.Option(
        _DEFAULT_LIMIT,
        "--limit",
        "-n",
        help="Maximum number of rows to show (most recent first).",
    ),
    status: str | None = typer.Option(
        None,
        "--status",
        help="Only show interactions with this status "
        "(completed / failed / cancelled / in_progress).",
    ),
    since: str | None = typer.Option(
        None,
        "--since",
        help="Only show interactions created since this time "
        "(e.g. '7d', '24h', '2026-04-01', '2026-04-22T14:30:00Z').",
    ),
    show_full_id: bool = typer.Option(
        False,
        "--full-id",
        help="Print the full interaction id instead of a shortened version.",
    ),
    config_path: Path | None = typer.Option(
        None, "--config", help="Path to an alternate config TOML."
    ),
) -> None:
    """List recent research interactions."""
    console = Console()
    _ = config_path  # reserved for future filters (e.g. alternate state dirs)

    since_dt = None
    if since is not None:
        try:
            since_dt = parse_since(since)
        except ConfigError as exc:
            console.print(f"[red]Error:[/red] {exc}")
            raise typer.Exit(code=exc.exit_code) from exc

    store = open_store()
    records = store.recent(limit=limit, status=status, since=since_dt)

    if not records:
        console.print("[dim]No matching interactions found.[/dim]")
        return

    _render_table(console, records, show_full_id=show_full_id)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_table(console: Console, records: list[Record], *, show_full_id: bool) -> None:
    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Created", no_wrap=True)
    table.add_column("Status")
    table.add_column("Agent", style="dim")
    table.add_column("Tokens", justify="right")
    table.add_column("Query")

    for record in records:
        table.add_row(
            _format_id(record.id, full=show_full_id),
            record.created_at.strftime("%Y-%m-%d %H:%M"),
            _format_status(record.status),
            _shorten_agent(record.agent),
            _format_tokens(record.total_tokens),
            _truncate(record.query, _MAX_QUERY_CHARS),
        )

    console.print(table)


def _format_id(interaction_id: str, *, full: bool) -> str:
    if full:
        return interaction_id
    # Show the first 12 characters — long enough to disambiguate in most
    # stores, short enough to keep the table readable.
    return interaction_id[:12] + ("…" if len(interaction_id) > 12 else "")


def _format_status(status: str) -> str:
    palette = {
        "completed": "green",
        "failed": "red",
        "cancelled": "yellow",
        "in_progress": "blue",
    }
    color = palette.get(status, "white")
    return f"[{color}]{status}[/{color}]"


def _shorten_agent(agent: str) -> str:
    # 'deep-research-preview-04-2026' → 'preview'
    # 'deep-research-max-preview-04-2026' → 'max'
    if "max" in agent:
        return "max"
    if "preview" in agent:
        return "preview"
    return agent


def _format_tokens(total_tokens: int | None) -> str:
    if total_tokens is None:
        return "-"
    if total_tokens >= 1000:
        return f"{total_tokens / 1000:.1f}k"
    return str(total_tokens)


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"
