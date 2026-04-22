"""Tests for `gdr.core.models`."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from gdr.constants import AGENT_FAST, DEFAULT_TOOLS, TOOL_MCP_SERVER
from gdr.core.models import (
    AgentConfig,
    FileSearchSpec,
    McpSpec,
    MediaPart,
    Record,
    RunContext,
    TextPart,
    default_run_context_for_query,
)

# ---------------------------------------------------------------------------
# TextPart / MediaPart
# ---------------------------------------------------------------------------


def test_textpart_requires_nonempty_text() -> None:
    with pytest.raises(ValidationError):
        TextPart(text="")
    part = TextPart(text="hello")
    assert part.type == "text"


def test_textpart_is_frozen() -> None:
    part = TextPart(text="hi")
    with pytest.raises(ValidationError):
        part.text = "mutated"  # type: ignore[misc]


def test_mediapart_requires_exactly_one_source() -> None:
    # neither set
    with pytest.raises(ValidationError):
        MediaPart(type="image", mime_type="image/png")
    # both set
    with pytest.raises(ValidationError):
        MediaPart(type="image", mime_type="image/png", uri="http://x", data="Zm9v")
    # uri only — ok
    MediaPart(type="image", mime_type="image/png", uri="http://x")
    # data only — ok
    MediaPart(type="image", mime_type="image/png", data="Zm9v")


def test_mediapart_requires_mime_type() -> None:
    with pytest.raises(ValidationError):
        MediaPart(type="document", uri="http://x", mime_type="")


# ---------------------------------------------------------------------------
# McpSpec
# ---------------------------------------------------------------------------


def test_mcpspec_accepts_http_and_https() -> None:
    McpSpec(name="svc", url="https://mcp.example.com")
    McpSpec(name="svc", url="http://localhost:9000")


def test_mcpspec_rejects_non_http_scheme() -> None:
    with pytest.raises(ValidationError):
        McpSpec(name="svc", url="file:///etc/passwd")
    with pytest.raises(ValidationError):
        McpSpec(name="svc", url="javascript:alert(1)")


def test_mcpspec_name_length_enforced() -> None:
    with pytest.raises(ValidationError):
        McpSpec(name="", url="https://x")
    with pytest.raises(ValidationError):
        McpSpec(name="x" * 65, url="https://x")


# ---------------------------------------------------------------------------
# FileSearchSpec
# ---------------------------------------------------------------------------


def test_file_search_store_name_prefix_required() -> None:
    with pytest.raises(ValidationError):
        FileSearchSpec(file_search_store_names=("not-a-store-name",))
    FileSearchSpec(file_search_store_names=("fileSearchStores/my-store",))


def test_file_search_requires_at_least_one_store() -> None:
    with pytest.raises(ValidationError):
        FileSearchSpec(file_search_store_names=())


# ---------------------------------------------------------------------------
# AgentConfig
# ---------------------------------------------------------------------------


def test_agent_config_defaults() -> None:
    cfg = AgentConfig()
    assert cfg.type == "deep-research"
    assert cfg.thinking_summaries == "auto"
    assert cfg.visualization == "auto"
    assert cfg.collaborative_planning is False


def test_agent_config_rejects_unknown_values() -> None:
    with pytest.raises(ValidationError):
        AgentConfig(thinking_summaries="maximum")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        AgentConfig(visualization="please")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# RunContext
# ---------------------------------------------------------------------------


def test_runcontext_default_construction(tmp_path: Path) -> None:
    ctx = default_run_context_for_query("Hello world", tmp_path)
    assert ctx.query == "Hello world"
    assert ctx.agent == AGENT_FAST
    assert ctx.builtin_tools == DEFAULT_TOOLS
    assert ctx.background is True
    assert ctx.stream is True


def test_runcontext_is_frozen(tmp_path: Path) -> None:
    ctx = default_run_context_for_query("hi", tmp_path)
    with pytest.raises(ValidationError):
        ctx.query = "mutated"  # type: ignore[misc]


def test_runcontext_rejects_configured_tool_in_builtin_list(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        RunContext(
            query="x",
            agent=AGENT_FAST,
            builtin_tools=(TOOL_MCP_SERVER,),
            output_dir=tmp_path,
        )


def test_runcontext_rejects_empty_query(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        RunContext(query="", agent=AGENT_FAST, output_dir=tmp_path)


# ---------------------------------------------------------------------------
# Record
# ---------------------------------------------------------------------------


def test_record_round_trip(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    rec = Record(
        id="abc-123",
        created_at=now,
        status="completed",
        agent=AGENT_FAST,
        query="Research TPUs",
        output_dir=tmp_path,
        tools=("google_search",),
    )
    dumped = rec.model_dump(mode="json")
    loaded = Record.model_validate(dumped)
    assert loaded == rec
