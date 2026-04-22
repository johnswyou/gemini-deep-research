"""Tests for ``gdr follow-up``."""

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


def _write_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        f'output_dir = "{tmp_path / "reports"}"\nconfirm_max = false\n',
        encoding="utf-8",
    )
    return path


def _fake_completed(id_: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=id_,
        status="completed",
        outputs=[SimpleNamespace(type="text", text="Follow-up answer.", annotations=[])],
        usage=SimpleNamespace(total_tokens=500),
    )


def _install_fake_sdk(mocker: Any, *, created: Any, got: Any) -> MagicMock:
    fake_interactions = MagicMock()
    fake_interactions.create.return_value = created
    fake_interactions.get.return_value = got
    fake_client = MagicMock()
    fake_client.interactions = fake_interactions
    mocker.patch("google.genai.Client", return_value=fake_client)
    return fake_interactions


class TestFollowUp:
    def test_passes_previous_id_and_question(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path)
        fake = _install_fake_sdk(
            mocker,
            created=SimpleNamespace(id="intfu-xyz", status="in_progress"),
            got=_fake_completed("intfu-xyz"),
        )

        result = runner.invoke(
            app,
            [
                "follow-up",
                "intparent-abc",
                "Elaborate on section 3.",
                "--no-stream",
                "--config",
                str(cfg),
                "--api-key",
                "AIzaSy-test-key-XXXX",
            ],
        )
        assert result.exit_code == 0, result.output
        call_kwargs = fake.create.call_args.kwargs
        assert call_kwargs["previous_interaction_id"] == "intparent-abc"
        assert call_kwargs["input"] == "Elaborate on section 3."
        # Follow-up uses the execution agent_config (collaborative_planning=False).
        assert call_kwargs["agent_config"]["collaborative_planning"] is False

    def test_dry_run_skips_api(self, runner: CliRunner, tmp_path: Path, mocker: Any) -> None:
        cfg = _write_config(tmp_path)
        mock_client_ctor = mocker.patch("google.genai.Client")
        result = runner.invoke(
            app,
            [
                "follow-up",
                "intparent-abc",
                "What about next quarter?",
                "--dry-run",
                "--config",
                str(cfg),
            ],
        )
        assert result.exit_code == 0, result.output
        mock_client_ctor.assert_not_called()
        assert "Dry run" in result.output
        assert "intparent-abc" in result.output

    def test_writes_artifacts_to_output_dir(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path)
        _install_fake_sdk(
            mocker,
            created=SimpleNamespace(id="intfu-001", status="in_progress"),
            got=_fake_completed("intfu-001"),
        )
        result = runner.invoke(
            app,
            [
                "follow-up",
                "intparent-xyz",
                "Tell me more.",
                "--no-stream",
                "--config",
                str(cfg),
                "--api-key",
                "AIzaSy-test-key-XXXX",
            ],
        )
        assert result.exit_code == 0, result.output
        runs = list((tmp_path / "reports").glob("*_intfu*"))
        assert len(runs) == 1
        assert (runs[0] / "report.md").read_text(encoding="utf-8").startswith("# Tell me more.")
