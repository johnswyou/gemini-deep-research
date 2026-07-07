"""Thin, testable façade around `google.genai.Client`.

Responsibilities:

* Enforce that an API key is present before we try to construct the SDK client.
* Assert the installed `google-genai` SDK exposes the Interactions API
  (added in 1.55.0) so users get a helpful upgrade hint instead of an
  `AttributeError` at call time.
* Provide a stable attribute surface (`interactions`) that tests can mock
  without patching the SDK's internals.
* Never let the API key leak into logs, repr, or error messages beyond a
  short fingerprint.

Retry lives where the long waits are — ``gdr.ui.progress.poll_until_complete``
retries transient poll failures with backoff. This module stays small and
import-cheap so the CLI's startup cost and `gdr doctor` remain fast.
"""

from __future__ import annotations

import importlib.metadata
from typing import TYPE_CHECKING, Any

from gdr.constants import MIN_GENAI_VERSION
from gdr.errors import ConfigError

if TYPE_CHECKING:
    from google.genai import Client as GenaiClient


_MISSING_KEY_HINT = (
    "No Gemini API key found.\n"
    "  • Set GEMINI_API_KEY in your environment, or\n"
    '  • Add `api_key = "env:GEMINI_API_KEY"` to ~/.config/gdr/config.toml, or\n'
    "  • Pass --api-key to the command.\n"
    "Get a key at https://aistudio.google.com/apikey"
)


def sdk_version() -> str:
    """Return the installed google-genai SDK version, or 'unknown'."""
    try:
        return importlib.metadata.version("google-genai")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _installed_genai_major() -> int | None:
    """Major version component of the installed google-genai, or None."""
    raw = sdk_version()
    if raw == "unknown":
        return None
    try:
        return int(raw.split(".", 1)[0])
    except ValueError:
        return None


def _require_supported_sdk() -> None:
    """Fail fast when the installed SDK predates the current Interactions schema.

    A 1.x SDK emits the legacy wire schema, which the Gemini backend now
    rejects with a 400 ("upgrade to >= 2.0.0"). Surfacing that mid-run is
    confusing; catch it up front with an actionable upgrade hint instead.
    """
    major = _installed_genai_major()
    if major is not None and major < 2:
        raise ConfigError(
            f"Installed google-genai {sdk_version()} speaks the legacy Interactions "
            f"API schema, which the Gemini backend no longer accepts. Upgrade to "
            f">= {MIN_GENAI_VERSION}: `uv pip install -U 'google-genai>={MIN_GENAI_VERSION}'`."
        )


def api_key_fingerprint(key: str) -> str:
    """Return a safe-to-print fingerprint of an API key.

    Shows the first 4 and last 4 characters of the key so users can confirm
    which key is active without exposing the full secret in logs or
    `gdr doctor` output.
    """
    if len(key) < 12:
        return "invalid"
    return f"{key[:4]}…{key[-4:]}"


class GdrClient:
    """Thin wrapper over `google.genai.Client`.

    Holds the underlying SDK client as a private attribute so we can swap
    implementations or add retry/observability layers without changing
    callers. The `interactions` property proxies straight through.
    """

    def __init__(self, api_key: str | None) -> None:
        if not api_key:
            raise ConfigError(_MISSING_KEY_HINT)

        # Import lazily so the CLI can boot without network prerequisites —
        # `gdr --help`, `gdr --version`, and config-only commands should not
        # pay the cost of pulling in google.genai and its transitive deps.
        from google import genai  # noqa: PLC0415

        _require_supported_sdk()

        try:
            client = genai.Client(api_key=api_key)
        except Exception as exc:  # pragma: no cover - SDK error surface is broad
            raise ConfigError(f"Failed to initialize google-genai client: {exc}") from exc

        if not hasattr(client, "interactions"):
            raise ConfigError(
                f"Installed google-genai {sdk_version()} does not expose the "
                f"Interactions API. Upgrade to >= {MIN_GENAI_VERSION}: "
                f"`uv pip install -U 'google-genai>={MIN_GENAI_VERSION}'`."
            )

        self._genai: GenaiClient = client
        self._api_key = api_key

    # -- public surface -------------------------------------------------

    @property
    def interactions(self) -> Any:
        """The underlying `client.interactions` resource."""
        return self._genai.interactions

    @property
    def raw(self) -> GenaiClient:
        """Escape hatch to the underlying google-genai Client."""
        return self._genai

    def fingerprint(self) -> str:
        """Printable fingerprint of the API key in use."""
        return api_key_fingerprint(self._api_key)

    # -- representation -------------------------------------------------

    def __repr__(self) -> str:
        # Never include the key, even redacted, to avoid accidental logging.
        return f"GdrClient(sdk_version={sdk_version()!r})"
