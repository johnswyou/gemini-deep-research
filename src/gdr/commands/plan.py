"""``gdr plan`` — async subcommands for the collaborative planning flow.

Two entry points:

* ``gdr plan refine <id> <feedback>`` — add one more refinement turn to an
  existing plan and print the new plan with its id. Useful when iterating
  across terminal sessions (a plan created yesterday can be refined
  tomorrow).
* ``gdr plan approve <id>`` — approve a plan and kick off the full
  research run. Internally this is the same as ``gdr research --plan``
  resuming from the approval step, so it shares the execution pipeline.

For interactive approve/refine in one session, prefer
``gdr research --plan <query>`` — these async commands exist for the
"come back tomorrow" use case.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import typer
from rich.console import Console

from gdr.commands.research import execute_research
from gdr.config import load_config
from gdr.core.client import GdrClient
from gdr.core.planning import (
    PlanRequest,
    extract_interaction_id,
    run_plan_phase,
    show_plan,
)
from gdr.errors import ConfigError, GdrError

app = typer.Typer(
    name="plan",
    help="Manage collaborative research plans (approve or refine across sessions).",
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


# ---------------------------------------------------------------------------
# Shared option helpers
# ---------------------------------------------------------------------------


def _build_plan_client(
    console: Console, *, api_key: str | None, config_path: Path | None
) -> tuple[GdrClient, str]:
    """Load config, resolve the API key, and construct the SDK client.

    Returns ``(client, default_agent)``. Exits with a friendly message on
    any config/auth failure.
    """
    config = load_config(path=config_path)
    resolved_key = api_key or os.environ.get("GEMINI_API_KEY") or config.api_key
    try:
        client = GdrClient(api_key=resolved_key)
    except ConfigError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=exc.exit_code) from exc
    return client, config.default_agent


# ---------------------------------------------------------------------------
# refine
# ---------------------------------------------------------------------------


@app.command("refine", help="Refine an existing plan with new feedback.")
def refine_cmd(
    plan_id: str = typer.Argument(..., help="The id of the plan to iterate on."),
    feedback: str = typer.Argument(
        ..., help="Your feedback. E.g. 'Focus on 2024 data and drop methodology.'"
    ),
    api_key: str | None = typer.Option(
        None, "--api-key", help="Override the API key for this run only."
    ),
    config_path: Path | None = typer.Option(
        None, "--config", help="Path to an alternate config TOML."
    ),
) -> None:
    """Produce a refined plan by feeding feedback into an existing one."""
    console = Console()
    client, default_agent = _build_plan_client(console, api_key=api_key, config_path=config_path)

    request = PlanRequest(
        input_text=feedback,
        agent=default_agent,
        previous_interaction_id=plan_id,
    )
    try:
        plan_interaction = run_plan_phase(client, req=request, console=console)
    except GdrError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=exc.exit_code) from exc

    show_plan(console, plan_interaction)

    new_id = extract_interaction_id(plan_interaction)
    if not new_id:
        console.print("[red]Refinement returned no new interaction id.[/red]")
        raise typer.Exit(code=5)

    console.print(f"[green]New plan id:[/green] [bold]{new_id}[/bold]")
    console.print(
        "Next steps:\n"
        f"  • Keep iterating:  [bold]gdr plan refine {new_id} '<feedback>'[/bold]\n"
        f"  • Run the research: [bold]gdr plan approve {new_id}[/bold]"
    )

    # Let downstream scripting grab the new id cleanly.
    if not sys.stdout.isatty():
        sys.stdout.write(f"{new_id}\n")


# ---------------------------------------------------------------------------
# approve
# ---------------------------------------------------------------------------


@app.command("approve", help="Approve a plan and run the full research.")
def approve_cmd(
    plan_id: str = typer.Argument(..., help="The id of the plan to approve and execute."),
    display_query: str | None = typer.Option(
        None,
        "--query",
        "-q",
        help="Optional label for the output directory slug and metadata. "
        "Defaults to a generic 'approved-plan-<id6>' string.",
    ),
    stream: bool | None = typer.Option(
        None,
        "--stream/--no-stream",
        help="Stream live thought summaries and text deltas. Defaults to on when stdout is a TTY.",
    ),
    output: Path | None = typer.Option(None, "--output", "-o", help="Exact output directory."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the request body as JSON and exit without calling the API."
    ),
    api_key: str | None = typer.Option(
        None, "--api-key", help="Override the API key for this run only."
    ),
    config_path: Path | None = typer.Option(
        None, "--config", help="Path to an alternate config TOML."
    ),
) -> None:
    """Approve an existing plan and run the research it describes."""
    console = Console()
    config = load_config(path=config_path)

    use_stream = stream if stream is not None else _stdout_is_tty()
    label = display_query or f"approved-plan-{_short_id(plan_id)}"

    execute_research(
        config=config,
        display_query=label,
        use_max=False,  # `approve` inherits the agent from the config
        use_stream=use_stream,
        output=output,
        api_key=api_key,
        no_confirm=True,  # plans have already been reviewed; no Max prompt
        console=console,
        dry_run=dry_run,
        previous_interaction_id=plan_id,
        api_input="Plan looks good!",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stdout_is_tty() -> bool:
    isatty = getattr(sys.stdout, "isatty", None)
    return bool(isatty()) if callable(isatty) else False


def _short_id(interaction_id: str) -> str:
    """6-char sanitized fragment for use in slugs and display labels."""
    import re  # noqa: PLC0415 — only needed here

    return re.sub(r"[^A-Za-z0-9]+", "", interaction_id)[:6] or "noid"
