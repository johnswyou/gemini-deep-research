"""Parse CLI flags into domain-model building blocks.

Separated from ``commands/research.py`` so the command module stays small
and focused on orchestration. All parsers raise :class:`ConfigError` with
actionable messages — never bare ``ValueError`` — so the CLI layer can
map them to exit code 4 uniformly.

The functions in this module are pure: they read inputs, validate them,
and return immutable domain objects. Network and filesystem side effects
are limited to :func:`parse_file` (which reads the local file to base64).
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Literal

from gdr.constants import SIMPLE_TOOLS, TOOL_FILE_SEARCH, TOOL_MCP_SERVER, TOOL_URL_CONTEXT
from gdr.core.models import FileSearchSpec, McpSpec, MediaKind, MediaPart, TextPart
from gdr.errors import ConfigError

# ---------------------------------------------------------------------------
# --tool validation
# ---------------------------------------------------------------------------


def validate_tool_names(tools: list[str]) -> tuple[str, ...]:
    """Ensure every ``--tool`` value is a simple builtin tool.

    ``file_search`` and ``mcp_server`` are configured tools; the user
    should use ``--file-search-store`` / ``--mcp`` instead. We reject them
    here with a message that points at the right flag.
    """
    out: list[str] = []
    for raw in tools:
        name = raw.strip()
        if name in {TOOL_FILE_SEARCH, TOOL_MCP_SERVER}:
            hint = "--file-search-store" if name == TOOL_FILE_SEARCH else "--mcp"
            raise ConfigError(f"--tool {name!r} requires configuration; use {hint} to wire it up.")
        if name not in SIMPLE_TOOLS:
            raise ConfigError(f"Unknown --tool {name!r}. Simple tools: {list(SIMPLE_TOOLS)}")
        out.append(name)
    return tuple(out)


# ---------------------------------------------------------------------------
# --file → MediaPart
# ---------------------------------------------------------------------------

_DEFAULT_MIME = "application/octet-stream"


def _media_kind_for_mime(mime: str) -> MediaKind:
    """Map a MIME type to the API's coarse-grained media kind.

    Deep Research accepts `image`/`document`/`audio`/`video`. Anything that
    doesn't fit the first three is treated as a document (PDFs, CSVs, plain
    text, JSON, etc.), matching how the API categorizes them.
    """
    top = mime.split("/", 1)[0].lower()
    if top == "image":
        return "image"
    if top == "audio":
        return "audio"
    if top == "video":
        return "video"
    return "document"


def parse_file(path: Path) -> MediaPart:
    """Read a local file, base64-encode it, and wrap in a MediaPart.

    Raises ConfigError if the file doesn't exist or is unreadable.
    """
    resolved = path.expanduser()
    if not resolved.is_file():
        raise ConfigError(f"--file path does not exist or is not a regular file: {path}")
    try:
        raw = resolved.read_bytes()
    except OSError as exc:
        raise ConfigError(f"Failed to read --file {path}: {exc}") from exc
    mime, _ = mimetypes.guess_type(resolved.name)
    if not mime:
        mime = _DEFAULT_MIME
    data = base64.b64encode(raw).decode("ascii")
    return MediaPart(type=_media_kind_for_mime(mime), data=data, mime_type=mime)


def parse_files(paths: list[Path]) -> tuple[MediaPart, ...]:
    """Batch-parse a list of --file paths."""
    return tuple(parse_file(p) for p in paths)


# ---------------------------------------------------------------------------
# --url → TextPart + url_context tool
# ---------------------------------------------------------------------------


def urls_as_text_part(urls: list[str]) -> TextPart | None:
    """Render a list of URLs into a single supplementary TextPart.

    Returns None when the list is empty so callers can skip appending.
    """
    cleaned = [u.strip() for u in urls if u and u.strip()]
    if not cleaned:
        return None
    body = "Additional URLs to consider:\n" + "\n".join(cleaned)
    return TextPart(text=body)


def ensure_url_context_tool(tools: tuple[str, ...], *, has_urls: bool) -> tuple[str, ...]:
    """If the user passed --url but left url_context out of --tool, add it."""
    if not has_urls or TOOL_URL_CONTEXT in tools:
        return tools
    return (*tools, TOOL_URL_CONTEXT)


# ---------------------------------------------------------------------------
# --mcp and --mcp-header → McpSpec[]
# ---------------------------------------------------------------------------

_MCP_HELP = "--mcp expects 'NAME=URL', e.g. --mcp deploys=https://mcp.example.com"
_HEADER_HELP = (
    "--mcp-header expects 'NAME=Key:Value', e.g. --mcp-header deploys=Authorization:Bearer abc"
)


def parse_mcp_spec_token(raw: str, headers_by_name: dict[str, dict[str, str]]) -> McpSpec:
    """Turn a ``--mcp`` token into an :class:`McpSpec` with any collected headers."""
    if "=" not in raw:
        raise ConfigError(f"{_MCP_HELP}. Got {raw!r}.")
    name_part, _, url_part = raw.partition("=")
    name = name_part.strip()
    url = url_part.strip()
    if not name or not url:
        raise ConfigError(f"{_MCP_HELP}. Got {raw!r}.")
    headers = headers_by_name.get(name, {})
    try:
        return McpSpec(name=name, url=url, headers=headers)
    except ValueError as exc:
        raise ConfigError(f"Invalid --mcp {raw!r}: {exc}") from exc


def parse_mcp_header_token(raw: str) -> tuple[str, str, str]:
    """Parse ``--mcp-header NAME=Key:Value`` into ``(name, key, value)``.

    Splits only on the *first* ``=`` and *first* ``:`` so header values
    containing ``:`` (e.g. ``Bearer abc:123``) survive intact.
    """
    if "=" not in raw:
        raise ConfigError(f"{_HEADER_HELP}. Got {raw!r}.")
    name_part, _, rest = raw.partition("=")
    name = name_part.strip()
    if not name:
        raise ConfigError(f"{_HEADER_HELP}. Got {raw!r}.")
    if ":" not in rest:
        raise ConfigError(f"{_HEADER_HELP}. Got {raw!r}.")
    key_part, _, value_part = rest.partition(":")
    key = key_part.strip()
    value = value_part.strip()
    if not key:
        raise ConfigError(f"{_HEADER_HELP}. Got {raw!r}.")
    return name, key, value


def parse_mcps(mcp_tokens: list[str], header_tokens: list[str]) -> tuple[McpSpec, ...]:
    """Assemble :class:`McpSpec` instances from raw CLI tokens.

    Duplicate ``--mcp`` names are rejected. Any ``--mcp-header`` referring
    to a NAME that has no matching ``--mcp`` declaration is also rejected
    so typos surface loudly instead of silently dropping headers.
    """
    headers_by_name: dict[str, dict[str, str]] = {}
    for token in header_tokens:
        name, key, value = parse_mcp_header_token(token)
        headers_by_name.setdefault(name, {})[key] = value

    specs: list[McpSpec] = []
    seen_names: set[str] = set()
    for token in mcp_tokens:
        spec = parse_mcp_spec_token(token, headers_by_name)
        if spec.name in seen_names:
            raise ConfigError(f"--mcp {spec.name!r} specified more than once.")
        seen_names.add(spec.name)
        specs.append(spec)

    orphans = sorted(set(headers_by_name) - seen_names)
    if orphans:
        raise ConfigError(
            f"--mcp-header references unknown MCP server(s): {orphans}. "
            f"Pass --mcp {orphans[0]}=<URL> first."
        )
    return tuple(specs)


# ---------------------------------------------------------------------------
# --file-search-store → FileSearchSpec
# ---------------------------------------------------------------------------

_STORE_PREFIX = "fileSearchStores/"


def parse_file_search_stores(names: list[str]) -> FileSearchSpec | None:
    """Normalize bare store names to the ``fileSearchStores/`` prefix.

    The API requires the prefix but typing it every time is tedious, so we
    accept ``--file-search-store my-store`` and ``--file-search-store
    fileSearchStores/my-store`` equivalently.
    """
    cleaned = [n.strip() for n in names if n and n.strip()]
    if not cleaned:
        return None
    prefixed = tuple(n if n.startswith(_STORE_PREFIX) else f"{_STORE_PREFIX}{n}" for n in cleaned)
    try:
        return FileSearchSpec(file_search_store_names=prefixed)
    except ValueError as exc:  # pragma: no cover - pydantic re-raises; belt + braces
        raise ConfigError(f"Invalid --file-search-store value: {exc}") from exc


# ---------------------------------------------------------------------------
# --visualization
# ---------------------------------------------------------------------------

_VisualizationLiteral = Literal["auto", "off"]


def validate_visualization(value: str | None) -> _VisualizationLiteral | None:
    """Return a validated ``auto``/``off`` literal or None when unset."""
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in {"auto", "off"}:
        return normalized  # type: ignore[return-value]
    raise ConfigError(f"--visualization must be 'auto' or 'off', got {value!r}.")
