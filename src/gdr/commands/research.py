"""The primary ``gdr research <query>`` command.

Phase 3 scope (polling-only MVP):

* Accept the query and a small set of flags (``--max``, ``--output``,
  ``--dry-run``, ``--api-key``, ``--no-confirm``, ``--config``).
* Load TOML config, resolve the API key, build a ``RunContext``.
* Build create() kwargs via :mod:`gdr.core.requests`.
* On ``--dry-run``, print the kwargs as JSON and exit without hitting the
  API.
* Otherwise: submit the interaction, poll to completion with a live
  status line, render artifacts (report.md / sources.json / metadata.json
  / transcript.json), append a Record to the local store, and print the
  paths.

Streaming (Phase 4), collaborative planning (Phase 5), tool/MCP/multimodal
flags (Phase 6), history commands (Phase 7), and operator commands
(Phase 8) hook in without touching this file's core flow — they extend
``RunContext`` construction and wrap the polling call.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel

from gdr.config import Config, load_config
from gdr.constants import AGENT_FAST, AGENT_MAX
from gdr.core.client import GdrClient
from gdr.core.models import AgentConfig, Record, RunContext
from gdr.core.persistence import JsonlStore, Store
from gdr.core.rendering import write_artifacts
from gdr.core.requests import build_create_kwargs
from gdr.core.security import SecurityPolicy, sanitize_slug
from gdr.errors import ConfigError, GdrError
from gdr.ui.progress import run_with_live_status

_UTC = timezone.utc

# ---------------------------------------------------------------------------
# Option parsing helpers
# ---------------------------------------------------------------------------


def _resolve_api_key(cli_arg: str | None, env: dict[str, str], config: Config) -> str | None:
    """CLI flag → GEMINI_API_KEY env var → config value (already env-expanded)."""
    if cli_arg:
        return cli_arg
    env_key = env.get("GEMINI_API_KEY")
    if env_key:
        return env_key
    return config.api_key


def _allocate_output_dir(
    *,
    root: Path,
    query: str,
    interaction_id: str,
    started_at: datetime,
    override: Path | None,
    policy: SecurityPolicy,
) -> Path:
    """Resolve the directory that will hold this run's artifacts.

    When ``override`` is given it's used verbatim (still subject to
    ``SecurityPolicy.confine`` if the user passed something weird — e.g. a
    path outside their configured root). Otherwise we build a stable
    ``<ts>_<slug>_<id6>`` name under the config's output_dir.
    """
    if override is not None:
        return override.expanduser().resolve()

    slug = sanitize_slug(query)
    id_fragment = re.sub(r"[^A-Za-z0-9]+", "", interaction_id)[:6] or "noid"
    ts = started_at.strftime("%Y-%m-%dT%H-%M")
    candidate = root / f"{ts}_{slug}_{id_fragment}"
    return policy.confine(candidate)


def _build_run_context(
    *, query: str, config: Config, use_max: bool, output_dir: Path
) -> RunContext:
    agent = AGENT_MAX if use_max else config.default_agent
    if use_max:
        # Respect explicit CLI choice even when config points at something else.
        agent = AGENT_MAX
    elif config.default_agent not in (AGENT_FAST, AGENT_MAX):
        # Allow unknown ids from config (Google may release new agents), but
        # fall back to the documented fast agent when config is truly empty.
        agent = config.default_agent

    return RunContext(
        query=query,
        agent=agent,
        builtin_tools=config.default_tools,
        output_dir=output_dir,
        stream=False,  # Phase 3: polling only. Phase 4 flips this per --stream.
        background=True,
        agent_config=AgentConfig(
            thinking_summaries=config.thinking_summaries,  # type: ignore[arg-type]
            visualization=config.visualization,  # type: ignore[arg-type]
            collaborative_planning=False,
        ),
        confirm_max=config.confirm_max,
        auto_open=config.auto_open,
    )


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def run(
    query: str = typer.Argument(..., help="Your research question."),
    use_max: bool = typer.Option(
        False, "--max", help="Use Deep Research Max (higher quality, longer runtime, higher cost)."
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Exact output directory (overrides the default <ts>_<slug>_<id> layout).",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the request body as JSON and exit without calling the API."
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
    """Run a Deep Research task and save the report to disk."""
    console = Console()
    config = load_config(path=config_path)

    # For --dry-run we don't need network or API key. Build the kwargs with
    # a synthetic output_dir that passes validation but doesn't need to exist.
    dry_output = output if output is not None else config.output_dir / "(dry-run)"
    policy = SecurityPolicy(
        output_root=config.output_dir,
        safe_untrusted=config.safe_untrusted,
        untrusted=False,
    )

    try:
        ctx_for_dry = _build_run_context(
            query=query, config=config, use_max=use_max, output_dir=dry_output
        )
        kwargs, stripped = build_create_kwargs(ctx_for_dry, policy)
    except ConfigError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=exc.exit_code) from exc

    if stripped:
        console.print(
            f"[yellow]Untrusted-input mode stripped tools:[/yellow] {', '.join(stripped)}"
        )

    if dry_run:
        _print_dry_run(console, kwargs)
        return

    # Max confirmation gate — skip when the user has opted out either per-run
    # (--no-confirm) or globally (confirm_max = false in config).
    if use_max and ctx_for_dry.confirm_max and not no_confirm and not _confirm_max(console):
        console.print("[yellow]Aborted.[/yellow]")
        raise typer.Exit(code=0)

    import os  # noqa: PLC0415 — only needed for API key env lookup

    resolved_key = _resolve_api_key(api_key, dict(os.environ), config)
    try:
        client = GdrClient(api_key=resolved_key)
    except ConfigError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=exc.exit_code) from exc

    started_at = datetime.now(_UTC)
    try:
        interaction = client.interactions.create(**kwargs)
    except Exception as exc:  # pragma: no cover - surface SDK errors as GdrError
        console.print(f"[red]Failed to start research:[/red] {exc}")
        raise typer.Exit(code=5) from exc

    interaction_id = getattr(interaction, "id", None) or (
        interaction.get("id") if isinstance(interaction, dict) else None
    )
    if not interaction_id:
        console.print("[red]API returned no interaction id; cannot proceed.[/red]")
        raise typer.Exit(code=5)

    console.print(f"[green]Research started.[/green]  id=[dim]{interaction_id}[/dim]")

    final_output_dir = _allocate_output_dir(
        root=config.output_dir,
        query=query,
        interaction_id=interaction_id,
        started_at=started_at,
        override=output,
        policy=policy,
    )

    # Rebuild RunContext with the actual output_dir so rendering sees the
    # right value. (We used a synthetic one above because the interaction id
    # wasn't known yet.)
    ctx = ctx_for_dry.model_copy(update={"output_dir": final_output_dir})

    try:
        interaction = run_with_live_status(
            client.interactions.get,
            interaction_id,
            console=console,
            query=query,
        )
        finished_at = datetime.now(_UTC)
    except GdrError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=exc.exit_code) from exc

    paths = write_artifacts(
        interaction,
        ctx=ctx,
        output_dir=final_output_dir,
        policy=policy,
        started_at=started_at,
        finished_at=finished_at,
    )

    _record_run(
        interaction=interaction,
        ctx=ctx,
        started_at=started_at,
        finished_at=finished_at,
    )

    _print_done(console, paths)


# ---------------------------------------------------------------------------
# Small helpers kept private to this module
# ---------------------------------------------------------------------------


def _print_dry_run(console: Console, kwargs: dict[str, Any]) -> None:
    console.print("[bold]Dry run — the following would be sent to the API:[/bold]")
    console.print_json(json.dumps(kwargs, default=str))


def _confirm_max(console: Console) -> bool:
    console.print(
        Panel.fit(
            "You requested Deep Research [bold]Max[/bold].\n"
            "Max runs take longer and cost more (~$3-$7 per task vs ~$1-$3 for the fast agent).\n"
            "Use [bold]--no-confirm[/bold] to skip this prompt in scripts.",
            title="Heads up",
            border_style="yellow",
        )
    )
    # typer.confirm returns a bool in interactive mode.
    return bool(typer.confirm("Proceed with Max?", default=False))


def _record_run(
    *,
    interaction: Any,
    ctx: RunContext,
    started_at: datetime,
    finished_at: datetime,
    store: Store | None = None,
) -> None:
    """Append a Record describing this run to the local store.

    The ``store`` parameter is injectable so tests can pass a memory-backed
    fake. In normal use we open the default JsonlStore just-in-time.
    """
    interaction_id = getattr(interaction, "id", None) or (
        interaction.get("id") if isinstance(interaction, dict) else None
    )
    if not interaction_id:
        return  # Nothing actionable to record.

    tools = list(ctx.builtin_tools)
    if ctx.file_search is not None:
        tools.append("file_search")
    tools.extend("mcp_server" for _ in ctx.mcp_servers)

    total_tokens = getattr(getattr(interaction, "usage", None), "total_tokens", None)

    record = Record(
        id=str(interaction_id),
        parent_id=ctx.previous_interaction_id,
        created_at=started_at,
        finished_at=finished_at,
        status=str(
            getattr(interaction, "status", None)
            or (interaction.get("status") if isinstance(interaction, dict) else "unknown")
        ),
        agent=ctx.agent,
        query=ctx.query,
        output_dir=ctx.output_dir,
        total_tokens=total_tokens,
        tools=tuple(tools),
    )

    target_store = store if store is not None else JsonlStore.open()
    target_store.append(record)


def _print_done(console: Console, paths: dict[str, Path]) -> None:
    console.print()
    console.print("[bold green]Done.[/bold green]")
    console.print(f"  report:     {paths['report']}")
    console.print(f"  sources:    {paths['sources']}")
    console.print(f"  metadata:   {paths['metadata']}")
    console.print(f"  transcript: {paths['transcript']}")
    # Let downstream shell code consume the primary artifact without parsing
    # the panel — e.g. `PATH=$(gdr research ... --quiet)`. We print the
    # report path last with no styling so it's the last stdout line when
    # Rich wraps the rest.
    if not sys.stdout.isatty():
        sys.stdout.write(f"{paths['report']}\n")
