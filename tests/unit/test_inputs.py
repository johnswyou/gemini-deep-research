"""Tests for `gdr.core.inputs` — CLI flag parsers."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from gdr.constants import SIMPLE_TOOLS, TOOL_URL_CONTEXT
from gdr.core.inputs import (
    ensure_url_context_tool,
    parse_file,
    parse_file_search_stores,
    parse_files,
    parse_mcp_header_token,
    parse_mcp_spec_token,
    parse_mcps,
    urls_as_text_part,
    validate_tool_names,
    validate_visualization,
)
from gdr.errors import ConfigError

# A valid 1x1 transparent PNG, base64 — small enough to inline but still a
# real image file that mimetypes.guess_type recognizes.
TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)
TINY_PNG_BYTES = base64.b64decode(TINY_PNG_B64)


# ---------------------------------------------------------------------------
# validate_tool_names
# ---------------------------------------------------------------------------


class TestValidateToolNames:
    def test_simple_tools_pass_through(self) -> None:
        assert validate_tool_names(list(SIMPLE_TOOLS)) == tuple(SIMPLE_TOOLS)

    def test_rejects_configured_tools_with_hint(self) -> None:
        with pytest.raises(ConfigError, match="--file-search-store"):
            validate_tool_names(["file_search"])
        with pytest.raises(ConfigError, match="--mcp"):
            validate_tool_names(["mcp_server"])

    def test_rejects_unknown_tool(self) -> None:
        with pytest.raises(ConfigError, match="Unknown --tool"):
            validate_tool_names(["mind_reader"])

    def test_strips_whitespace(self) -> None:
        assert validate_tool_names(["  google_search "]) == ("google_search",)


# ---------------------------------------------------------------------------
# parse_file / parse_files
# ---------------------------------------------------------------------------


class TestParseFile:
    def test_reads_and_base64_encodes_png(self, tmp_path: Path) -> None:
        png_path = tmp_path / "tiny.png"
        png_path.write_bytes(TINY_PNG_BYTES)
        part = parse_file(png_path)
        assert part.type == "image"
        assert part.mime_type == "image/png"
        assert part.data == TINY_PNG_B64

    def test_pdf_is_document_kind(self, tmp_path: Path) -> None:
        pdf_path = tmp_path / "doc.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 fake pdf bytes")
        part = parse_file(pdf_path)
        assert part.type == "document"
        assert part.mime_type == "application/pdf"

    def test_audio_wav_is_audio_kind(self, tmp_path: Path) -> None:
        wav_path = tmp_path / "clip.wav"
        wav_path.write_bytes(b"RIFF....WAVEfmt ")
        part = parse_file(wav_path)
        assert part.type == "audio"

    def test_unknown_extension_falls_back_to_document_octet_stream(self, tmp_path: Path) -> None:
        weird = tmp_path / "mystery.xyzq"
        weird.write_bytes(b"\x00\x01\x02")
        part = parse_file(weird)
        assert part.type == "document"
        assert part.mime_type == "application/octet-stream"

    def test_missing_path_raises_config_error(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="does not exist"):
            parse_file(tmp_path / "nope.pdf")

    def test_directory_raises_config_error(self, tmp_path: Path) -> None:
        (tmp_path / "sub").mkdir()
        with pytest.raises(ConfigError, match="not a regular file"):
            parse_file(tmp_path / "sub")

    def test_parse_files_batches_in_order(self, tmp_path: Path) -> None:
        a = tmp_path / "a.pdf"
        a.write_bytes(b"first")
        b = tmp_path / "b.pdf"
        b.write_bytes(b"second")
        parts = parse_files([a, b])
        assert len(parts) == 2
        assert parts[0].data == base64.b64encode(b"first").decode("ascii")
        assert parts[1].data == base64.b64encode(b"second").decode("ascii")


# ---------------------------------------------------------------------------
# urls_as_text_part / ensure_url_context_tool
# ---------------------------------------------------------------------------


class TestUrlsAsTextPart:
    def test_builds_text_part_listing_urls(self) -> None:
        part = urls_as_text_part(["https://a.example", "https://b.example"])
        assert part is not None
        assert "https://a.example" in part.text
        assert "https://b.example" in part.text
        assert part.text.startswith("Additional URLs")

    def test_empty_returns_none(self) -> None:
        assert urls_as_text_part([]) is None
        assert urls_as_text_part(["", "   "]) is None

    def test_trims_whitespace(self) -> None:
        part = urls_as_text_part(["  https://a.example  "])
        assert part is not None
        assert "https://a.example" in part.text
        assert "  https" not in part.text


class TestEnsureUrlContextTool:
    def test_adds_url_context_when_missing_and_has_urls(self) -> None:
        result = ensure_url_context_tool(("google_search",), has_urls=True)
        assert TOOL_URL_CONTEXT in result
        # Existing tool preserved.
        assert "google_search" in result

    def test_noop_when_already_present(self) -> None:
        tools = ("google_search", TOOL_URL_CONTEXT)
        assert ensure_url_context_tool(tools, has_urls=True) == tools

    def test_noop_when_no_urls(self) -> None:
        tools = ("google_search",)
        assert ensure_url_context_tool(tools, has_urls=False) == tools


# ---------------------------------------------------------------------------
# parse_mcp_spec_token / parse_mcp_header_token / parse_mcps
# ---------------------------------------------------------------------------


class TestParseMcpSpecToken:
    def test_name_and_url(self) -> None:
        spec = parse_mcp_spec_token("deploys=https://mcp.example.com", headers_by_name={})
        assert spec.name == "deploys"
        assert spec.url == "https://mcp.example.com"
        assert spec.headers == {}

    def test_attaches_headers_from_map(self) -> None:
        spec = parse_mcp_spec_token(
            "deploys=https://mcp.example.com",
            headers_by_name={"deploys": {"Authorization": "Bearer abc"}},
        )
        assert spec.headers == {"Authorization": "Bearer abc"}

    def test_rejects_missing_equals(self) -> None:
        with pytest.raises(ConfigError, match="NAME=URL"):
            parse_mcp_spec_token("not-a-kv-pair", headers_by_name={})

    def test_rejects_empty_name_or_url(self) -> None:
        with pytest.raises(ConfigError):
            parse_mcp_spec_token("=https://x.example", headers_by_name={})
        with pytest.raises(ConfigError):
            parse_mcp_spec_token("deploys=", headers_by_name={})

    def test_rejects_bad_url_scheme(self) -> None:
        with pytest.raises(ConfigError, match="scheme"):
            parse_mcp_spec_token("deploys=ftp://x.example", headers_by_name={})


class TestParseMcpHeaderToken:
    def test_name_key_value(self) -> None:
        assert parse_mcp_header_token("deploys=Authorization:Bearer abc") == (
            "deploys",
            "Authorization",
            "Bearer abc",
        )

    def test_value_containing_colons_preserved(self) -> None:
        # Value "Bearer abc:123" must survive intact (split only on FIRST colon).
        _, key, value = parse_mcp_header_token("svc=X-Token:Bearer abc:123")
        assert key == "X-Token"
        assert value == "Bearer abc:123"

    def test_rejects_missing_equals(self) -> None:
        with pytest.raises(ConfigError, match="NAME=Key:Value"):
            parse_mcp_header_token("just a thing")

    def test_rejects_missing_colon_in_value(self) -> None:
        with pytest.raises(ConfigError, match="NAME=Key:Value"):
            parse_mcp_header_token("svc=no-colon-here")

    def test_rejects_empty_name(self) -> None:
        with pytest.raises(ConfigError):
            parse_mcp_header_token("=X:Y")

    def test_rejects_empty_key(self) -> None:
        with pytest.raises(ConfigError):
            parse_mcp_header_token("svc=:value")


class TestParseMcps:
    def test_builds_specs_and_attaches_headers(self) -> None:
        specs = parse_mcps(
            ["deploys=https://mcp.example.com"],
            ["deploys=Authorization:Bearer abc"],
        )
        assert len(specs) == 1
        assert specs[0].name == "deploys"
        assert specs[0].headers == {"Authorization": "Bearer abc"}

    def test_multiple_servers_each_get_headers(self) -> None:
        specs = parse_mcps(
            ["a=https://a.example", "b=https://b.example"],
            ["a=Authorization:Bearer A", "b=X-Token:xyz"],
        )
        by_name = {s.name: s for s in specs}
        assert by_name["a"].headers == {"Authorization": "Bearer A"}
        assert by_name["b"].headers == {"X-Token": "xyz"}

    def test_rejects_duplicate_mcp_names(self) -> None:
        with pytest.raises(ConfigError, match="specified more than once"):
            parse_mcps(
                ["same=https://a.example", "same=https://b.example"],
                [],
            )

    def test_rejects_header_for_unknown_server(self) -> None:
        with pytest.raises(ConfigError, match="unknown MCP server"):
            parse_mcps(
                ["known=https://k.example"],
                ["typo=Authorization:Bearer x"],
            )

    def test_empty_inputs_return_empty_tuple(self) -> None:
        assert parse_mcps([], []) == ()


# ---------------------------------------------------------------------------
# parse_file_search_stores
# ---------------------------------------------------------------------------


class TestParseFileSearchStores:
    def test_prepends_prefix_for_bare_names(self) -> None:
        spec = parse_file_search_stores(["kb-2025"])
        assert spec is not None
        assert spec.file_search_store_names == ("fileSearchStores/kb-2025",)

    def test_passes_prefixed_names_through(self) -> None:
        spec = parse_file_search_stores(["fileSearchStores/kb-2025"])
        assert spec is not None
        assert spec.file_search_store_names == ("fileSearchStores/kb-2025",)

    def test_mixed_input_normalized(self) -> None:
        spec = parse_file_search_stores(["a", "fileSearchStores/b"])
        assert spec is not None
        assert spec.file_search_store_names == (
            "fileSearchStores/a",
            "fileSearchStores/b",
        )

    def test_empty_returns_none(self) -> None:
        assert parse_file_search_stores([]) is None
        assert parse_file_search_stores(["  "]) is None


# ---------------------------------------------------------------------------
# validate_visualization
# ---------------------------------------------------------------------------


class TestValidateVisualization:
    def test_auto_and_off_accepted(self) -> None:
        assert validate_visualization("auto") == "auto"
        assert validate_visualization("off") == "off"

    def test_none_returns_none(self) -> None:
        assert validate_visualization(None) is None

    def test_case_insensitive(self) -> None:
        assert validate_visualization("AUTO") == "auto"
        assert validate_visualization("Off") == "off"

    def test_invalid_raises(self) -> None:
        with pytest.raises(ConfigError, match="auto"):
            validate_visualization("maybe")
