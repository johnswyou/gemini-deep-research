"""``gdr config`` — manage the gdr TOML configuration.

Four subcommands:

* ``gdr config path`` — print the resolved config file path.
* ``gdr config get [KEY]`` — print the full config, or the value at
  ``KEY`` (dot-separated path, e.g. ``mcp_servers.factset.url``).
* ``gdr config set KEY VALUE`` — write a *top-level* scalar or list key
  into the config TOML. Nested keys (``mcp_servers.<x>.url``) must be
  edited via ``gdr config edit`` — the writer intentionally doesn't
  support partial nested writes to avoid subtle round-trip bugs.
* ``gdr config edit`` — open ``$EDITOR`` (or ``$VISUAL``, or ``vi``)
  on the config file, creating a minimal template if missing.

**Writer tradeoff:** ``gdr config set`` regenerates the TOML file from
the parsed dict, so **comments in the file are lost**. Users who need
comment-preserving edits should use ``gdr config edit`` instead.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from gdr.config import Config, default_config_path, load_config
from gdr.errors import ConfigError

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on 3.10 only
    import tomli as tomllib

app = typer.Typer(
    name="config",
    help="Manage the gdr TOML configuration.",
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


# ---------------------------------------------------------------------------
# `gdr config path`
# ---------------------------------------------------------------------------


@app.command("path", help="Print the resolved config file path.")
def path_cmd(
    config_path: Path | None = typer.Option(
        None, "--config", help="Path to an alternate config TOML."
    ),
) -> None:
    target = config_path if config_path is not None else default_config_path()
    # Plain stdout — this is meant to be captured by shell scripts
    # (``path=$(gdr config path)``) so no Rich formatting / wrapping.
    typer.echo(str(target))


# ---------------------------------------------------------------------------
# `gdr config get [KEY]`
# ---------------------------------------------------------------------------


@app.command("get", help="Print a config value or the whole config.")
def get_cmd(
    key: str | None = typer.Argument(
        None,
        help="Dot-separated path into the config "
        "(e.g. 'default_agent', 'mcp_servers.factset.url'). "
        "Omit to print the full config.",
    ),
    config_path: Path | None = typer.Option(
        None, "--config", help="Path to an alternate config TOML."
    ),
) -> None:
    console = Console()
    try:
        config = load_config(path=config_path)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=exc.exit_code) from exc

    data = config.model_dump(mode="json")
    if key is None:
        _print_pretty(console, data)
        return

    value, found = _lookup(data, key)
    if not found:
        console.print(f"[yellow]No such key:[/yellow] {key}")
        raise typer.Exit(code=4)

    if isinstance(value, (dict, list)):
        _print_pretty(console, value)
    else:
        console.print(str(value) if value is not None else "")


# ---------------------------------------------------------------------------
# `gdr config set KEY VALUE`
# ---------------------------------------------------------------------------


@app.command("set", help="Set a top-level scalar/list key in the config.")
def set_cmd(
    key: str = typer.Argument(
        ...,
        help="Top-level config key (e.g. 'default_agent'). "
        "Nested keys (mcp_servers.*) are not supported; use `gdr config edit` instead.",
    ),
    value: str = typer.Argument(
        ...,
        help="Value. Types are inferred: true/false → bool, integers → int, "
        "dotted numbers → float, otherwise string.",
    ),
    config_path: Path | None = typer.Option(
        None, "--config", help="Path to an alternate config TOML."
    ),
) -> None:
    console = Console()
    target = config_path if config_path is not None else default_config_path()

    if "." in key:
        console.print(
            f"[red]`gdr config set` only supports top-level keys.[/red]\n"
            f"For nested keys like {key!r}, edit the file directly: "
            f"[bold]gdr config edit[/bold]"
        )
        raise typer.Exit(code=4)

    target.parent.mkdir(parents=True, exist_ok=True)
    existing = _load_toml_raw(target)
    existing[key] = _infer_type(value)

    # Validate the resulting dict by round-tripping through the Pydantic
    # model. This catches typos, unknown keys, and invalid values before
    # we write anything to disk.
    try:
        Config.model_validate(existing)
    except Exception as exc:
        console.print(f"[red]Refusing to write invalid config:[/red] {exc}")
        raise typer.Exit(code=4) from exc

    _write_toml(target, existing)
    console.print(
        f"[green]Wrote[/green] [bold]{key}[/bold] = {existing[key]!r} [dim]→ {target}[/dim]"
    )


# ---------------------------------------------------------------------------
# `gdr config edit`
# ---------------------------------------------------------------------------


@app.command("edit", help="Open the config file in $EDITOR.")
def edit_cmd(
    config_path: Path | None = typer.Option(
        None, "--config", help="Path to an alternate config TOML."
    ),
) -> None:
    console = Console()
    target = config_path if config_path is not None else default_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        _write_template(target)
        console.print(f"[dim]Created template at {target}[/dim]")

    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    # shlex.split so EDITOR can contain args (e.g. "code --wait").
    cmd = [*shlex.split(editor), str(target)]
    if shutil.which(cmd[0]) is None:
        console.print(
            f"[red]Editor {cmd[0]!r} not found on PATH.[/red]\n"
            f"Set $EDITOR to a valid command or install {cmd[0]!r}."
        )
        raise typer.Exit(code=4)

    subprocess.run(cmd, check=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SENTINEL = object()


def _lookup(data: Any, key: str) -> tuple[Any, bool]:
    """Dot-separated-path lookup. Returns ``(value, found)``."""
    current: Any = data
    for part in key.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None, False
    return current, True


def _infer_type(value: str) -> Any:
    """Coerce a string from the CLI into a TOML-compatible Python value."""
    lowered = value.strip().lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    # Leave leading/trailing whitespace-only strings as strings.
    stripped = value.strip()
    if stripped and stripped[0] in "-+0123456789":
        try:
            return int(stripped)
        except ValueError:
            try:
                return float(stripped)
            except ValueError:
                pass
    return value


def _load_toml_raw(path: Path) -> dict[str, Any]:
    """Parse an existing TOML file, or return an empty dict."""
    if not path.exists():
        return {}
    try:
        parsed: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc
    return parsed


def _write_toml(path: Path, data: dict[str, Any]) -> None:
    """Regenerate the TOML file from a dict.

    Minimal, purpose-built for the :class:`Config` shape:

    * Scalars (bool, int, float, str) and simple lists at the top level.
    * ``mcp_servers`` as a dict-of-dicts → emitted as ``[mcp_servers.NAME]``
      tables, with a nested ``headers`` sub-table when present.

    **Limitation:** comments in the original file are lost. This is
    documented in the module docstring.
    """
    lines: list[str] = []

    # Top-level scalars and lists first.
    for key in sorted(data):
        value = data[key]
        if isinstance(value, dict):
            continue
        lines.append(f"{key} = {_format_value(value)}")

    # Nested tables: mcp_servers.<name> with optional headers sub-table.
    nested = {k: v for k, v in data.items() if isinstance(v, dict) and v}
    for parent_key in sorted(nested):
        parent_value = nested[parent_key]
        for child_key in sorted(parent_value):
            child_value = parent_value[child_key]
            if not isinstance(child_value, dict):
                # Top-level dict that isn't dict-of-dicts — unexpected for
                # the current Config shape, but handle gracefully.
                lines.extend(["", f"[{parent_key}]", f"{child_key} = {_format_value(child_value)}"])
                continue
            lines.append("")
            lines.append(f"[{parent_key}.{child_key}]")
            for leaf_key in sorted(child_value):
                leaf_value = child_value[leaf_key]
                if isinstance(leaf_value, dict):
                    # e.g. headers.Authorization
                    lines.append("")
                    lines.append(f"[{parent_key}.{child_key}.{leaf_key}]")
                    for k in sorted(leaf_value):
                        lines.append(f"{k} = {_format_value(leaf_value[k])}")
                else:
                    lines.append(f"{leaf_key} = {_format_value(leaf_value)}")

    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return _quote_string(value)
    if isinstance(value, (list, tuple)):
        inner = ", ".join(_format_value(v) for v in value)
        return f"[{inner}]"
    if isinstance(value, Path):
        return _quote_string(str(value))
    # Fallback — stringify anything else.
    return _quote_string(str(value))


def _quote_string(s: str) -> str:
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _write_template(path: Path) -> None:
    template = (
        "# gdr config\n"
        "# See README for the full list of keys.\n"
        "#\n"
        '# api_key = "env:GEMINI_API_KEY"\n'
        '# default_agent = "deep-research-preview-04-2026"\n'
        '# output_dir = "~/gdr-reports"\n'
        "# auto_open = true\n"
        "# confirm_max = true\n"
        '# default_tools = ["google_search", "url_context", "code_execution"]\n'
        '# thinking_summaries = "auto"\n'
        '# visualization = "auto"\n'
        "# safe_untrusted = true\n"
        "#\n"
        "# [mcp_servers.example]\n"
        '# url = "https://mcp.example.com"\n'
        '# headers.Authorization = "Bearer env:EXAMPLE_TOKEN"\n'
    )
    path.write_text(template, encoding="utf-8")


def _print_pretty(console: Console, data: Any) -> None:
    """Pretty-print a dict/value using Rich's JSON renderer."""
    import json  # noqa: PLC0415 — only used here

    console.print_json(json.dumps(data, default=str))


# Silence unused-import warnings from the sentinel that we keep around for
# future "missing key" detection (currently ``_lookup`` returns a bool).
_ = _SENTINEL
