"""Tests for ``gdr doctor``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from gdr.cli import app


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


@pytest.fixture(autouse=True)
def _mock_dns(mocker: Any) -> Any:
    """Mock DNS by default so tests never actually hit the network."""
    return mocker.patch("gdr.commands.doctor.socket.gethostbyname", return_value="142.250.0.1")


def _write_config(tmp_path: Path, body: str = "") -> Path:
    """Write (or overwrite) the config at the autouse GDR_CONFIG_PATH."""
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


class TestDoctor:
    def test_all_pass_when_env_healthy(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "AIzaSy-test-key-ABCDEFGH")
        output_dir = tmp_path / "reports"
        output_dir.mkdir()
        (tmp_path / "state").mkdir()
        _write_config(tmp_path, f'output_dir = "{output_dir}"\n')

        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0, result.output
        assert "PASS" in result.output
        assert "all checks pass" in result.output
        # API key should show a fingerprint, never the raw key.
        assert "AIzaSy-test-key-ABCDEFGH" not in result.output
        assert "AIza" in result.output  # prefix of fingerprint

    def test_missing_api_key_fails(self, runner: CliRunner, tmp_path: Path) -> None:
        (tmp_path / "reports").mkdir()
        (tmp_path / "state").mkdir()
        _write_config(tmp_path, f'output_dir = "{tmp_path / "reports"}"\n')

        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 4
        assert "API key" in result.output
        assert "FAIL" in result.output

    def test_dns_failure_fails(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mocker: Any,
    ) -> None:
        # Override the autouse DNS mock.
        mocker.patch(
            "gdr.commands.doctor.socket.gethostbyname",
            side_effect=OSError("Name or service not known"),
        )
        monkeypatch.setenv("GEMINI_API_KEY", "AIzaSy-test-key-ABCDEFGH")
        (tmp_path / "reports").mkdir()
        (tmp_path / "state").mkdir()
        _write_config(tmp_path, f'output_dir = "{tmp_path / "reports"}"\n')

        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 4
        assert "cannot resolve" in result.output

    def test_missing_config_warns_without_fix(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "AIzaSy-test-key-ABCDEFGH")
        result = runner.invoke(app, ["doctor"])
        # Missing config file + missing output_dir + missing state_dir are all
        # WARN-level (not FAIL), so we should exit 0 but see warnings.
        assert result.exit_code == 0, result.output
        assert "WARN" in result.output

    def test_fix_creates_missing_dirs_and_config(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "AIzaSy-test-key-ABCDEFGH")
        # Note: NO directories or config file seeded.
        result = runner.invoke(app, ["doctor", "--fix"])
        assert result.exit_code == 0, result.output

        config_path = tmp_path / "config.toml"
        assert config_path.is_file()
        # Default output_dir is ~/gdr-reports — we can't assert on that
        # safely in CI. But state_dir under GDR_STATE_DIR should exist.
        assert (tmp_path / "state").is_dir()

    def test_fix_is_idempotent(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "AIzaSy-test-key-ABCDEFGH")
        first = runner.invoke(app, ["doctor", "--fix"])
        assert first.exit_code == 0, first.output
        second = runner.invoke(app, ["doctor", "--fix"])
        assert second.exit_code == 0, second.output

    def test_malformed_config_reported_as_fail(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "AIzaSy-test-key-ABCDEFGH")
        _write_config(tmp_path, "this is not = valid toml ][")
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 4
        assert "config file" in result.output.lower()

    def test_non_writable_output_dir_fails(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mocker: Any,
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "AIzaSy-test-key-ABCDEFGH")
        output_dir = tmp_path / "reports"
        output_dir.mkdir()
        (tmp_path / "state").mkdir()
        _write_config(tmp_path, f'output_dir = "{output_dir}"\n')

        # Simulate a non-writable directory without chmod gymnastics.
        def _access(path: Any, mode: int) -> bool:
            return str(path) != str(output_dir)

        mocker.patch("gdr.commands.doctor.os.access", side_effect=_access)

        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 4
        assert "not writable" in result.output
