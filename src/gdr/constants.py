"""Constants shared across gdr modules."""

from __future__ import annotations

from typing import Final

APP_NAME: Final[str] = "gdr"
APP_DESCRIPTION: Final[str] = (
    "Gemini Deep Research — a terminal client for Google's Deep Research agents."
)

# Agent identifiers introduced on 2026-04-21.
# See https://ai.google.dev/gemini-api/docs/deep-research#supported-versions
AGENT_FAST: Final[str] = "deep-research-preview-04-2026"
AGENT_MAX: Final[str] = "deep-research-max-preview-04-2026"
KNOWN_AGENTS: Final[tuple[str, ...]] = (AGENT_FAST, AGENT_MAX)

# Minimum google-genai SDK version that supports `client.interactions.*`.
MIN_GENAI_VERSION: Final[str] = "1.55.0"

# Deep Research documents a 60 minute upper bound per task.
MAX_RESEARCH_SECONDS: Final[int] = 60 * 60

# Polling cadence for background interactions.
POLL_INITIAL_SECONDS: Final[float] = 5.0
POLL_EXTENDED_SECONDS: Final[float] = 15.0
POLL_INITIAL_WINDOW_SECONDS: Final[int] = 120

# Tool type strings accepted by the Interactions API for Deep Research.
TOOL_GOOGLE_SEARCH: Final[str] = "google_search"
TOOL_URL_CONTEXT: Final[str] = "url_context"
TOOL_CODE_EXECUTION: Final[str] = "code_execution"
TOOL_FILE_SEARCH: Final[str] = "file_search"
TOOL_MCP_SERVER: Final[str] = "mcp_server"
BUILTIN_TOOLS: Final[tuple[str, ...]] = (
    TOOL_GOOGLE_SEARCH,
    TOOL_URL_CONTEXT,
    TOOL_CODE_EXECUTION,
    TOOL_FILE_SEARCH,
)

# Terminal interaction statuses.
STATUS_COMPLETED: Final[str] = "completed"
STATUS_FAILED: Final[str] = "failed"
STATUS_CANCELLED: Final[str] = "cancelled"
STATUS_IN_PROGRESS: Final[str] = "in_progress"
TERMINAL_STATUSES: Final[frozenset[str]] = frozenset(
    {STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED}
)
