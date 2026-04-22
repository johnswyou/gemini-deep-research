"""Tests for `gdr.config`."""

from __future__ import annotations

from pathlib import Path

import pytest

from gdr.config import (
    Config,
    _expand_env_string,
    _walk_and_expand,
    default_config_path,
    load_config,
)
from gdr.constants import AGENT_FAST, DEFAULT_TOOLS
from gdr.errors import ConfigError

# ---------------------------------------------------------------------------
# env expansion
# ---------------------------------------------------------------------------


def test_env_expansion_resolves_variable() -> None:
    assert _expand_env_string("env:MY_TOKEN", env={"MY_TOKEN": "abc"}) == "abc"


def test_env_expansion_passes_through_plain_strings() -> None:
    assert _expand_env_string("hello", env={}) == "hello"
    assert _expand_env_string("", env={}) == ""


def test_env_expansion_does_not_expand_midstring() -> None:
    # Only the leading `env:` prefix is special; embedded tokens pass through.
    assert _expand_env_string("hi env:X", env={"X": "y"}) == "hi env:X"


def test_env_expansion_strips_whitespace_in_var_name() -> None:
    assert _expand_env_string("env:  SPACY  ", env={"SPACY": "ok"}) == "ok"


def test_env_expansion_errors_when_variable_missing() -> None:
    with pytest.raises(ConfigError) as excinfo:
        _expand_env_string("env:NOPE", env={})
    assert "NOPE" in str(excinfo.value)


def test_env_expansion_rejects_empty_var_reference() -> None:
    with pytest.raises(ConfigError):
        _expand_env_string("env:", env={})
    with pytest.raises(ConfigError):
        _expand_env_string("env:   ", env={})


def test_walk_and_expand_recurses_nested_structures() -> None:
    raw = {
        "top": "env:A",
        "nested": {"deeper": ["env:B", {"inner": "env:C"}]},
        "untouched": 42,
    }
    expanded = _walk_and_expand(raw, env={"A": "1", "B": "2", "C": "3"})
    assert expanded == {
        "top": "1",
        "nested": {"deeper": ["2", {"inner": "3"}]},
        "untouched": 42,
    }


# ---------------------------------------------------------------------------
# default path resolution
# ---------------------------------------------------------------------------


def test_default_config_path_respects_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GDR_CONFIG_PATH", "/tmp/custom/config.toml")
    assert default_config_path() == Path("/tmp/custom/config.toml")


def test_default_config_path_uses_xdg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GDR_CONFIG_PATH", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", "/var/xdg")
    assert default_config_path() == Path("/var/xdg/gdr/config.toml")


def test_default_config_path_falls_back_to_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GDR_CONFIG_PATH", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert default_config_path() == Path.home() / ".config" / "gdr" / "config.toml"


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------


def test_load_config_returns_defaults_when_file_missing(tmp_path: Path) -> None:
    cfg = load_config(path=tmp_path / "does-not-exist.toml", env={})
    assert isinstance(cfg, Config)
    assert cfg.default_agent == AGENT_FAST
    assert cfg.default_tools == DEFAULT_TOOLS


def test_load_config_parses_minimal_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        'default_agent = "deep-research-max-preview-04-2026"\n'
        'output_dir = "~/gdr-reports"\n'
        "auto_open = false\n",
        encoding="utf-8",
    )
    cfg = load_config(path=path, env={"HOME": str(tmp_path)})
    assert cfg.default_agent == "deep-research-max-preview-04-2026"
    assert cfg.auto_open is False


def test_load_config_expands_env_references(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('api_key = "env:MY_KEY"\n', encoding="utf-8")
    cfg = load_config(path=path, env={"MY_KEY": "AIzaTestKey1234"})
    assert cfg.api_key == "AIzaTestKey1234"


def test_load_config_reports_missing_env_var(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('api_key = "env:UNSET_VAR"\n', encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path=path, env={})
    assert "UNSET_VAR" in str(excinfo.value)


def test_load_config_rejects_unknown_top_level_key(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('banana = "yes"\n', encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path=path, env={})
    assert "banana" in str(excinfo.value)


def test_load_config_rejects_bad_tool(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('default_tools = ["google_search", "fake_tool"]\n', encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path=path, env={})
    assert "fake_tool" in str(excinfo.value)


def test_load_config_rejects_bad_visualization(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('visualization = "sparkly"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path=path, env={})


def test_load_config_expands_nested_mcp_headers(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "[mcp_servers.factset]\n"
        'url = "https://mcp.factset.com"\n'
        'headers.Authorization = "Bearer env:FACTSET_TOKEN"\n',
        encoding="utf-8",
    )
    cfg = load_config(path=path, env={"FACTSET_TOKEN": "secret-token-1234"})
    server = cfg.mcp_servers["factset"]
    assert server.url == "https://mcp.factset.com"
    # env:FACTSET_TOKEN inside a longer Bearer string does NOT get expanded
    # because we only expand values starting with `env:`. Values that need a
    # secret should be `env:VAR` alone. This documents that semantic.
    assert server.headers["Authorization"] == "Bearer env:FACTSET_TOKEN"


def test_load_config_expands_full_env_header(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "[mcp_servers.factset]\n"
        'url = "https://mcp.factset.com"\n'
        'headers.X-Token = "env:FACTSET_TOKEN"\n',
        encoding="utf-8",
    )
    cfg = load_config(path=path, env={"FACTSET_TOKEN": "secret-token-1234"})
    assert cfg.mcp_servers["factset"].headers["X-Token"] == "secret-token-1234"


def test_load_config_rejects_invalid_toml(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("not-valid-toml = = =", encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path=path, env={})
    assert "TOML" in str(excinfo.value)
