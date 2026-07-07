"""``gdr follow-up <id> <question>`` — ask a follow-up question.

Delegates to :func:`gdr.commands.research.execute_research` with
``previous_interaction_id=<id>`` so the new interaction inherits the
prior run's context. The follow-up question becomes both the display
query (for the output directory slug) and the on-wire ``input``.

Two execution modes:

* Default: the follow-up runs the full Deep Research agent again
  (fresh multi-minute, ~$1-3 research grounded in the parent context).
* ``--model <id>``: the follow-up targets a plain Gemini model (e.g.
  ``gemini-3.1-pro-preview``) — fast and cheap, right for "elaborate on
  section 3"-style clarification over the parent's existing findings.

Security posture: if the parent run executed in untrusted-input mode,
the follow-up inherits it (the local record persists the flag). Pass
``--untrusted-input`` to force it on a follow-up whose parent has no
local record.

Unlike ``gdr research --plan``, follow-ups don't go through the
planning loop — you've presumably already seen the plan (or the output)
of the parent run, and want an incremental answer.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from gdr.commands._common import (
    friendly_errors,
    load_cfg,
    lookup_record,
    open_store,
    stdout_is_tty,
)
from gdr.commands.research import execute_research
from gdr.errors import ConfigError


@friendly_errors
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
    model: str | None = typer.Option(
        None,
        "--model",
        help=(
            "Answer with a plain Gemini model (e.g. gemini-3.1-pro-preview) instead of "
            "re-running a Deep Research agent — much faster and cheaper for "
            "clarification questions."
        ),
    ),
    stream: bool | None = typer.Option(
        None,
        "--stream/--no-stream",
        help="Stream live output. Defaults to on when stdout is a TTY.",
    ),
    output: Path | None = typer.Option(
        None, "--output", "-o", help="Exact output directory for the follow-up run."
    ),
    untrusted_input: bool = typer.Option(
        False,
        "--untrusted-input",
        help=(
            "Treat the run as handling untrusted content (strips code_execution and "
            "mcp_server tools). Inherited automatically from the parent run's record."
        ),
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
    use_stream = stream if stream is not None else stdout_is_tty()

    if model is not None and use_max:
        raise ConfigError(
            "--model and --max are mutually exclusive: --model targets a plain "
            "Gemini model, --max a Deep Research agent."
        )

    # Inherit the parent's security posture. A follow-up re-uses the
    # parent's (possibly attacker-influenced) context, so trust must not
    # silently reset just because no new files/URLs are attached.
    effective_untrusted = untrusted_input
    parent = lookup_record(open_store(), interaction_id)
    if parent is not None and parent.untrusted and not untrusted_input:
        effective_untrusted = True
        console.print(
            "[dim]Untrusted-input mode inherited from the parent run "
            "(code_execution / mcp_server stay disabled).[/dim]"
        )

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
        untrusted_input=effective_untrusted,
        model=model,
    )
