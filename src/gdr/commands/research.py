"""The primary ``gdr research <query>`` command.

The Typer entry point (:func:`run`) is a thin wrapper that parses flags
and delegates to :func:`execute_research`, which is also imported by
:mod:`gdr.commands.plan` so ``gdr plan approve <id>`` reuses the full
submit → stream/poll → render pipeline.
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
from gdr.constants import AGENT_FAST, AGENT_MAX, TERMINAL_STATUSES
from gdr.core.client import GdrClient
from gdr.core.inputs import (
    ensure_url_context_tool,
    parse_file_search_stores,
    parse_files,
    parse_mcps,
    urls_as_text_part,
    validate_tool_names,
    validate_visualization,
)
from gdr.core.models import (
    AgentConfig,
    FileSearchSpec,
    InputPart,
    McpSpec,
    Record,
    RunContext,
)
from gdr.core.persistence import JsonlStore, Store
from gdr.core.planning import interactive_plan_loop
from gdr.core.rendering import write_artifacts
from gdr.core.requests import build_create_kwargs
from gdr.core.security import SecurityPolicy, sanitize_slug
from gdr.errors import ConfigError, GdrError, StreamError
from gdr.ui.live import stream_with_live_ui
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


def _resolve_agent(config: Config, *, use_max: bool) -> str:
    if use_max:
        return AGENT_MAX
    if config.default_agent not in (AGENT_FAST, AGENT_MAX):
        # Allow unknown ids from config (Google may release new agents), but
        # still honor them as-is.
        return config.default_agent
    return config.default_agent


def _build_run_context(
    *,
    query: str,
    config: Config,
    use_max: bool,
    output_dir: Path,
    stream: bool,
    previous_interaction_id: str | None = None,
    builtin_tools: tuple[str, ...] | None = None,
    mcp_servers: tuple[McpSpec, ...] = (),
    file_search: FileSearchSpec | None = None,
    input_parts: tuple[InputPart, ...] = (),
    visualization: str | None = None,
    untrusted_input: bool = False,
) -> RunContext:
    tools = config.default_tools if builtin_tools is None else builtin_tools
    effective_visualization = visualization if visualization is not None else config.visualization
    return RunContext(
        query=query,
        agent=_resolve_agent(config, use_max=use_max),
        builtin_tools=tools,
        mcp_servers=mcp_servers,
        file_search=file_search,
        input_parts=input_parts,
        output_dir=output_dir,
        stream=stream,
        background=True,
        agent_config=AgentConfig(
            thinking_summaries=config.thinking_summaries,  # type: ignore[arg-type]
            visualization=effective_visualization,  # type: ignore[arg-type]
            collaborative_planning=False,
        ),
        previous_interaction_id=previous_interaction_id,
        confirm_max=config.confirm_max,
        auto_open=config.auto_open,
        untrusted_input=untrusted_input,
    )


def _default_stream_preference() -> bool:
    """Default ``--stream`` value: on when stdout is an interactive TTY."""
    isatty = getattr(sys.stdout, "isatty", None)
    return bool(isatty()) if callable(isatty) else False


def _parse_flag_inputs(
    *,
    tool_names: list[str],
    mcp_tokens: list[str],
    mcp_header_tokens: list[str],
    files: list[Path],
    urls: list[str],
    file_search_stores: list[str],
    visualization: str | None,
) -> tuple[
    tuple[str, ...] | None,
    tuple[McpSpec, ...],
    FileSearchSpec | None,
    tuple[InputPart, ...],
    str | None,
]:
    """Turn raw CLI flag values into typed domain objects.

    Returns ``(tools_override, mcp_specs, file_search, input_parts,
    visualization)``. ``tools_override`` is ``None`` when no ``--tool``
    flags were passed, meaning "use config defaults"; any other value
    means "replace with this list".
    """
    tools_override: tuple[str, ...] | None = validate_tool_names(tool_names) if tool_names else None
    mcp_specs = parse_mcps(mcp_tokens, mcp_header_tokens)
    file_search = parse_file_search_stores(file_search_stores)

    # Assemble supplementary input parts: files first, then a URL-block
    # text part at the end so the agent reads URLs as explicit context.
    parts: list[InputPart] = list(parse_files(files))
    url_part = urls_as_text_part(urls)
    if url_part is not None:
        parts.append(url_part)

    # When --url is passed, make sure url_context is in the tool list so
    # the agent can actually follow them. Only touch the list when tools
    # were explicitly set; otherwise the config default (which already
    # includes url_context) covers it.
    if tools_override is not None:
        tools_override = ensure_url_context_tool(tools_override, has_urls=bool(urls))

    vis_literal = validate_visualization(visualization)
    return tools_override, mcp_specs, file_search, tuple(parts), vis_literal


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def run(
    query: str = typer.Argument(..., help="Your research question."),
    use_max: bool = typer.Option(
        False, "--max", help="Use Deep Research Max (higher quality, longer runtime, higher cost)."
    ),
    use_plan: bool = typer.Option(
        False,
        "--plan",
        help="Review and refine the agent's plan before it runs the research.",
    ),
    stream: bool | None = typer.Option(
        None,
        "--stream/--no-stream",
        help="Stream live thought summaries and text deltas. Defaults to on when stdout is a TTY.",
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Exact output directory (overrides the default <ts>_<slug>_<id> layout).",
    ),
    tools: list[str] = typer.Option(
        [],
        "--tool",
        help=(
            "Enable a simple builtin tool (google_search, url_context, code_execution). "
            "Repeatable; overrides config defaults when specified."
        ),
    ),
    mcps: list[str] = typer.Option(
        [],
        "--mcp",
        help=("Attach an MCP server as NAME=URL. Repeatable. Use --mcp-header to add auth."),
    ),
    mcp_headers: list[str] = typer.Option(
        [],
        "--mcp-header",
        help="Attach a header to an MCP server as NAME=Key:Value. Repeatable.",
    ),
    files: list[Path] = typer.Option(
        [],
        "--file",
        help="Attach a local file (PDF, image, audio, video, etc.) as input. Repeatable.",
    ),
    urls: list[str] = typer.Option(
        [],
        "--url",
        help="Attach a URL for the agent to ground on. Enables url_context. Repeatable.",
    ),
    file_search_stores: list[str] = typer.Option(
        [],
        "--file-search-store",
        help=(
            "Enable File Search on a named store. Accepts bare names or "
            "'fileSearchStores/<name>'. Repeatable."
        ),
    ),
    visualization: str | None = typer.Option(
        None,
        "--visualization",
        help="Control chart/infographic generation: 'auto' or 'off'.",
    ),
    untrusted_input: bool = typer.Option(
        False,
        "--untrusted-input",
        help=(
            "Treat inputs (files/URLs) as untrusted. Strips code_execution and "
            "mcp_server tools to reduce prompt-injection blast radius."
        ),
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
    use_stream = _default_stream_preference() if stream is None else stream

    # Parse CLI inputs into domain objects up front. Any parse error is a
    # ConfigError → exit code 4 so the user sees a friendly message.
    try:
        tools_override, mcp_specs, file_search_spec, extra_parts, vis_literal = _parse_flag_inputs(
            tool_names=tools,
            mcp_tokens=mcps,
            mcp_header_tokens=mcp_headers,
            files=files,
            urls=urls,
            file_search_stores=file_search_stores,
            visualization=visualization,
        )
    except ConfigError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=exc.exit_code) from exc

    # --untrusted-input OR (files/urls + safe_untrusted = true) triggers tool
    # stripping. The SecurityPolicy owns the actual filtering.
    auto_untrusted = bool((files or urls) and config.safe_untrusted)
    effective_untrusted = untrusted_input or auto_untrusted

    # --plan kicks off the interactive planning loop before we hit the
    # execution path. The user's query becomes the seed input for the
    # planning phase; we feed the approved plan id into execute_research
    # below so the final run inherits plan context.
    previous_interaction_id: str | None = None
    approve_input: str | None = None
    if use_plan and not dry_run:
        client = _safe_build_client(console, api_key=api_key, config=config)
        plan_id = interactive_plan_loop(
            client,
            initial_query=query,
            agent=_resolve_agent(config, use_max=use_max),
            console=console,
        )
        if plan_id is None:
            console.print("[yellow]Plan cancelled.[/yellow]")
            raise typer.Exit(code=0)
        previous_interaction_id = plan_id
        approve_input = "Plan looks good!"
        console.print(f"[green]Plan approved.[/green]  id=[dim]{plan_id}[/dim]")

    execute_research(
        config=config,
        display_query=query,
        use_max=use_max,
        use_stream=use_stream,
        output=output,
        api_key=api_key,
        no_confirm=no_confirm,
        console=console,
        dry_run=dry_run,
        previous_interaction_id=previous_interaction_id,
        api_input=approve_input,
        plan_mode_for_dry_run=use_plan,
        builtin_tools=tools_override,
        mcp_servers=mcp_specs,
        file_search=file_search_spec,
        input_parts=extra_parts,
        visualization=vis_literal,
        untrusted_input=effective_untrusted,
    )


# ---------------------------------------------------------------------------
# Public helper — reused by gdr.commands.plan.approve_cmd
# ---------------------------------------------------------------------------


def execute_research(
    *,
    config: Config,
    display_query: str,
    use_max: bool,
    use_stream: bool,
    output: Path | None,
    api_key: str | None,
    no_confirm: bool,
    console: Console,
    dry_run: bool = False,
    previous_interaction_id: str | None = None,
    api_input: str | None = None,
    plan_mode_for_dry_run: bool = False,
    builtin_tools: tuple[str, ...] | None = None,
    mcp_servers: tuple[McpSpec, ...] = (),
    file_search: FileSearchSpec | None = None,
    input_parts: tuple[InputPart, ...] = (),
    visualization: str | None = None,
    untrusted_input: bool = False,
) -> None:
    """Run the full submit → stream/poll → render pipeline.

    Shared between ``gdr research`` (with or without ``--plan``) and
    ``gdr plan approve``. When ``previous_interaction_id`` is set, the
    created interaction inherits the plan context and ``api_input``
    (typically ``"Plan looks good!"``) replaces the display query on the
    wire.

    ``plan_mode_for_dry_run`` is set by ``run`` when ``--plan --dry-run``
    is combined; it makes the printed kwargs describe the *plan* phase
    rather than the execution phase, so users see what the planning call
    would look like.
    """
    policy = SecurityPolicy(
        output_root=config.output_dir,
        safe_untrusted=config.safe_untrusted,
        untrusted=untrusted_input,
    )

    # For --dry-run we don't need network or API key. The synthetic
    # output_dir needs to pass validation but doesn't need to exist.
    dry_output = output if output is not None else config.output_dir / "(dry-run)"

    try:
        ctx_for_kwargs = _build_run_context(
            query=display_query,
            config=config,
            use_max=use_max,
            output_dir=dry_output,
            stream=use_stream,
            previous_interaction_id=previous_interaction_id,
            builtin_tools=builtin_tools,
            mcp_servers=mcp_servers,
            file_search=file_search,
            input_parts=input_parts,
            visualization=visualization,
            untrusted_input=untrusted_input,
        )
        kwargs, stripped = _build_request_kwargs(
            ctx_for_kwargs,
            policy,
            api_input=api_input,
            plan_mode_for_dry_run=plan_mode_for_dry_run,
            dry_run=dry_run,
        )
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
    # (--no-confirm) or globally (confirm_max = false in config). Also skip
    # when we're continuing from an approved plan — they've already
    # consented.
    if (
        use_max
        and ctx_for_kwargs.confirm_max
        and not no_confirm
        and previous_interaction_id is None
        and not _confirm_max(console)
    ):
        console.print("[yellow]Aborted.[/yellow]")
        raise typer.Exit(code=0)

    client = _safe_build_client(console, api_key=api_key, config=config)

    started_at = datetime.now(_UTC)
    try:
        create_result = client.interactions.create(**kwargs)
    except Exception as exc:  # pragma: no cover - surface SDK errors as GdrError
        console.print(f"[red]Failed to start research:[/red] {exc}")
        raise typer.Exit(code=5) from exc

    try:
        interaction_id = _consume_create_result(
            create_result,
            use_stream=use_stream,
            console=console,
            query=display_query,
        )
    except StreamError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=exc.exit_code) from exc

    if not interaction_id:
        console.print("[red]API returned no interaction id; cannot proceed.[/red]")
        raise typer.Exit(code=5)

    console.print(f"[green]Research started.[/green]  id=[dim]{interaction_id}[/dim]")

    final_output_dir = _allocate_output_dir(
        root=config.output_dir,
        query=display_query,
        interaction_id=interaction_id,
        started_at=started_at,
        override=output,
        policy=policy,
    )

    ctx = ctx_for_kwargs.model_copy(update={"output_dir": final_output_dir})

    _finalize_and_render(
        client=client,
        ctx=ctx,
        interaction_id=interaction_id,
        final_output_dir=final_output_dir,
        policy=policy,
        started_at=started_at,
        console=console,
        query=display_query,
    )


def _build_request_kwargs(
    ctx: RunContext,
    policy: SecurityPolicy,
    *,
    api_input: str | None,
    plan_mode_for_dry_run: bool,
    dry_run: bool,
) -> tuple[dict[str, Any], list[str]]:
    """Build create() kwargs with optional api_input override and plan-mode
    preview for --dry-run.

    When ``plan_mode_for_dry_run`` is True (set only by --plan --dry-run),
    we show what the first *plan* request would look like instead of the
    execution request, since that's what we'd actually send first.
    """
    if plan_mode_for_dry_run and dry_run:
        from gdr.core.planning import PlanRequest, build_plan_kwargs  # noqa: PLC0415 — lazy

        req = PlanRequest(input_text=ctx.query, agent=ctx.agent)
        return build_plan_kwargs(req), []

    kwargs, stripped = build_create_kwargs(ctx, policy)
    if api_input is not None:
        kwargs["input"] = api_input
    return kwargs, stripped


def _safe_build_client(console: Console, *, api_key: str | None, config: Config) -> GdrClient:
    """Build a GdrClient with clear error messaging."""
    import os  # noqa: PLC0415 — only needed for API key env lookup

    resolved_key = _resolve_api_key(api_key, dict(os.environ), config)
    try:
        return GdrClient(api_key=resolved_key)
    except ConfigError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=exc.exit_code) from exc


# ---------------------------------------------------------------------------
# Small helpers kept private to this module
# ---------------------------------------------------------------------------


def _finalize_and_render(
    *,
    client: GdrClient,
    ctx: RunContext,
    interaction_id: str,
    final_output_dir: Path,
    policy: SecurityPolicy,
    started_at: datetime,
    console: Console,
    query: str,
) -> None:
    """Fetch authoritative outputs, render artifacts, record, announce.

    Split out of ``run`` to keep that function readable (and within lint
    thresholds). The flow is:

    1. Call ``.get(id=...)`` once — if terminal, use it.
    2. Otherwise, the run is still going (most likely because we just
       streamed through the normal path where ``interaction.complete``
       arrives before ``status`` flips in the ``.get`` projection, or
       because we disconnected mid-stream). Fall through to the Rich
       live-status polling helper until terminal.
    3. Write artifacts, append a Record, print the paths.
    """
    try:
        latest = client.interactions.get(id=interaction_id)
        status = getattr(latest, "status", None) or (
            latest.get("status") if isinstance(latest, dict) else None
        )
        interaction = (
            latest
            if status in TERMINAL_STATUSES
            else run_with_live_status(
                client.interactions.get,
                interaction_id,
                console=console,
                query=query,
            )
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


def _consume_create_result(
    create_result: Any,
    *,
    use_stream: bool,
    console: Console,
    query: str,
) -> str | None:
    """Extract the interaction id from ``client.interactions.create()``.

    For non-streaming calls the SDK returns a partial Interaction object —
    we pull ``.id`` directly. For streaming calls it returns an iterator
    yielding SSE events; we consume it through the live UI, which extracts
    the id on the ``interaction.start`` event and surfaces disconnects
    gracefully by returning the id anyway (the caller polls to finish).

    Raises :class:`StreamError` on an explicit error event.
    """
    if not use_stream:
        return getattr(create_result, "id", None) or (
            create_result.get("id") if isinstance(create_result, dict) else None
        )

    def _on_disconnect(exc: Exception) -> None:
        console.print(
            f"[yellow]Stream disconnected ({type(exc).__name__}); "
            f"falling through to polling.[/yellow]"
        )

    result = stream_with_live_ui(
        create_result, console=console, query=query, on_disconnect=_on_disconnect
    )
    return result.interaction_id


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
