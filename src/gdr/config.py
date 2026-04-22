"""Configuration loader for gdr.

`~/.config/gdr/config.toml` is the canonical config location (overridable via
`GDR_CONFIG_PATH`). Values can reference environment variables using the
`env:VAR_NAME` syntax, which is expanded at load time. Missing env vars
surface as `ConfigError` with a clear message instead of failing later.

Example::

    api_key = "env:GEMINI_API_KEY"
    default_agent = "deep-research-preview-04-2026"
    output_dir = "~/gdr-reports"
    default_tools = ["google_search", "url_context", "code_execution"]

    [mcp_servers.factset]
    url = "https://mcp.factset.com"
    headers.Authorization = "Bearer env:FACTSET_TOKEN"
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on 3.10 only
    import tomli as tomllib

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic import ValidationError as PydanticValidationError

from gdr.constants import AGENT_FAST, DEFAULT_TOOLS, SIMPLE_TOOLS
from gdr.errors import ConfigError

ENV_PREFIX = "env:"

# ---------------------------------------------------------------------------
# Pydantic config models
# ---------------------------------------------------------------------------


class McpServerConfig(BaseModel):
    """A named MCP server entry in config TOML.

    Matches the shape of `[mcp_servers.<name>]` tables. Header values may
    themselves use `env:VAR` references.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str = Field(min_length=1)
    headers: dict[str, str] = Field(default_factory=dict)
    allowed_tools: tuple[str, ...] | None = None


class Config(BaseModel):
    """Fully-loaded gdr configuration.

    Always has defaults for every field, so an empty config file loads fine.
    The `api_key` may be None here; the client wrapper decides whether that's
    a hard error depending on whether the current command needs the API.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    api_key: str | None = None
    default_agent: str = AGENT_FAST
    output_dir: Path = Field(default_factory=lambda: Path.home() / "gdr-reports")
    auto_open: bool = True
    confirm_max: bool = True
    default_tools: tuple[str, ...] = DEFAULT_TOOLS
    thinking_summaries: str = "auto"
    visualization: str = "auto"
    safe_untrusted: bool = True
    mcp_servers: dict[str, McpServerConfig] = Field(default_factory=dict)

    @field_validator("output_dir", mode="before")
    @classmethod
    def _expand_output_dir(cls, v: Any) -> Path:
        if isinstance(v, Path):
            return v.expanduser().resolve()
        if isinstance(v, str):
            return Path(v).expanduser().resolve()
        raise TypeError(f"output_dir must be str or Path, got {type(v).__name__}")

    @field_validator("default_tools")
    @classmethod
    def _validate_tools(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for name in v:
            if name not in SIMPLE_TOOLS:
                raise ValueError(
                    f"{name!r} is not a simple builtin tool. Simple tools: {list(SIMPLE_TOOLS)}"
                )
        return v

    @field_validator("thinking_summaries")
    @classmethod
    def _validate_thinking(cls, v: str) -> str:
        if v not in {"auto", "none"}:
            raise ValueError(f"thinking_summaries must be 'auto' or 'none', got {v!r}")
        return v

    @field_validator("visualization")
    @classmethod
    def _validate_visualization(cls, v: str) -> str:
        if v not in {"auto", "off"}:
            raise ValueError(f"visualization must be 'auto' or 'off', got {v!r}")
        return v


# ---------------------------------------------------------------------------
# env:VAR expansion
# ---------------------------------------------------------------------------


def _expand_env_string(value: str, *, env: dict[str, str]) -> str:
    """Resolve an env:VAR reference.

    For values prefixed with ``env:``, look up ``VAR`` in ``env`` and return
    the result. Leading/trailing whitespace around the var name is allowed.
    If the variable is unset, raise ConfigError with a helpful message.

    Non-prefixed strings are returned unchanged. Notably, the prefix must be
    at the very start of the value — embedded ``env:`` tokens are not
    expanded (too easy to trip over when a legitimate string happens to
    contain that substring).
    """
    if not value.startswith(ENV_PREFIX):
        return value
    var = value[len(ENV_PREFIX) :].strip()
    if not var:
        raise ConfigError(
            f"Empty env reference {value!r}. Use `env:VAR_NAME` to pull from the environment."
        )
    resolved = env.get(var)
    if resolved is None:
        raise ConfigError(f"Config references env var ${var} but it is not set in the environment.")
    return resolved


def _walk_and_expand(data: Any, *, env: dict[str, str]) -> Any:
    """Recursively expand `env:VAR` references in a TOML-parsed structure."""
    if isinstance(data, str):
        return _expand_env_string(data, env=env)
    if isinstance(data, dict):
        return {k: _walk_and_expand(v, env=env) for k, v in data.items()}
    if isinstance(data, list):
        return [_walk_and_expand(v, env=env) for v in data]
    return data


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def default_config_path() -> Path:
    """Return the canonical config file path.

    Overridable via the ``GDR_CONFIG_PATH`` env var for tests and CI. We
    intentionally do *not* depend on `platformdirs` here — that package's
    `user_config_path` returns different locations on different platforms
    (`~/Library/Application Support/gdr` on macOS, for example), which
    surprises users who expect XDG-style `~/.config/gdr/`. The XDG path is
    conventional for terminal tooling; we prefer predictability.
    """
    override = os.environ.get("GDR_CONFIG_PATH")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "gdr" / "config.toml"
    return Path.home() / ".config" / "gdr" / "config.toml"


def load_config(
    path: Path | None = None,
    *,
    env: dict[str, str] | None = None,
) -> Config:
    """Load and validate the gdr config file.

    If the file does not exist, returns a default :class:`Config`. Missing
    env vars referenced by `env:VAR` become :class:`ConfigError`. Extra keys
    or invalid types raise :class:`ConfigError` with Pydantic's validation
    message.

    The ``env`` parameter is provided primarily for testing; it defaults to
    ``os.environ``.
    """
    source_env: dict[str, str] = dict(os.environ) if env is None else env
    target = path if path is not None else default_config_path()

    if not target.exists():
        return Config()

    try:
        raw_bytes = target.read_bytes()
    except OSError as exc:
        raise ConfigError(f"Failed to read config at {target}: {exc}") from exc

    try:
        raw: dict[str, Any] = tomllib.loads(raw_bytes.decode("utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {target}: {exc}") from exc

    try:
        expanded = _walk_and_expand(raw, env=source_env)
    except ConfigError:
        raise  # already well-formatted

    try:
        return Config.model_validate(expanded)
    except PydanticValidationError as exc:
        raise ConfigError(f"Invalid config at {target}:\n{_format_validation_error(exc)}") from exc


def _format_validation_error(exc: PydanticValidationError) -> str:
    lines: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "<root>"
        lines.append(f"  {loc}: {err['msg']}")
    return "\n".join(lines)
