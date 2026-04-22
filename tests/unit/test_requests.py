"""Tests for `gdr.core.requests` — RunContext → create() kwargs translation."""

from __future__ import annotations

from pathlib import Path

import pytest

from gdr.constants import AGENT_FAST, AGENT_MAX, DEFAULT_TOOLS
from gdr.core.models import (
    AgentConfig,
    FileSearchSpec,
    McpSpec,
    MediaPart,
    RunContext,
    TextPart,
)
from gdr.core.requests import build_create_kwargs, build_tools
from gdr.core.security import SecurityPolicy
from gdr.errors import ConfigError


def _ctx(**overrides: object) -> RunContext:
    defaults: dict[str, object] = {
        "query": "Research TPUs",
        "agent": AGENT_FAST,
        "builtin_tools": DEFAULT_TOOLS,
        "output_dir": Path("/tmp/gdr"),
        "stream": False,
        "background": True,
    }
    defaults.update(overrides)
    return RunContext(**defaults)  # type: ignore[arg-type]


def _policy(tmp: Path, *, untrusted: bool = False) -> SecurityPolicy:
    return SecurityPolicy(output_root=tmp, untrusted=untrusted)


class TestBuildTools:
    def test_simple_tools_only(self, tmp_path: Path) -> None:
        ctx = _ctx()
        tools, stripped = build_tools(ctx, _policy(tmp_path))
        assert [t["type"] for t in tools] == list(DEFAULT_TOOLS)
        assert stripped == []

    def test_file_search_after_simple_tools(self, tmp_path: Path) -> None:
        ctx = _ctx(file_search=FileSearchSpec(file_search_store_names=("fileSearchStores/a",)))
        tools, _ = build_tools(ctx, _policy(tmp_path))
        assert tools[-1]["type"] == "file_search"
        assert tools[-1]["file_search_store_names"] == ["fileSearchStores/a"]

    def test_mcp_serialized_at_end_and_validates_headers(self, tmp_path: Path) -> None:
        mcp = McpSpec(
            name="deploys",
            url="https://mcp.example.com/mcp",
            headers={"Authorization": "Bearer x"},
        )
        ctx = _ctx(mcp_servers=(mcp,))
        tools, _ = build_tools(ctx, _policy(tmp_path))
        mcp_entry = tools[-1]
        assert mcp_entry["type"] == "mcp_server"
        assert mcp_entry["name"] == "deploys"
        assert mcp_entry["headers"] == {"Authorization": "Bearer x"}

    def test_invalid_mcp_header_aborts(self, tmp_path: Path) -> None:
        # Pydantic validation on McpSpec doesn't check for CR/LF — that's the
        # security policy's job. We construct a spec with a bad header value
        # and expect build_tools to raise ConfigError via policy validation.
        mcp = McpSpec(
            name="deploys",
            url="https://mcp.example.com",
            headers={"X-Custom": "line1\r\nX-Evil: yes"},
        )
        ctx = _ctx(mcp_servers=(mcp,))
        with pytest.raises(ConfigError):
            build_tools(ctx, _policy(tmp_path))

    def test_untrusted_mode_strips_code_execution(self, tmp_path: Path) -> None:
        ctx = _ctx(builtin_tools=("google_search", "code_execution"))
        tools, stripped = build_tools(ctx, _policy(tmp_path, untrusted=True))
        assert [t["type"] for t in tools] == ["google_search"]
        assert stripped == ["code_execution"]

    def test_untrusted_mode_strips_mcp_server(self, tmp_path: Path) -> None:
        # Use a tool set without code_execution so the only untrusted-stripped
        # tool is mcp_server — makes the assertion sharp.
        mcp = McpSpec(name="svc", url="https://x.example.com")
        ctx = _ctx(builtin_tools=("google_search",), mcp_servers=(mcp,))
        tools, stripped = build_tools(ctx, _policy(tmp_path, untrusted=True))
        assert all(t["type"] != "mcp_server" for t in tools)
        assert stripped == ["mcp_server"]


class TestBuildCreateKwargs:
    def test_string_input_when_no_parts(self, tmp_path: Path) -> None:
        ctx = _ctx(query="What are TPUs?")
        kwargs, _ = build_create_kwargs(ctx, _policy(tmp_path))
        assert kwargs["input"] == "What are TPUs?"

    def test_list_input_when_parts_present(self, tmp_path: Path) -> None:
        parts = (MediaPart(type="document", uri="https://x/a.pdf", mime_type="application/pdf"),)
        ctx = _ctx(input_parts=parts, query="Summarize this.")
        kwargs, _ = build_create_kwargs(ctx, _policy(tmp_path))
        assert isinstance(kwargs["input"], list)
        assert kwargs["input"][0] == {"type": "text", "text": "Summarize this."}
        assert kwargs["input"][1]["type"] == "document"
        assert kwargs["input"][1]["uri"] == "https://x/a.pdf"
        assert kwargs["input"][1]["mime_type"] == "application/pdf"

    def test_text_part_in_input_parts_is_preserved(self, tmp_path: Path) -> None:
        # Even though our RunContext always prepends a text part from `query`,
        # any explicit TextParts in input_parts must also make it through.
        parts = (TextPart(text="Extra context."),)
        ctx = _ctx(input_parts=parts)
        kwargs, _ = build_create_kwargs(ctx, _policy(tmp_path))
        assert len(kwargs["input"]) == 2

    def test_always_emits_background_and_agent(self, tmp_path: Path) -> None:
        ctx = _ctx(agent=AGENT_MAX)
        kwargs, _ = build_create_kwargs(ctx, _policy(tmp_path))
        assert kwargs["background"] is True
        assert kwargs["agent"] == AGENT_MAX

    def test_agent_config_includes_type_deep_research(self, tmp_path: Path) -> None:
        ctx = _ctx(agent_config=AgentConfig(thinking_summaries="auto", visualization="off"))
        kwargs, _ = build_create_kwargs(ctx, _policy(tmp_path))
        assert kwargs["agent_config"]["type"] == "deep-research"
        assert kwargs["agent_config"]["visualization"] == "off"

    def test_stream_flag_only_present_when_true(self, tmp_path: Path) -> None:
        ctx_no_stream = _ctx(stream=False)
        kwargs_ns, _ = build_create_kwargs(ctx_no_stream, _policy(tmp_path))
        assert "stream" not in kwargs_ns

        ctx_stream = _ctx(stream=True)
        kwargs_s, _ = build_create_kwargs(ctx_stream, _policy(tmp_path))
        assert kwargs_s["stream"] is True

    def test_tools_omitted_when_empty(self, tmp_path: Path) -> None:
        ctx = _ctx(builtin_tools=())
        kwargs, _ = build_create_kwargs(ctx, _policy(tmp_path))
        assert "tools" not in kwargs

    def test_previous_interaction_id_passed_through(self, tmp_path: Path) -> None:
        ctx = _ctx(previous_interaction_id="prev-abc-123")
        kwargs, _ = build_create_kwargs(ctx, _policy(tmp_path))
        assert kwargs["previous_interaction_id"] == "prev-abc-123"

    def test_stripped_tools_returned_to_caller(self, tmp_path: Path) -> None:
        ctx = _ctx(builtin_tools=("google_search", "code_execution"))
        _, stripped = build_create_kwargs(ctx, _policy(tmp_path, untrusted=True))
        assert stripped == ["code_execution"]


class TestCombinedToolsAndMultimodal:
    """Phase 6 — file_search + MCP + media parts together."""

    def test_builtin_plus_file_search_plus_mcp_preserves_order(self, tmp_path: Path) -> None:
        mcp = McpSpec(name="svc", url="https://svc.example.com", headers={})
        ctx = _ctx(
            builtin_tools=("google_search", "url_context"),
            file_search=FileSearchSpec(file_search_store_names=("fileSearchStores/kb",)),
            mcp_servers=(mcp,),
        )
        tools, _ = build_tools(ctx, _policy(tmp_path))
        types = [t["type"] for t in tools]
        # Contract: builtin → file_search → mcp_server, in that order.
        assert types == ["google_search", "url_context", "file_search", "mcp_server"]

    def test_media_part_and_text_query_become_parts_list(self, tmp_path: Path) -> None:
        png = MediaPart(type="image", data="aGk=", mime_type="image/png")
        pdf = MediaPart(type="document", data="aGk=", mime_type="application/pdf")
        ctx = _ctx(input_parts=(png, pdf), query="Summarize.")
        kwargs, _ = build_create_kwargs(ctx, _policy(tmp_path))
        assert isinstance(kwargs["input"], list)
        assert kwargs["input"][0] == {"type": "text", "text": "Summarize."}
        assert kwargs["input"][1]["type"] == "image"
        assert kwargs["input"][1]["data"] == "aGk="
        assert kwargs["input"][1]["mime_type"] == "image/png"
        assert kwargs["input"][2]["type"] == "document"

    def test_untrusted_strips_mcp_but_keeps_file_search(self, tmp_path: Path) -> None:
        """file_search is safe under untrusted mode — only code_execution and
        mcp_server are stripped."""
        mcp = McpSpec(name="svc", url="https://svc.example.com")
        ctx = _ctx(
            builtin_tools=("google_search", "code_execution"),
            file_search=FileSearchSpec(file_search_store_names=("fileSearchStores/kb",)),
            mcp_servers=(mcp,),
        )
        tools, stripped = build_create_kwargs(ctx, _policy(tmp_path, untrusted=True))
        types = [t["type"] for t in tools["tools"]]
        assert "code_execution" not in types
        assert "mcp_server" not in types
        assert "file_search" in types
        assert sorted(stripped) == ["code_execution", "mcp_server"]

    def test_mcp_with_valid_authorization_header_kept(self, tmp_path: Path) -> None:
        mcp = McpSpec(
            name="deploys",
            url="https://mcp.example.com",
            headers={"Authorization": "Bearer safe-token"},
        )
        ctx = _ctx(mcp_servers=(mcp,))
        tools, _ = build_tools(ctx, _policy(tmp_path))
        # Headers survive unmodified through requests (redaction is transcript-only).
        assert tools[-1]["headers"] == {"Authorization": "Bearer safe-token"}
