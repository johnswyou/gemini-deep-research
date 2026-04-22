"""``gdr cancel <id>`` — cancel a running interaction.

Calls ``client.interactions.cancel(id=...)`` on the SDK. If the SDK
doesn't expose a cancel method (older versions, different SDK forks),
we surface that clearly so the user knows to upgrade rather than see a
cryptic AttributeError.

Idempotent: cancelling an already-terminal interaction is a no-op — we
fetch the current status first and short-circuit with a friendly
message rather than making a pointless network call.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from gdr.commands._common import build_client, get_attr_or_key, load_cfg
from gdr.constants import TERMINAL_STATUSES


def run(
    interaction_id: str = typer.Argument(..., help="Interaction id to cancel."),
    api_key: str | None = typer.Option(
        None, "--api-key", help="Override the API key for this run only."
    ),
    config_path: Path | None = typer.Option(
        None, "--config", help="Path to an alternate config TOML."
    ),
) -> None:
    """Cancel an in-progress Deep Research interaction."""
    console = Console()
    config = load_cfg(config_path)
    client = build_client(console, api_key=api_key, config=config)

    try:
        current = client.interactions.get(id=interaction_id)
    except Exception as exc:
        console.print(f"[red]Failed to fetch interaction:[/red] {exc}")
        raise typer.Exit(code=5) from exc

    current_status = str(get_attr_or_key(current, "status") or "unknown")
    if current_status in TERMINAL_STATUSES:
        console.print(
            f"[yellow]Interaction {interaction_id} is already in a terminal "
            f"state ({current_status}); nothing to cancel.[/yellow]"
        )
        return

    cancel = getattr(client.interactions, "cancel", None)
    if cancel is None:
        console.print(
            "[red]This google-genai SDK build does not expose interactions.cancel.[/red]\n"
            "Upgrade the `google-genai` package or cancel via the Gemini Console."
        )
        raise typer.Exit(code=4)

    try:
        cancel(id=interaction_id)
    except Exception as exc:
        console.print(f"[red]Failed to cancel interaction:[/red] {exc}")
        raise typer.Exit(code=5) from exc

    console.print(f"[green]Cancel request sent[/green] for id [dim]{interaction_id}[/dim].")
