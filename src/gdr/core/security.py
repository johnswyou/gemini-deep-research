"""Security policy — one place to audit.

Responsibilities:

1. **MCP header validation** — names must match a safe character set; values
   must not contain CR/LF (header injection); reserved hop-by-hop headers
   are rejected outright.
2. **Output path confinement** — every artifact path must resolve to a
   location under the configured output root. Slugs derived from the user's
   query are sanitized to a conservative charset.
3. **Redaction** — when writing `transcript.json`, `doctor` output, or
   error messages, sensitive fields (MCP auth headers, API keys) are
   replaced with ``[REDACTED]``.
4. **Untrusted-input tool filtering** — when `--untrusted-input` is active,
   or when the user has attached `--file`/`--url` inputs under
   `safe_untrusted = true`, certain dangerous tools are removed from the
   outgoing request.

These primitives are exposed both as module-level functions (for ad-hoc
use) and gathered on a :class:`SecurityPolicy` value that threads through
the request flow.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gdr.constants import TOOL_CODE_EXECUTION, TOOL_MCP_SERVER
from gdr.errors import ConfigError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-]{0,63}$")
_FORBIDDEN_HEADER_CHARS = re.compile(r"[\r\n\x00]")

# Hop-by-hop and otherwise-reserved HTTP headers that must not be overridden.
_RESERVED_HEADERS: frozenset[str] = frozenset(
    h.lower()
    for h in (
        "Host",
        "Content-Length",
        "Connection",
        "Transfer-Encoding",
        "Upgrade",
        "Proxy-Authorization",
        "TE",
        "Trailer",
        "Expect",
    )
)

_SLUG_NOISE_RE = re.compile(r"[^a-z0-9]+")
_SLUG_MAX_LEN = 64

# Substrings of header names whose values should be redacted in transcripts.
_REDACT_HEADER_SUBSTRINGS: tuple[str, ...] = ("auth", "token", "key", "secret", "cookie")

# Tools disabled when running under untrusted-input mode.
UNTRUSTED_DISALLOWED_TOOLS: frozenset[str] = frozenset({TOOL_CODE_EXECUTION, TOOL_MCP_SERVER})

REDACTED = "[REDACTED]"

# ---------------------------------------------------------------------------
# Header validation
# ---------------------------------------------------------------------------


def validate_mcp_header(name: str, value: str) -> None:
    """Raise ConfigError if the header name/value pair is unsafe.

    Rules:

    * Name must match ``[A-Za-z0-9][A-Za-z0-9-]{0,63}`` — rejects spaces,
      colons, and anything that could sneak into the request line.
    * Name must not be a hop-by-hop or reserved header.
    * Value must not contain CR, LF, or NUL — the classic header-injection
      payload shape.
    """
    if not isinstance(name, str) or not _HEADER_NAME_RE.match(name):
        raise ConfigError(
            f"Invalid MCP header name {name!r}: must match [A-Za-z0-9-]+ "
            f"and be no longer than 64 characters."
        )
    if name.lower() in _RESERVED_HEADERS:
        raise ConfigError(f"MCP header {name!r} is reserved and cannot be overridden.")
    if not isinstance(value, str) or _FORBIDDEN_HEADER_CHARS.search(value):
        raise ConfigError(
            f"MCP header {name!r} has an invalid value: CR, LF, and NUL "
            f"characters are not permitted (header injection prevention)."
        )


def validate_mcp_headers(headers: dict[str, str]) -> None:
    """Validate every entry in an MCP server's headers dict."""
    for name, value in headers.items():
        validate_mcp_header(name, value)


# ---------------------------------------------------------------------------
# Slug + path confinement
# ---------------------------------------------------------------------------


def sanitize_slug(text: str, *, max_length: int = _SLUG_MAX_LEN) -> str:
    """Turn arbitrary user text into a filesystem-safe slug.

    Lowercases, collapses non-alphanumeric runs to single dashes, trims
    leading/trailing dashes, and caps the length. Never returns an empty
    string — falls back to ``"query"`` if the input contains no safe chars.
    """
    lowered = text.lower()
    collapsed = _SLUG_NOISE_RE.sub("-", lowered).strip("-")
    truncated = collapsed[:max_length].strip("-")
    return truncated or "query"


def ensure_under_root(candidate: Path, root: Path) -> Path:
    """Resolve ``candidate`` and ensure it lives under ``root``.

    Raises ConfigError on escape attempts (e.g. slugs containing ``..``
    embedded in tricky Unicode that somehow defeats sanitize_slug, or
    symlinks pointing outside the tree).
    """
    resolved_root = root.resolve()
    resolved_candidate = candidate.resolve()
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ConfigError(
            f"Refusing to write outside the configured output_dir. "
            f"Root: {resolved_root}; attempted: {resolved_candidate}."
        ) from exc
    return resolved_candidate


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def _should_redact_header_name(name: str) -> bool:
    lowered = name.lower()
    return any(token in lowered for token in _REDACT_HEADER_SUBSTRINGS)


def _redact_headers(headers: dict[str, Any]) -> dict[str, Any]:
    return {
        name: (REDACTED if _should_redact_header_name(str(name)) else value)
        for name, value in headers.items()
    }


def redact_sensitive(data: Any) -> Any:
    """Return a deep copy of ``data`` with known sensitive fields redacted.

    Walks dicts and lists recursively. Any dict keyed ``"headers"`` has its
    values redacted for header names containing auth/token/key/secret/cookie
    substrings. Any top-level or nested key named ``"api_key"`` is replaced
    with ``[REDACTED]`` regardless of depth.

    This is a best-effort redactor intended for `transcript.json` output
    and error diagnostics — it is not a substitute for keeping secrets out
    of the payload in the first place.
    """
    if isinstance(data, dict):
        out: dict[str, Any] = {}
        for key, value in data.items():
            if key == "api_key":
                out[key] = REDACTED
            elif key == "headers" and isinstance(value, dict):
                out[key] = _redact_headers(value)
            else:
                out[key] = redact_sensitive(value)
        return out
    if isinstance(data, list):
        return [redact_sensitive(item) for item in data]
    return copy.deepcopy(data) if isinstance(data, (set, tuple)) else data


# ---------------------------------------------------------------------------
# Untrusted input filtering
# ---------------------------------------------------------------------------


def filter_tools_for_untrusted(
    tools: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Remove tools disallowed under untrusted-input mode.

    Returns the filtered tool list and the list of tool ``type`` strings
    that were stripped, so the caller can show a warning.
    """
    kept: list[dict[str, Any]] = []
    stripped: list[str] = []
    for tool in tools:
        tool_type = tool.get("type")
        if isinstance(tool_type, str) and tool_type in UNTRUSTED_DISALLOWED_TOOLS:
            stripped.append(tool_type)
        else:
            kept.append(tool)
    return kept, stripped


# ---------------------------------------------------------------------------
# SecurityPolicy object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SecurityPolicy:
    """Value object bundling the live security settings for a run.

    Construct once at the command-handler boundary and pass downstream.
    Every method is a thin alias over a module-level function so callers
    can either use the policy or the function, whichever reads better in
    context.
    """

    output_root: Path
    safe_untrusted: bool = True
    untrusted: bool = False

    # -- header / tool validation --------------------------------------

    def validate_mcp_headers(self, headers: dict[str, str]) -> None:
        validate_mcp_headers(headers)

    def filter_tools(self, tools: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
        """Apply untrusted-input tool filtering if active."""
        if self.untrusted:
            return filter_tools_for_untrusted(tools)
        return list(tools), []

    # -- path confinement ----------------------------------------------

    def confine(self, candidate: Path) -> Path:
        return ensure_under_root(candidate, self.output_root)

    def output_subdir(self, slug: str, interaction_id: str) -> Path:
        """Build a confined subdirectory path under the output root.

        Layout matches the plan: ``<output_root>/<iso_ts>_<slug>_<id6>``.
        The timestamp component is caller-supplied via the ``slug`` argument
        when that makes sense; callers typically prefix with a timestamp
        before calling here (keeps the function single-purpose).
        """
        sanitized = sanitize_slug(slug)
        id_fragment = re.sub(r"[^A-Za-z0-9]+", "", interaction_id)[:6] or "noid"
        candidate = self.output_root / f"{sanitized}_{id_fragment}"
        return self.confine(candidate)

    # -- redaction -----------------------------------------------------

    @staticmethod
    def redact(data: Any) -> Any:
        return redact_sensitive(data)
