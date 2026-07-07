"""``gdr show <id>`` — print a saved artifact from a prior research run.

Reads the interaction record from ``JsonlStore`` to locate the run's
``output_dir``, then prints the requested artifact. No API calls — this
is purely a local browser over already-written files.

``--part`` selects which artifact to render:

* ``text`` (default) → ``report.md``
* ``sources`` → ``sources.json``, pretty-printed
* ``metadata`` → ``metadata.json``, pretty-printed
* ``transcript`` → ``transcript.json``, pretty-printed
* ``images`` → list of ``images/*`` files with paths

If the run directory has been deleted or moved, we print a friendly
message rather than crashing with a Python traceback.
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path

import typer
from rich.console import Console

from gdr.commands._common import friendly_errors, lookup_record, open_store
from gdr.core.models import Record
from gdr.core.persistence import Store


class Part(str, Enum):
    text = "text"
    sources = "sources"
    metadata = "metadata"
    transcript = "transcript"
    images = "images"


@friendly_errors
def run(
    interaction_id: str = typer.Argument(
        ..., help="Interaction id (full or first-N unique prefix)."
    ),
    part: Part = typer.Option(Part.text, "--part", "-p", help="Which artifact to render."),
) -> None:
    """Print a saved artifact from a prior research run.

    Output goes through plain stdout (no Rich styling or wrapping) so it
    can be piped: ``gdr show <id> > report.md`` round-trips byte-for-byte.
    """
    console = Console()

    store = open_store()
    record = lookup_record(store, interaction_id)
    if record is None:
        # Try prefix match as a convenience — 'gdr show intabc' works
        # when the interaction was 'intabcxyz123'.
        matches = _find_by_prefix(store, interaction_id)
        if len(matches) == 1:
            record = matches[0]
        elif len(matches) > 1:
            shown = ", ".join(r.id for r in matches[:5])
            console.print(
                f"[red]Prefix {interaction_id!r} matches {len(matches)} records:[/red] "
                f"{shown}{'…' if len(matches) > 5 else ''}\n"
                f"Use more characters of the id."
            )
            raise typer.Exit(code=4)

    if record is None:
        console.print(
            f"[red]No record found for id {interaction_id!r}.[/red]\n"
            f"Run [bold]gdr ls[/bold] to see known ids."
        )
        raise typer.Exit(code=4)

    output_dir = record.output_dir
    if not output_dir.exists():
        console.print(
            f"[yellow]Record exists but output directory is missing:[/yellow] {output_dir}"
        )
        raise typer.Exit(code=4)

    _render_part(console, output_dir=output_dir, part=part)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_part(console: Console, *, output_dir: Path, part: Part) -> None:
    if part is Part.text:
        _print_text_file(console, output_dir / "report.md")
    elif part is Part.sources:
        _print_json_file(console, output_dir / "sources.json")
    elif part is Part.metadata:
        _print_json_file(console, output_dir / "metadata.json")
    elif part is Part.transcript:
        _print_json_file(console, output_dir / "transcript.json")
    elif part is Part.images:
        _print_images(console, output_dir)


def _print_text_file(console: Console, path: Path) -> None:
    if not path.is_file():
        console.print(f"[yellow]Missing file:[/yellow] {path}")
        raise typer.Exit(code=4)
    # typer.echo, not console.print: Rich hard-wraps at terminal width
    # (80 on pipes), which would corrupt the report when redirected.
    typer.echo(path.read_text(encoding="utf-8"), nl=False)


def _print_json_file(console: Console, path: Path) -> None:
    if not path.is_file():
        console.print(f"[yellow]Missing file:[/yellow] {path}")
        raise typer.Exit(code=4)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        console.print(f"[red]Could not parse {path}:[/red] {exc}")
        raise typer.Exit(code=4) from exc
    # Plain stdout so `gdr show --part sources > x.json` stays valid JSON.
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


def _print_images(console: Console, output_dir: Path) -> None:
    images_dir = output_dir / "images"
    if not images_dir.is_dir():
        console.print("[dim]No images were generated for this run.[/dim]")
        return
    files = sorted(p for p in images_dir.iterdir() if p.is_file())
    if not files:
        console.print("[dim]No images were generated for this run.[/dim]")
        return
    for path in files:
        # One unwrapped path per line — shell-loop friendly.
        typer.echo(str(path))


def _find_by_prefix(store: Store, prefix: str) -> list[Record]:
    """Return every record whose id starts with ``prefix``."""
    return [r for r in store.recent() if r.id.startswith(prefix)]
