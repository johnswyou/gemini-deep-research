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

# Minimum google-genai SDK version that speaks the current (post-May-2026)
# Interactions API schema. The legacy schema emitted by 1.x SDKs is rejected
# server-side with a 400 ("upgrade to >= 2.0.0"), so 2.0.0 is a hard floor.
MIN_GENAI_VERSION: Final[str] = "2.0.0"

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

# "Simple" tools take no configuration beyond their type string.
SIMPLE_TOOLS: Final[tuple[str, ...]] = (
    TOOL_GOOGLE_SEARCH,
    TOOL_URL_CONTEXT,
    TOOL_CODE_EXECUTION,
)
# "Configured" tools require extra fields (store names, URLs, headers).
CONFIGURED_TOOLS: Final[tuple[str, ...]] = (TOOL_FILE_SEARCH, TOOL_MCP_SERVER)
ALL_TOOLS: Final[tuple[str, ...]] = SIMPLE_TOOLS + CONFIGURED_TOOLS

# Default tool set when the user doesn't pass `--tool` flags.
DEFAULT_TOOLS: Final[tuple[str, ...]] = SIMPLE_TOOLS

# Interaction statuses (google-genai 2.x models the full set:
# in_progress / requires_action / completed / failed / cancelled / incomplete /
# budget_exceeded). `budget_exceeded` was added in the 2.x schema — the agent
# stopped because it hit a budget cap, which is terminal (a partial report may
# still be present).
STATUS_COMPLETED: Final[str] = "completed"
STATUS_FAILED: Final[str] = "failed"
STATUS_CANCELLED: Final[str] = "cancelled"
STATUS_INCOMPLETE: Final[str] = "incomplete"
STATUS_BUDGET_EXCEEDED: Final[str] = "budget_exceeded"
STATUS_IN_PROGRESS: Final[str] = "in_progress"
TERMINAL_STATUSES: Final[frozenset[str]] = frozenset(
    {
        STATUS_COMPLETED,
        STATUS_FAILED,
        STATUS_CANCELLED,
        STATUS_INCOMPLETE,
        STATUS_BUDGET_EXCEEDED,
    }
)
