"""Tests for ``gdr cancel``."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from gdr.cli import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GDR_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GDR_CONFIG_PATH", str(tmp_path / "no-such.toml"))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


def _install_sdk(
    mocker: Any,
    *,
    get_returns: Any,
    has_cancel: bool = True,
    cancel_raises: Exception | None = None,
) -> MagicMock:
    fake_interactions = MagicMock()
    fake_interactions.get.return_value = get_returns
    if has_cancel:
        if cancel_raises is not None:
            fake_interactions.cancel.side_effect = cancel_raises
        else:
            fake_interactions.cancel.return_value = None
    else:
        del fake_interactions.cancel  # attribute truly absent
    fake_client = MagicMock()
    fake_client.interactions = fake_interactions
    mocker.patch("google.genai.Client", return_value=fake_client)
    return fake_interactions


class TestCancel:
    def test_in_progress_cancelled_successfully(self, runner: CliRunner, mocker: Any) -> None:
        _install_sdk(
            mocker,
            get_returns=SimpleNamespace(id="intrun-1", status="in_progress", outputs=[]),
        )
        result = runner.invoke(app, ["cancel", "intrun-1", "--api-key", "AIzaSy-test-key-XXXX"])
        assert result.exit_code == 0
        assert "Cancel request sent" in result.output

    def test_already_terminal_short_circuits(self, runner: CliRunner, mocker: Any) -> None:
        fake = _install_sdk(
            mocker,
            get_returns=SimpleNamespace(id="intdone-2", status="completed", outputs=[]),
        )
        result = runner.invoke(app, ["cancel", "intdone-2", "--api-key", "AIzaSy-test-key-XXXX"])
        assert result.exit_code == 0
        assert "already in a terminal state" in result.output
        # cancel() should not have been called.
        assert fake.cancel.called is False

    def test_sdk_without_cancel_reports_clearly(self, runner: CliRunner, mocker: Any) -> None:
        _install_sdk(
            mocker,
            get_returns=SimpleNamespace(id="intrun-3", status="in_progress", outputs=[]),
            has_cancel=False,
        )
        result = runner.invoke(app, ["cancel", "intrun-3", "--api-key", "AIzaSy-test-key-XXXX"])
        assert result.exit_code == 4
        assert "interactions.cancel" in result.output

    def test_cancel_failure_exits_five(self, runner: CliRunner, mocker: Any) -> None:
        _install_sdk(
            mocker,
            get_returns=SimpleNamespace(id="intrun-4", status="in_progress", outputs=[]),
            cancel_raises=RuntimeError("API failure"),
        )
        result = runner.invoke(app, ["cancel", "intrun-4", "--api-key", "AIzaSy-test-key-XXXX"])
        assert result.exit_code == 5
        assert "API failure" in result.output
