"""Tests for `gdr.core.security`."""

from __future__ import annotations

from pathlib import Path

import pytest

from gdr.core.security import (
    REDACTED,
    SecurityPolicy,
    ensure_under_root,
    filter_tools_for_untrusted,
    redact_sensitive,
    sanitize_slug,
    validate_mcp_header,
    validate_mcp_headers,
)
from gdr.errors import ConfigError

# ---------------------------------------------------------------------------
# validate_mcp_header
# ---------------------------------------------------------------------------


class TestValidateMcpHeader:
    def test_accepts_common_headers(self) -> None:
        validate_mcp_header("Authorization", "Bearer xyz")
        validate_mcp_header("X-Custom-Token", "abc123")
        validate_mcp_header("Accept", "application/json")

    def test_rejects_names_with_colons(self) -> None:
        with pytest.raises(ConfigError):
            validate_mcp_header("X:Foo", "v")

    def test_rejects_names_with_whitespace(self) -> None:
        with pytest.raises(ConfigError):
            validate_mcp_header("X Foo", "v")
        with pytest.raises(ConfigError):
            validate_mcp_header(" Authorization", "v")

    def test_rejects_empty_name(self) -> None:
        with pytest.raises(ConfigError):
            validate_mcp_header("", "v")

    def test_rejects_overlong_name(self) -> None:
        with pytest.raises(ConfigError):
            validate_mcp_header("X-" + "a" * 80, "v")

    def test_rejects_reserved_headers_case_insensitively(self) -> None:
        for name in ("Host", "host", "Content-Length", "TE", "Connection"):
            with pytest.raises(ConfigError):
                validate_mcp_header(name, "v")

    def test_rejects_crlf_in_value(self) -> None:
        with pytest.raises(ConfigError):
            validate_mcp_header("X-Inject", "legit\r\nX-Evil: yes")
        with pytest.raises(ConfigError):
            validate_mcp_header("X-Inject", "legit\nhello")
        with pytest.raises(ConfigError):
            validate_mcp_header("X-Inject", "legit\rhello")

    def test_rejects_null_in_value(self) -> None:
        with pytest.raises(ConfigError):
            validate_mcp_header("X-Foo", "abc\x00def")

    def test_validate_headers_fails_fast(self) -> None:
        with pytest.raises(ConfigError):
            validate_mcp_headers({"Authorization": "ok", "X Bad": "nope"})


# ---------------------------------------------------------------------------
# sanitize_slug
# ---------------------------------------------------------------------------


class TestSanitizeSlug:
    def test_lowercases_and_replaces_spaces(self) -> None:
        assert sanitize_slug("Hello World") == "hello-world"

    def test_collapses_runs_of_non_alphanum(self) -> None:
        assert sanitize_slug("foo!!!bar???baz") == "foo-bar-baz"

    def test_strips_leading_trailing_dashes(self) -> None:
        assert sanitize_slug("!!!wow!!!") == "wow"

    def test_enforces_max_length(self) -> None:
        assert len(sanitize_slug("a" * 200)) <= 64

    def test_handles_unicode(self) -> None:
        # Non-ASCII chars are treated as noise and collapsed.
        assert sanitize_slug("café über straße") == "caf-ber-stra-e"

    def test_empty_input_returns_fallback(self) -> None:
        assert sanitize_slug("") == "query"
        assert sanitize_slug("!!!") == "query"


# ---------------------------------------------------------------------------
# ensure_under_root
# ---------------------------------------------------------------------------


class TestEnsureUnderRoot:
    def test_allows_direct_child(self, tmp_path: Path) -> None:
        candidate = tmp_path / "ok"
        ensure_under_root(candidate, tmp_path)

    def test_allows_nested_subpath(self, tmp_path: Path) -> None:
        candidate = tmp_path / "a" / "b" / "c"
        ensure_under_root(candidate, tmp_path)

    def test_rejects_sibling(self, tmp_path: Path) -> None:
        sibling = tmp_path.parent / "elsewhere"
        with pytest.raises(ConfigError):
            ensure_under_root(sibling, tmp_path)

    def test_rejects_parent_traversal(self, tmp_path: Path) -> None:
        traversal = tmp_path / ".." / ".." / "etc" / "passwd"
        with pytest.raises(ConfigError):
            ensure_under_root(traversal, tmp_path)


# ---------------------------------------------------------------------------
# redact_sensitive
# ---------------------------------------------------------------------------


class TestRedactSensitive:
    def test_redacts_api_key_at_any_depth(self) -> None:
        data = {"api_key": "secret-1234", "nested": {"api_key": "inner-secret"}}
        out = redact_sensitive(data)
        assert out == {"api_key": REDACTED, "nested": {"api_key": REDACTED}}

    def test_redacts_auth_headers(self) -> None:
        data = {"headers": {"Authorization": "Bearer s3cret", "Accept": "application/json"}}
        out = redact_sensitive(data)
        assert out == {"headers": {"Authorization": REDACTED, "Accept": "application/json"}}

    def test_redacts_token_and_secret_headers(self) -> None:
        data = {"headers": {"X-API-Token": "x", "X-Secret-Value": "y", "Ok": "z"}}
        out = redact_sensitive(data)
        assert out["headers"]["X-API-Token"] == REDACTED
        assert out["headers"]["X-Secret-Value"] == REDACTED
        assert out["headers"]["Ok"] == "z"

    def test_walks_into_lists(self) -> None:
        data = {"tools": [{"type": "mcp_server", "headers": {"Authorization": "Bearer x"}}]}
        out = redact_sensitive(data)
        assert out["tools"][0]["headers"]["Authorization"] == REDACTED

    def test_returns_new_structure_not_mutating_input(self) -> None:
        data = {"api_key": "s"}
        out = redact_sensitive(data)
        assert data["api_key"] == "s"
        assert out["api_key"] == REDACTED

    def test_passes_through_scalars(self) -> None:
        assert redact_sensitive("plain string") == "plain string"
        assert redact_sensitive(42) == 42
        assert redact_sensitive(None) is None


# ---------------------------------------------------------------------------
# filter_tools_for_untrusted
# ---------------------------------------------------------------------------


class TestFilterToolsForUntrusted:
    def test_strips_code_execution_and_mcp_server(self) -> None:
        tools = [
            {"type": "google_search"},
            {"type": "code_execution"},
            {"type": "mcp_server", "url": "https://x"},
            {"type": "url_context"},
        ]
        kept, stripped = filter_tools_for_untrusted(tools)
        assert {t["type"] for t in kept} == {"google_search", "url_context"}
        assert set(stripped) == {"code_execution", "mcp_server"}

    def test_noop_when_nothing_to_strip(self) -> None:
        tools = [{"type": "google_search"}, {"type": "url_context"}]
        kept, stripped = filter_tools_for_untrusted(tools)
        assert kept == tools
        assert stripped == []


# ---------------------------------------------------------------------------
# SecurityPolicy object
# ---------------------------------------------------------------------------


class TestSecurityPolicy:
    def test_confine_resolves_under_root(self, tmp_path: Path) -> None:
        policy = SecurityPolicy(output_root=tmp_path)
        resolved = policy.confine(tmp_path / "sub")
        assert resolved.is_relative_to(tmp_path.resolve())

    def test_confine_rejects_escape(self, tmp_path: Path) -> None:
        policy = SecurityPolicy(output_root=tmp_path)
        with pytest.raises(ConfigError):
            policy.confine(tmp_path.parent / "elsewhere")

    def test_output_subdir_sanitizes_and_appends_id(self, tmp_path: Path) -> None:
        policy = SecurityPolicy(output_root=tmp_path)
        result = policy.output_subdir("Hello World!", "abc123xyz")
        assert result.name.startswith("hello-world")
        assert result.name.endswith("_abc123")
        assert result.is_relative_to(tmp_path.resolve())

    def test_filter_tools_respects_untrusted_flag(self) -> None:
        tools = [{"type": "google_search"}, {"type": "code_execution"}]

        trusted = SecurityPolicy(output_root=Path("/tmp"), untrusted=False)
        kept_trusted, stripped_trusted = trusted.filter_tools(tools)
        assert len(kept_trusted) == 2
        assert stripped_trusted == []

        untrusted = SecurityPolicy(output_root=Path("/tmp"), untrusted=True)
        kept_untrusted, stripped_untrusted = untrusted.filter_tools(tools)
        assert len(kept_untrusted) == 1
        assert stripped_untrusted == ["code_execution"]

    def test_redact_is_accessible_on_policy(self, tmp_path: Path) -> None:
        policy = SecurityPolicy(output_root=tmp_path)
        assert policy.redact({"api_key": "x"}) == {"api_key": REDACTED}
