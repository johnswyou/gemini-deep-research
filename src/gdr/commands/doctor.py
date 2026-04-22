"""``gdr doctor [--fix]`` — environment validation.

Runs a fixed list of checks against the local environment and prints a
colored table of results. Without ``--fix`` the command is read-only;
with ``--fix`` it creates missing directories and a minimal config
template. Exit code is 0 when no checks fail (warnings are OK), and 4
otherwise.

**Network probe is a DNS lookup**, not a real API call — ``doctor``
must not consume quota. The lookup targets
``generativelanguage.googleapis.com``; if your network blocks DNS to
Google, the check fails fast with a clear message.
"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path
from typing import Literal

import typer
from rich.console import Console
from rich.table import Table

from gdr.config import Config, default_config_path, load_config
from gdr.constants import MIN_GENAI_VERSION
from gdr.core.persistence import default_state_dir
from gdr.errors import ConfigError

_MIN_PYTHON: tuple[int, int] = (3, 10)
_API_HOST = "generativelanguage.googleapis.com"

CheckStatus = Literal["pass", "fail", "warn"]
CheckResult = tuple[str, CheckStatus, str]


def run(
    fix: bool = typer.Option(
        False,
        "--fix",
        help="Create missing directories and a minimal config template.",
    ),
    config_path: Path | None = typer.Option(
        None, "--config", help="Path to an alternate config TOML."
    ),
) -> None:
    """Validate the local environment for gdr."""
    console = Console()

    # Load config once (defensively — doctor must still report even if
    # the config is malformed).
    config: Config | None
    config_error: str | None = None
    try:
        config = load_config(path=config_path)
    except ConfigError as exc:
        config = None
        config_error = str(exc)

    results: list[CheckResult] = [
        _check_python(),
        _check_genai(),
        _check_config_file(config_path, config_error=config_error, fix=fix),
        _check_api_key(config),
        _check_network(),
        _check_output_dir(config, fix=fix),
        _check_state_dir(fix=fix),
    ]

    _render_table(console, results)

    fails = sum(1 for _, status, _ in results if status == "fail")
    if fails > 0:
        raise typer.Exit(code=4)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_python() -> CheckResult:
    current = sys.version_info[:2]
    ok = current >= _MIN_PYTHON
    detail = f"Python {sys.version.split()[0]} (required >= {'.'.join(map(str, _MIN_PYTHON))})"
    return "Python version", ("pass" if ok else "fail"), detail


def _check_genai() -> CheckResult:
    try:
        from google import genai  # noqa: PLC0415 — lazy import for startup cost
    except ImportError:
        return (
            "google-genai installed",
            "fail",
            f"package missing; install google-genai>={MIN_GENAI_VERSION}",
        )
    version = getattr(genai, "__version__", "unknown")
    return (
        "google-genai installed",
        "pass",
        f"version={version} (required >= {MIN_GENAI_VERSION})",
    )


def _check_config_file(
    config_path: Path | None,
    *,
    config_error: str | None,
    fix: bool,
) -> CheckResult:
    target = config_path if config_path is not None else default_config_path()
    if config_error is not None:
        return "config file", "fail", config_error
    if not target.exists():
        if fix:
            target.parent.mkdir(parents=True, exist_ok=True)
            _write_template(target)
            return "config file", "pass", f"created template at {target}"
        return (
            "config file",
            "warn",
            f"not found at {target}; run `gdr doctor --fix` or `gdr config edit`",
        )
    return "config file", "pass", str(target)


def _check_api_key(config: Config | None) -> CheckResult:
    env_key = os.environ.get("GEMINI_API_KEY")
    config_key = config.api_key if config else None
    resolved = env_key or config_key
    if not resolved:
        return (
            "API key available",
            "fail",
            "set GEMINI_API_KEY or `gdr config set api_key env:GEMINI_API_KEY`",
        )
    source = "GEMINI_API_KEY env" if env_key else "config"
    return "API key available", "pass", f"from {source}, fingerprint {_fingerprint(resolved)}"


def _check_network() -> CheckResult:
    try:
        socket.gethostbyname(_API_HOST)
    except OSError as exc:
        return (
            "Network reachable",
            "fail",
            f"cannot resolve {_API_HOST}: {exc}",
        )
    return "Network reachable", "pass", f"DNS OK for {_API_HOST}"


def _check_output_dir(config: Config | None, *, fix: bool) -> CheckResult:
    target = config.output_dir if config else Path.home() / "gdr-reports"
    if not target.exists():
        if fix:
            try:
                target.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return "output_dir", "fail", f"could not create {target}: {exc}"
            return "output_dir", "pass", f"created {target}"
        return "output_dir", "warn", f"missing; run --fix to create ({target})"
    if not os.access(target, os.W_OK):
        return "output_dir", "fail", f"not writable: {target}"
    return "output_dir", "pass", str(target)


def _check_state_dir(*, fix: bool) -> CheckResult:
    target = default_state_dir()
    if not target.exists():
        if fix:
            try:
                target.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return "state_dir", "fail", f"could not create {target}: {exc}"
            return "state_dir", "pass", f"created {target}"
        return "state_dir", "warn", f"missing; run --fix to create ({target})"
    if not os.access(target, os.W_OK):
        return "state_dir", "fail", f"not writable: {target}"
    return "state_dir", "pass", str(target)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fingerprint(key: str) -> str:
    if len(key) < 10:
        return "****"
    return f"{key[:4]}…{key[-4:]}"


def _render_table(console: Console, results: list[CheckResult]) -> None:
    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("Check", style="cyan")
    table.add_column("Status", no_wrap=True)
    table.add_column("Detail", style="dim")
    for name, status, detail in results:
        table.add_row(name, _colored_status(status), detail)
    console.print(table)

    fails = sum(1 for _, s, _ in results if s == "fail")
    warns = sum(1 for _, s, _ in results if s == "warn")
    summary_bits: list[str] = []
    if fails > 0:
        summary_bits.append(f"[red]{fails} failing[/red]")
    if warns > 0:
        summary_bits.append(f"[yellow]{warns} warning(s)[/yellow]")
    if not summary_bits:
        summary_bits.append("[green]all checks pass[/green]")
    console.print()
    console.print(" · ".join(summary_bits))


def _colored_status(status: CheckStatus) -> str:
    return {
        "pass": "[green]PASS[/green]",
        "fail": "[red]FAIL[/red]",
        "warn": "[yellow]WARN[/yellow]",
    }[status]


def _write_template(path: Path) -> None:
    # Duplicated with commands.config intentionally — doctor shouldn't
    # import from config.py because doing so creates a circular module
    # dependency through the Typer app registration path.
    content = (
        "# gdr config\n"
        "# See README for the full list of keys.\n"
        "#\n"
        '# api_key = "env:GEMINI_API_KEY"\n'
        '# default_agent = "deep-research-preview-04-2026"\n'
        '# output_dir = "~/gdr-reports"\n'
        "# auto_open = true\n"
        "# confirm_max = true\n"
    )
    path.write_text(content, encoding="utf-8")
