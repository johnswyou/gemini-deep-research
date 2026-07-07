"""The primary ``gdr research <query>`` command.

The Typer entry point (:func:`run`) is a thin wrapper that parses flags
and delegates to :func:`execute_research`, which is also imported by
:mod:`gdr.commands.plan` so ``gdr plan approve <id>`` reuses the full
submit → stream/poll → render pipeline.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel

from gdr.commands._common import friendly_errors, stdout_is_tty
from gdr.config import Config, load_config
from gdr.constants import (
    AGENT_MAX,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_IN_PROGRESS,
    TERMINAL_STATUSES,
    TOOL_URL_CONTEXT,
)
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
from gdr.core.normalize import error_of, get_field, has_report_content, interaction_status
from gdr.core.persistence import JsonlStore, Store
from gdr.core.planning import interactive_plan_loop
from gdr.core.rendering import write_artifacts
from gdr.core.requests import build_create_kwargs
from gdr.core.security import SecurityPolicy, id_fragment, sanitize_slug
from gdr.errors import (
    ConfigError,
    GdrError,
    NetworkError,
    ResearchCancelledError,
    ResearchFailedError,
    StreamError,
)
from gdr.ui.live import stream_with_live_ui
from gdr.ui.progress import run_with_live_status

_UTC = timezone.utc

EXIT_INTERRUPTED = 130


@dataclass(frozen=True)
class _CreateOutcome:
    interaction_id: str | None
    fallback_outputs: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    interrupted: bool = False
    fallback_total_tokens: int | None = None


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

    An explicit ``override`` (``--output``) is honored verbatim — it is the
    user's own machine and their stated intent, so confinement to the
    configured ``output_dir`` does not apply. Derived paths (the default
    ``<ts>_<slug>_<id6>`` layout) ARE confined, since their slug component
    comes from arbitrary query text.
    """
    if override is not None:
        return override.expanduser().resolve()

    slug = sanitize_slug(query)
    ts = started_at.strftime("%Y-%m-%dT%H-%M")
    candidate = root / f"{ts}_{slug}_{id_fragment(interaction_id)}"
    return policy.confine(candidate)


def _resolve_agent(config: Config, *, use_max: bool) -> str:
    """``--max`` wins; otherwise honor the configured agent id as-is
    (Google may release new agents, so unknown ids are not rejected)."""
    return AGENT_MAX if use_max else config.default_agent


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
    model: str | None = None,
) -> RunContext:
    tools = config.default_tools if builtin_tools is None else builtin_tools
    effective_visualization = visualization if visualization is not None else config.visualization
    return RunContext(
        query=query,
        agent=model if model is not None else _resolve_agent(config, use_max=use_max),
        model=model,
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


def _merge_config_mcps(config: Config, cli_specs: tuple[McpSpec, ...]) -> tuple[McpSpec, ...]:
    """Attach config-declared ``[mcp_servers.*]`` entries to the run.

    CLI ``--mcp`` flags win on name collision (they are the more explicit
    intent); config servers are appended in name order so the wire shape
    stays deterministic.
    """
    cli_names = {spec.name for spec in cli_specs}
    merged = list(cli_specs)
    for name in sorted(config.mcp_servers):
        if name in cli_names:
            continue
        server = config.mcp_servers[name]
        try:
            merged.append(
                McpSpec(
                    name=name,
                    url=server.url,
                    headers=dict(server.headers),
                    allowed_tools=server.allowed_tools,
                )
            )
        except ValueError as exc:
            raise ConfigError(f"Invalid [mcp_servers.{name}] entry in config: {exc}") from exc
    return tuple(merged)


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


@friendly_errors
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
    use_stream = stdout_is_tty() if stream is None else stream

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

    # --url must guarantee the url_context tool regardless of where the
    # effective tool list came from. _parse_flag_inputs already covers the
    # --tool override case; cover narrowed config defaults here.
    if urls and tools_override is None and TOOL_URL_CONTEXT not in config.default_tools:
        tools_override = ensure_url_context_tool(tuple(config.default_tools), has_urls=True)

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
        # The Max cost gate must precede the *plan* interaction: the plan
        # itself already runs on the Max agent, and by approval time
        # previous_interaction_id is set, which skips the gate inside
        # execute_research.
        if use_max and config.confirm_max and not no_confirm and not _confirm_max(console):
            console.print("[yellow]Aborted.[/yellow]")
            raise typer.Exit(code=0)
        client = _safe_build_client(console, api_key=api_key, config=config)
        plan_id = interactive_plan_loop(
            client,
            initial_query=query,
            agent=_resolve_agent(config, use_max=use_max),
            console=console,
            input_parts=extra_parts,
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
    model: str | None = None,
) -> None:
    """Run the full submit → stream/poll → render pipeline.

    Shared between ``gdr research`` (with or without ``--plan``),
    ``gdr plan approve``, and ``gdr follow-up``. When
    ``previous_interaction_id`` is set, the created interaction inherits
    the parent context and ``api_input`` (e.g. ``"Plan looks good!"``)
    replaces the display query on the wire.

    ``model`` switches the run to a plain Gemini model instead of a Deep
    Research agent (lightweight follow-ups): ``model=`` on the wire, no
    ``agent_config``, no builtin/config tools, no Max confirmation.

    ``plan_mode_for_dry_run`` is set by ``run`` when ``--plan --dry-run``
    is combined; it makes the printed kwargs describe the *plan* phase
    rather than the execution phase, so users see what the planning call
    would look like.
    """
    policy = SecurityPolicy(
        output_root=config.output_dir,
        untrusted=untrusted_input,
    )

    # For --dry-run we don't need network or API key. The synthetic
    # output_dir needs to pass validation but doesn't need to exist.
    dry_output = output if output is not None else config.output_dir / "(dry-run)"

    try:
        if model is None:
            mcp_servers = _merge_config_mcps(config, mcp_servers)
        elif builtin_tools is None:
            # Plain-model follow-ups are lightweight Q&A over existing
            # context: no research tools unless explicitly requested.
            builtin_tools = ()
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
            model=model,
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

    # Untrusted mode strips mcp_server from the request, so there are no
    # credentials in flight to warn about.
    if not policy.untrusted:
        _warn_plaintext_mcp(console, ctx_for_kwargs.mcp_servers)

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
    create_outcome, interaction_id, recorded_dirs = _submit_interaction(
        client=client,
        kwargs=kwargs,
        use_stream=use_stream,
        console=console,
        display_query=display_query,
        config=config,
        output=output,
        policy=policy,
        ctx_for_kwargs=ctx_for_kwargs,
        started_at=started_at,
    )

    final_output_dir = recorded_dirs.get(interaction_id) or _allocate_output_dir(
        root=config.output_dir,
        query=display_query,
        interaction_id=interaction_id,
        started_at=started_at,
        override=output,
        policy=policy,
    )

    ctx = ctx_for_kwargs.model_copy(update={"output_dir": final_output_dir})

    if interaction_id not in recorded_dirs:
        _record_run(
            interaction={"id": interaction_id, "status": STATUS_IN_PROGRESS},
            ctx=ctx,
            started_at=started_at,
            finished_at=None,
        )

    if create_outcome.interrupted:
        _print_interrupted(console, interaction_id)
        raise typer.Exit(code=EXIT_INTERRUPTED)

    console.print(f"[green]Research started.[/green]  id=[dim]{interaction_id}[/dim]")

    try:
        _finalize_and_render(
            client=client,
            ctx=ctx,
            interaction_id=interaction_id,
            final_output_dir=final_output_dir,
            policy=policy,
            started_at=started_at,
            console=console,
            query=display_query,
            fallback_outputs=create_outcome.fallback_outputs,
            fallback_total_tokens=create_outcome.fallback_total_tokens,
        )
    except KeyboardInterrupt:
        _print_interrupted(console, interaction_id)
        raise typer.Exit(code=EXIT_INTERRUPTED) from None


def _submit_interaction(
    *,
    client: GdrClient,
    kwargs: dict[str, Any],
    use_stream: bool,
    console: Console,
    display_query: str,
    config: Config,
    output: Path | None,
    policy: SecurityPolicy,
    ctx_for_kwargs: RunContext,
    started_at: datetime,
) -> tuple[_CreateOutcome, str, dict[str, Path]]:
    """Create the interaction and consume its stream when streaming.

    Returns the outcome, a validated interaction id, and the map of ids
    already recorded (with their allocated output dirs) by the stream's
    on-start callback. A mid-stream ``error`` event exits with a reattach
    hint — the run was recorded when the stream announced its id — and an
    interrupt before any id is known exits 130.
    """
    try:
        create_result = client.interactions.create(**kwargs)
    except Exception as exc:
        # google-genai's APIError carries the HTTP status in `.code`. A
        # rejected key is an auth problem (documented exit 4), not a
        # network failure (exit 5).
        if getattr(exc, "code", None) in (401, 403):
            raise ConfigError(
                f"The API rejected the request as unauthorized: {exc}. "
                f"Check your API key (`gdr doctor` shows which one is active)."
            ) from exc
        raise NetworkError(f"Failed to start research: {exc}") from exc

    record_stream_start, recorded_dirs = _make_stream_start_recorder(
        config=config,
        display_query=display_query,
        started_at=started_at,
        output=output,
        policy=policy,
        ctx_for_kwargs=ctx_for_kwargs,
    )

    try:
        create_outcome = _consume_create_result(
            create_result,
            use_stream=use_stream,
            console=console,
            query=display_query,
            client=client,
            on_interaction_id=record_stream_start,
        )
    except StreamError as exc:
        # An external `gdr cancel` kills the stream with a generic
        # api_error event; the interaction's real status says what
        # actually happened. Exit 2 (documented "cancelled") when so.
        if exc.interaction_id and _current_status(client, exc.interaction_id) == STATUS_CANCELLED:
            console.print(
                f"[yellow]Research cancelled (interaction id: {exc.interaction_id}).[/yellow]\n"
                f"Artifacts were not written; `gdr resume {exc.interaction_id}` "
                f"renders the post-mortem."
            )
            raise typer.Exit(code=ResearchCancelledError.exit_code) from exc
        console.print(f"[red]{exc}[/red]")
        if exc.interaction_id:
            # The run was recorded when the stream announced its id; tell
            # the user how to pick it back up.
            _print_reattach_hint(console, exc.interaction_id)
        raise typer.Exit(code=exc.exit_code) from exc

    interaction_id = create_outcome.interaction_id
    if not interaction_id:
        if create_outcome.interrupted:
            console.print("[yellow]Interrupted before the API returned an id.[/yellow]")
            raise typer.Exit(code=EXIT_INTERRUPTED)
        raise NetworkError("API returned no interaction id; cannot proceed.")

    return create_outcome, interaction_id, recorded_dirs


def _make_stream_start_recorder(
    *,
    config: Config,
    display_query: str,
    started_at: datetime,
    output: Path | None,
    policy: SecurityPolicy,
    ctx_for_kwargs: RunContext,
) -> tuple[Callable[[str], None], dict[str, Path]]:
    """Build the on-start callback that records a streamed run early.

    Recovery commands (`gdr ls` / `status` / `resume`) must see
    interrupted, failed, and timed-out runs — not just the ones that
    finished cleanly. Streamed runs learn their id mid-stream
    (`interaction.created`), so the in_progress record is written from
    inside the stream via this callback; the polling path records right
    after create() returns instead. The terminal append at the end of the
    run overwrites the row (last write wins). The returned dict maps
    recorded ids to their allocated output dirs so the post-stream path
    can reuse them without re-recording.
    """
    recorded_dirs: dict[str, Path] = {}

    def _record(new_id: str) -> None:
        directory = _allocate_output_dir(
            root=config.output_dir,
            query=display_query,
            interaction_id=new_id,
            started_at=started_at,
            override=output,
            policy=policy,
        )
        recorded_dirs[new_id] = directory
        _record_run(
            interaction={"id": new_id, "status": STATUS_IN_PROGRESS},
            ctx=ctx_for_kwargs.model_copy(update={"output_dir": directory}),
            started_at=started_at,
            finished_at=None,
        )

    return _record, recorded_dirs


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

        req = PlanRequest(input_text=ctx.query, agent=ctx.agent, input_parts=ctx.input_parts)
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
    fallback_outputs: tuple[dict[str, Any], ...] = (),
    fallback_total_tokens: int | None = None,
) -> None:
    """Fetch authoritative outputs, render artifacts, record, announce.

    Split out of ``run`` to keep that function readable (and within lint
    thresholds). The flow is:

    1. Call ``.get(id=...)`` once — if terminal, use it.
    2. Otherwise, the run is still going (most likely because we just
       streamed through the normal path where the completion event
       arrives before ``status`` flips in the ``.get`` projection, or
       because we disconnected mid-stream). Fall through to the Rich
       live-status polling helper until terminal.
    3. Write artifacts and the terminal Record for EVERY terminal state —
       a failed run's transcript and metadata are exactly what you want
       for a post-mortem — then exit 0 only if it actually completed.
    """
    failure: GdrError | None = None
    try:
        try:
            latest = client.interactions.get(id=interaction_id)
        except Exception as exc:
            raise NetworkError(
                f"Failed to fetch interaction {interaction_id}: {exc}. "
                f"The research may still be running — try `gdr resume {interaction_id}`."
            ) from exc
        status = get_field(latest, "status")
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
    except (ResearchFailedError, ResearchCancelledError) as exc:
        # The poll saw a terminal failure. Re-fetch once (best-effort) so
        # artifacts capture whatever diagnostics the API exposes.
        failure = exc
        interaction = _refetch_terminal(client, interaction_id, fallback_status=exc)

    interaction = _with_fallback_outputs(
        interaction, fallback_outputs, fallback_total_tokens=fallback_total_tokens
    )
    finished_at = datetime.now(_UTC)

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

    final_status = str(get_field(interaction, "status") or "unknown")
    if failure is not None or final_status != STATUS_COMPLETED:
        _print_not_completed(
            console,
            status=final_status,
            interaction=interaction,
            paths=paths,
            failure=failure,
        )
        raise typer.Exit(code=_exit_code_for_status(final_status, failure))

    _print_done(console, paths)

    if ctx.auto_open and stdout_is_tty():
        typer.launch(str(paths["report"]))


def _consume_create_result(
    create_result: Any,
    *,
    use_stream: bool,
    console: Console,
    query: str,
    client: GdrClient | None = None,
    on_interaction_id: Callable[[str], None] | None = None,
) -> _CreateOutcome:
    """Extract the interaction id from ``client.interactions.create()``.

    For non-streaming calls the SDK returns a partial Interaction object —
    we pull ``.id`` directly. For streaming calls it returns an iterator
    yielding SSE events; we consume it through the live UI. A dropped
    stream is re-attached via ``interactions.get(id=..., stream=True,
    last_event_id=...)`` when the client supports it; only after the
    reconnect budget is spent do we fall back to polling.

    Streamed outputs are only offered as a rendering fallback when the
    stream finished cleanly AND didn't end in a failure status — partial
    or failed streams must not masquerade as a report.

    Raises :class:`StreamError` on an explicit error event.
    """
    if not use_stream:
        return _CreateOutcome(interaction_id=get_field(create_result, "id"))

    def _on_disconnect(exc: Exception) -> None:
        console.print(
            f"[yellow]Stream disconnected ({type(exc).__name__}); "
            f"falling through to polling.[/yellow]"
        )

    def _reconnect(interaction_id: str, last_event_id: str | None) -> Any:
        assert client is not None  # guarded by the `reconnect=` argument below
        kwargs: dict[str, Any] = {"id": interaction_id, "stream": True}
        if last_event_id:
            kwargs["last_event_id"] = last_event_id
        return client.interactions.get(**kwargs)

    result = stream_with_live_ui(
        create_result,
        console=console,
        query=query,
        on_disconnect=_on_disconnect,
        reconnect=_reconnect if client is not None else None,
        on_start=on_interaction_id,
    )
    clean_completion = result.completed_cleanly and result.status in (None, STATUS_COMPLETED)
    return _CreateOutcome(
        interaction_id=result.interaction_id,
        fallback_outputs=result.streamed_outputs if clean_completion else (),
        interrupted=result.interrupted,
        fallback_total_tokens=result.total_tokens if clean_completion else None,
    )


def _with_fallback_outputs(
    interaction: Any,
    fallback_outputs: tuple[dict[str, Any], ...],
    *,
    fallback_total_tokens: int | None = None,
) -> Any:
    """Attach cleanly streamed outputs when the terminal fetch has no report.

    The fetch is authoritative whenever it carries renderable report
    content — under the 2.x schema that content arrives in the ``steps``
    timeline, so the check goes through the normalizer, never a raw field
    (``Interaction`` has no ``outputs`` attribute to key on). The streamed
    buffer only stands in when the fetch has no report at all (the
    empty-fetch backends the v0.1.2 hotfix targeted).

    Also fills in usage from the stream's completion event when the fetch
    carries none, so streamed runs don't lose their token counts.
    """
    if not fallback_outputs or has_report_content(interaction):
        return interaction

    usage = get_field(interaction, "usage")
    if usage is None and fallback_total_tokens is not None:
        usage = {"total_tokens": fallback_total_tokens}

    outputs = [dict(output) for output in fallback_outputs]
    if isinstance(interaction, dict):
        return {**interaction, "outputs": outputs, "usage": usage}
    return {
        "id": get_field(interaction, "id"),
        "status": get_field(interaction, "status"),
        "outputs": outputs,
        "usage": usage,
        "error": get_field(interaction, "error"),
        # Merge, don't rebuild: keep the fetch's timeline and timestamps so
        # the transcript writer (which prefers ``steps``) and resume-time
        # consumers don't lose them when the streamed body stands in.
        "steps": get_field(interaction, "steps"),
        "updated": get_field(interaction, "updated"),
    }


def _warn_plaintext_mcp(console: Console, mcp_servers: tuple[McpSpec, ...]) -> None:
    """Flag MCP servers that would send auth headers over plain HTTP."""
    for spec in mcp_servers:
        if spec.url.startswith("http://") and spec.headers:
            console.print(
                f"[yellow]Warning:[/yellow] MCP server {spec.name!r} uses plain http:// "
                f"with auth headers — credentials will be sent unencrypted."
            )


def _current_status(client: GdrClient, interaction_id: str) -> str | None:
    """Best-effort status probe; None when the fetch itself fails."""
    try:
        return interaction_status(client.interactions.get(id=interaction_id))
    except Exception:
        return None


def _refetch_terminal(client: GdrClient, interaction_id: str, *, fallback_status: GdrError) -> Any:
    """Best-effort re-fetch after the poll reported a terminal failure.

    If the fetch itself fails we synthesize a minimal interaction so
    artifacts and the record still get written with the right status.
    """
    try:
        return client.interactions.get(id=interaction_id)
    except Exception:
        status = (
            STATUS_CANCELLED
            if isinstance(fallback_status, ResearchCancelledError)
            else STATUS_FAILED
        )
        return {"id": interaction_id, "status": status}


def _exit_code_for_status(status: str, failure: GdrError | None) -> int:
    if failure is not None:
        return failure.exit_code
    if status == STATUS_CANCELLED:
        return ResearchCancelledError.exit_code
    # failed, incomplete, or anything else terminal-but-not-completed.
    return ResearchFailedError.exit_code


def _print_not_completed(
    console: Console,
    *,
    status: str,
    interaction: Any,
    paths: dict[str, Path],
    failure: GdrError | None,
) -> None:
    detail = error_of(interaction)
    headline = str(failure) if failure is not None else f"Research ended with status: {status}."
    console.print(f"[red]{headline}[/red]")
    if detail:
        console.print(f"[red]Detail:[/red] {detail}")
    console.print(f"Partial artifacts (metadata + transcript) saved to: {paths['report'].parent}")


def _print_reattach_hint(console: Console, interaction_id: str) -> None:
    console.print(
        f"  Check on it:  [bold]gdr status {interaction_id}[/bold]\n"
        f"  Reattach:     [bold]gdr resume {interaction_id}[/bold]"
    )


def _print_interrupted(console: Console, interaction_id: str) -> None:
    console.print()
    console.print("[yellow]Interrupted.[/yellow] The research continues server-side.")
    _print_reattach_hint(console, interaction_id)


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
    finished_at: datetime | None,
    store: Store | None = None,
) -> None:
    """Append a Record describing this run to the local store.

    Called twice per run: once with status ``in_progress`` as soon as the
    interaction id is known (``finished_at=None``), and once with the
    terminal state. ``JsonlStore`` is last-write-wins per id, so the
    second append supersedes the first.

    The ``store`` parameter is injectable so tests can pass a memory-backed
    fake. In normal use we open the default JsonlStore just-in-time.
    """
    interaction_id = get_field(interaction, "id")
    if not interaction_id:
        return  # Nothing actionable to record.

    tools = list(ctx.builtin_tools)
    if ctx.file_search is not None:
        tools.append("file_search")
    tools.extend("mcp_server" for _ in ctx.mcp_servers)

    total_tokens = get_field(get_field(interaction, "usage"), "total_tokens")

    record = Record(
        id=str(interaction_id),
        parent_id=ctx.previous_interaction_id,
        created_at=started_at,
        finished_at=finished_at,
        status=str(get_field(interaction, "status") or "unknown"),
        agent=ctx.agent,
        query=ctx.query,
        output_dir=ctx.output_dir,
        total_tokens=total_tokens,
        tools=tuple(tools),
        untrusted=ctx.untrusted_input,
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
    # On a pipe, additionally emit the report path unwrapped and unstyled
    # as the final stdout line, so shell code can grab it with
    # `gdr research ... | tail -n 1` without parsing the Rich block.
    if not sys.stdout.isatty():
        sys.stdout.write(f"{paths['report']}\n")
