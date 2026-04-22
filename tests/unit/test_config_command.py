"""Tests for ``gdr config`` (path / get / set / edit)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from gdr.cli import app

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GDR_CONFIG_PATH", str(tmp_path / "config.toml"))
    monkeypatch.setenv("GDR_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)


def _write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# path
# ---------------------------------------------------------------------------


class TestPath:
    def test_prints_default_path(self, runner: CliRunner, tmp_path: Path) -> None:
        result = runner.invoke(app, ["config", "path"])
        assert result.exit_code == 0
        assert str(tmp_path / "config.toml") in result.output

    def test_honors_override_flag(self, runner: CliRunner, tmp_path: Path) -> None:
        override = tmp_path / "elsewhere.toml"
        result = runner.invoke(app, ["config", "path", "--config", str(override)])
        assert result.exit_code == 0
        assert str(override) in result.output


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


class TestGet:
    def test_get_full_config_with_no_key(self, runner: CliRunner, tmp_path: Path) -> None:
        _write_config(tmp_path, 'default_agent = "deep-research-preview-04-2026"\n')
        result = runner.invoke(app, ["config", "get"])
        assert result.exit_code == 0
        assert "deep-research-preview-04-2026" in result.output

    def test_get_scalar_key(self, runner: CliRunner, tmp_path: Path) -> None:
        _write_config(tmp_path, 'default_agent = "deep-research-preview-04-2026"\n')
        result = runner.invoke(app, ["config", "get", "default_agent"])
        assert result.exit_code == 0
        assert "deep-research-preview-04-2026" in result.output

    def test_get_nested_key(self, runner: CliRunner, tmp_path: Path) -> None:
        body = (
            'default_agent = "deep-research-preview-04-2026"\n\n'
            "[mcp_servers.factset]\n"
            'url = "https://mcp.factset.com"\n'
        )
        _write_config(tmp_path, body)
        result = runner.invoke(app, ["config", "get", "mcp_servers.factset.url"])
        assert result.exit_code == 0
        assert "https://mcp.factset.com" in result.output

    def test_get_missing_key_exits_four(self, runner: CliRunner, tmp_path: Path) -> None:
        _write_config(tmp_path, 'default_agent = "x"\n')
        result = runner.invoke(app, ["config", "get", "not_a_key"])
        assert result.exit_code == 4
        assert "No such key" in result.output

    def test_get_empty_config_prints_defaults(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["config", "get"])
        assert result.exit_code == 0
        # Pydantic defaults are populated — just ensure the output is
        # not empty.
        assert "default_agent" in result.output


# ---------------------------------------------------------------------------
# set
# ---------------------------------------------------------------------------


class TestSet:
    def test_set_scalar_string(self, runner: CliRunner, tmp_path: Path) -> None:
        result = runner.invoke(
            app, ["config", "set", "default_agent", "deep-research-preview-04-2026"]
        )
        assert result.exit_code == 0, result.output
        written = tomllib.loads((tmp_path / "config.toml").read_text(encoding="utf-8"))
        assert written["default_agent"] == "deep-research-preview-04-2026"

    def test_set_bool_inference(self, runner: CliRunner, tmp_path: Path) -> None:
        result = runner.invoke(app, ["config", "set", "auto_open", "false"])
        assert result.exit_code == 0, result.output
        written = tomllib.loads((tmp_path / "config.toml").read_text(encoding="utf-8"))
        assert written["auto_open"] is False

    def test_set_refuses_nested_key(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["config", "set", "mcp_servers.factset.url", "https://x.com"])
        assert result.exit_code == 4
        assert "top-level keys" in result.output

    def test_set_rejects_invalid_value(self, runner: CliRunner) -> None:
        # thinking_summaries only accepts "auto" or "none".
        result = runner.invoke(app, ["config", "set", "thinking_summaries", "maybe"])
        assert result.exit_code == 4
        assert "invalid config" in result.output.lower()

    def test_set_preserves_existing_nested_tables(self, runner: CliRunner, tmp_path: Path) -> None:
        body = (
            'default_agent = "deep-research-preview-04-2026"\n\n'
            "[mcp_servers.factset]\n"
            'url = "https://mcp.factset.com"\n'
        )
        _write_config(tmp_path, body)

        result = runner.invoke(app, ["config", "set", "auto_open", "true"])
        assert result.exit_code == 0, result.output

        written = tomllib.loads((tmp_path / "config.toml").read_text(encoding="utf-8"))
        assert written["auto_open"] is True
        # Nested table must survive.
        assert written["mcp_servers"]["factset"]["url"] == "https://mcp.factset.com"


# ---------------------------------------------------------------------------
# edit
# ---------------------------------------------------------------------------


class TestEdit:
    def test_creates_template_when_missing(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        mocker.patch("shutil.which", return_value="/usr/bin/vi")
        mocker.patch("subprocess.run")
        result = runner.invoke(app, ["config", "edit"])
        assert result.exit_code == 0
        path = tmp_path / "config.toml"
        assert path.is_file()
        assert "gdr config" in path.read_text(encoding="utf-8")

    def test_launches_configured_editor(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
    ) -> None:
        monkeypatch.setenv("EDITOR", "nano")
        mocker.patch("shutil.which", return_value="/usr/bin/nano")
        run_mock = mocker.patch("subprocess.run")

        result = runner.invoke(app, ["config", "edit"])
        assert result.exit_code == 0
        args, _ = run_mock.call_args
        cmd = args[0]
        assert cmd[0] == "nano"
        assert cmd[-1].endswith("config.toml")

    def test_missing_editor_exits_four(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, mocker: Any
    ) -> None:
        monkeypatch.setenv("EDITOR", "nonexistent-editor")
        mocker.patch("shutil.which", return_value=None)
        result = runner.invoke(app, ["config", "edit"])
        assert result.exit_code == 4
        assert "not found on PATH" in result.output


# ---------------------------------------------------------------------------
# Help sanity
# ---------------------------------------------------------------------------


class TestHelp:
    def test_config_top_level_help(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["config", "--help"])
        assert result.exit_code == 0
        for sub in ("path", "get", "set", "edit"):
            assert sub in result.output
