"""Pydantic domain models for gdr.

Design notes
------------

* Every model is frozen — gdr treats a single research run as an immutable
  value passed between layers. This keeps the runtime flow easy to reason
  about and prevents accidental mutation across command handlers.
* Models represent gdr's *own* domain, not the Interactions API wire shape.
  Translation to the SDK dict form happens in `gdr.core.requests` (Phase 3).
* Validation is intentionally strict (`extra="forbid"`) so typos in config
  fail loudly at load time rather than silently at runtime.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from gdr.constants import (
    AGENT_FAST,
    DEFAULT_TOOLS,
    SIMPLE_TOOLS,
)

# ---------------------------------------------------------------------------
# Input parts (multimodal)
# ---------------------------------------------------------------------------

MediaKind = Literal["image", "document", "audio", "video"]


class TextPart(BaseModel):
    """A text input part."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["text"] = "text"
    text: str = Field(min_length=1)


class MediaPart(BaseModel):
    """A non-text input part (image, document, audio, video).

    Exactly one of `uri` or `data` (base64) must be present. `mime_type` is
    required for all media parts — Gemini's API rejects parts without it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: MediaKind
    uri: str | None = None
    data: str | None = None
    mime_type: str = Field(min_length=1)

    @model_validator(mode="after")
    def _exactly_one_source(self) -> MediaPart:
        if (self.uri is None) == (self.data is None):
            raise ValueError("MediaPart requires exactly one of `uri` or `data` (base64).")
        return self


InputPart = Annotated[TextPart | MediaPart, Field(discriminator="type")]


# ---------------------------------------------------------------------------
# Tool specifications
# ---------------------------------------------------------------------------


class McpSpec(BaseModel):
    """A remote MCP server tool specification.

    Headers are validated for injection and redacted when serialized to
    `transcript.json` — see `gdr.core.security.SecurityPolicy`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    url: str = Field(min_length=1)
    headers: dict[str, str] = Field(default_factory=dict)
    allowed_tools: tuple[str, ...] | None = None

    @field_validator("url")
    @classmethod
    def _url_scheme(cls, v: str) -> str:
        if not (v.startswith("https://") or v.startswith("http://")):
            raise ValueError("MCP server URL must use http:// or https:// scheme.")
        return v


class FileSearchSpec(BaseModel):
    """A File Search tool specification."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    file_search_store_names: tuple[str, ...] = Field(min_length=1)

    @field_validator("file_search_store_names")
    @classmethod
    def _store_name_format(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for name in v:
            if not name.startswith("fileSearchStores/"):
                raise ValueError(
                    f"File Search store name must start with 'fileSearchStores/', got {name!r}"
                )
        return v


# ---------------------------------------------------------------------------
# Agent configuration
# ---------------------------------------------------------------------------

ThinkingSummaries = Literal["auto", "none"]
Visualization = Literal["auto", "off"]


class AgentConfig(BaseModel):
    """Deep Research `agent_config` object."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["deep-research"] = "deep-research"
    thinking_summaries: ThinkingSummaries = "auto"
    visualization: Visualization = "auto"
    collaborative_planning: bool = False


# ---------------------------------------------------------------------------
# RunContext — the result of merging config + env + CLI flags
# ---------------------------------------------------------------------------


class RunContext(BaseModel):
    """Immutable settings for a single `gdr research` invocation.

    Constructed once in the command handler by merging CLI flags, env vars,
    and loaded config. Everything downstream — request assembly, streaming,
    rendering — reads from this object only.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    query: str = Field(min_length=1)
    agent: str
    builtin_tools: tuple[str, ...] = Field(default_factory=tuple)
    mcp_servers: tuple[McpSpec, ...] = Field(default_factory=tuple)
    file_search: FileSearchSpec | None = None
    input_parts: tuple[InputPart, ...] = Field(default_factory=tuple)
    agent_config: AgentConfig = Field(default_factory=AgentConfig)
    previous_interaction_id: str | None = None
    stream: bool = True
    background: bool = True
    output_dir: Path
    auto_open: bool = True
    confirm_max: bool = True
    untrusted_input: bool = False

    @field_validator("builtin_tools")
    @classmethod
    def _simple_tools_only(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for name in v:
            if name not in SIMPLE_TOOLS:
                raise ValueError(
                    f"{name!r} is not a simple builtin tool. "
                    f"Use the typed fields (`file_search`, `mcp_servers`) for "
                    f"configured tools. Simple tools: {list(SIMPLE_TOOLS)}"
                )
        return v


# ---------------------------------------------------------------------------
# Local history record
# ---------------------------------------------------------------------------


class Record(BaseModel):
    """A single row in the local interaction store.

    Serialized as one JSON line in `interactions.jsonl`. Fields are kept
    minimal and stable so old records remain readable as the schema evolves.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    parent_id: str | None = None
    created_at: datetime
    finished_at: datetime | None = None
    status: str
    agent: str
    query: str
    output_dir: Path
    total_tokens: int | None = None
    tools: tuple[str, ...] = Field(default_factory=tuple)
    note: str | None = None


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------


def default_agent_config() -> AgentConfig:
    return AgentConfig()


def default_run_context_for_query(query: str, output_dir: Path) -> RunContext:
    """Convenience constructor for tests + smoke flows."""
    return RunContext(
        query=query,
        agent=AGENT_FAST,
        builtin_tools=DEFAULT_TOOLS,
        output_dir=output_dir,
    )
