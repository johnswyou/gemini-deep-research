"""``gdr resume <id>`` — reattach to a research run and finish rendering.

Use cases:

1. The user Ctrl+C'd a streaming run before it completed. The original
   ``gdr research`` invocation exited with code 130 and a "resume" hint.
   ``gdr resume <id>`` re-polls the interaction and writes artifacts
   when it reaches a terminal state.
2. The interaction completed while the user was away. Re-running
   ``gdr resume <id>`` renders the final outputs to disk.

Requirements:

* The local store must have a record for the id so we can reconstruct a
  ``RunContext`` (for ``write_artifacts``). Without it we can't know the
  original query / agent / tools; a helpful message directs the user to
  re-run via ``gdr research`` if they want a freshly-scoped run.
* An API key (to call ``.get(id=...)`` and poll if needed).

``--force`` overwrites artifacts in the original run directory if they
already exist. Otherwise we write to a sibling directory suffixed with
``_resumed_<ts>`` so prior artifacts are preserved.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import typer
from rich.console import Console

from gdr.commands._common import (
    build_client,
    get_attr_or_key,
    load_cfg,
    lookup_record,
    open_store,
)
from gdr.constants import TERMINAL_STATUSES
from gdr.core.models import AgentConfig, Record, RunContext
from gdr.core.rendering import write_artifacts
from gdr.core.security import SecurityPolicy
from gdr.errors import GdrError
from gdr.ui.progress import run_with_live_status

_UTC = timezone.utc


def run(
    interaction_id: str = typer.Argument(..., help="Interaction id to resume."),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite artifacts in the original run directory instead of "
        "writing to a suffixed sibling directory.",
    ),
    api_key: str | None = typer.Option(
        None, "--api-key", help="Override the API key for this run only."
    ),
    config_path: Path | None = typer.Option(
        None, "--config", help="Path to an alternate config TOML."
    ),
) -> None:
    """Reattach to a running or completed interaction and write its artifacts."""
    console = Console()
    config = load_cfg(config_path)

    store = open_store()
    record = lookup_record(store, interaction_id)
    if record is None:
        console.print(
            f"[red]No local record for id {interaction_id!r}.[/red]\n"
            f"`gdr resume` requires a record to reconstruct the run context. "
            f"Re-run via [bold]gdr research[/bold] if this is a brand-new query."
        )
        raise typer.Exit(code=4)

    client = build_client(console, api_key=api_key, config=config)

    try:
        latest = client.interactions.get(id=interaction_id)
    except Exception as exc:
        console.print(f"[red]Failed to fetch interaction:[/red] {exc}")
        raise typer.Exit(code=5) from exc

    status = str(get_attr_or_key(latest, "status") or "unknown")
    console.print(f"[bold]Current status:[/bold] {status}")

    if status not in TERMINAL_STATUSES:
        try:
            latest = run_with_live_status(
                client.interactions.get,
                interaction_id,
                console=console,
                query=record.query,
            )
        except GdrError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=exc.exit_code) from exc

    target_dir = _choose_output_dir(record.output_dir, force=force)
    ctx = _build_context_from_record(record, output_dir=target_dir)
    policy = SecurityPolicy(
        output_root=config.output_dir,
        safe_untrusted=config.safe_untrusted,
        untrusted=False,
    )

    paths = write_artifacts(
        latest,
        ctx=ctx,
        output_dir=target_dir,
        policy=policy,
        started_at=record.created_at,
        finished_at=datetime.now(_UTC),
    )

    console.print(f"[bold green]Resumed.[/bold green] artifacts -> {paths['report'].parent}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _choose_output_dir(original: Path, *, force: bool) -> Path:
    """Pick a write destination that doesn't clobber prior artifacts.

    ``--force`` writes straight to ``original``. Otherwise, if the
    original has *any* artifact files we route to a sibling directory
    with a ``_resumed_<ts>`` suffix so history stays intact.
    """
    original = original.expanduser()
    if force or _dir_is_empty_or_missing(original):
        return original
    suffix = datetime.now(_UTC).strftime("_resumed_%Y-%m-%dT%H-%M-%S")
    return original.with_name(original.name + suffix)


def _dir_is_empty_or_missing(path: Path) -> bool:
    if not path.exists():
        return True
    try:
        next(iter(path.iterdir()))
    except StopIteration:
        return True
    return False


def _build_context_from_record(record: Record, *, output_dir: Path) -> RunContext:
    """Reconstruct a RunContext from a Record — enough for write_artifacts.

    The artifact writer only reads ``query``, ``agent``, ``output_dir``,
    ``previous_interaction_id``, and ``builtin_tools`` / ``mcp_servers`` /
    ``file_search`` for metadata. We leave tool lists empty since the
    record only keeps summary strings — metadata.json's ``tools`` list
    will be empty on resume, and that's fine (the original run already
    wrote the authoritative version).
    """
    return RunContext(
        query=record.query,
        agent=record.agent,
        output_dir=output_dir,
        previous_interaction_id=record.parent_id,
        agent_config=AgentConfig(),
        stream=False,
        background=True,
    )
