"""Tests for ``gdr status``."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from gdr.cli import app
from gdr.core.models import Record
from gdr.core.persistence import JsonlStore

_UTC = timezone.utc


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GDR_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GDR_CONFIG_PATH", str(tmp_path / "no-such.toml"))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


def _install_fake_sdk(mocker: Any, *, got: Any) -> MagicMock:
    fake_interactions = MagicMock()
    fake_interactions.get.return_value = got
    fake_client = MagicMock()
    fake_client.interactions = fake_interactions
    mocker.patch("google.genai.Client", return_value=fake_client)
    return fake_interactions


def _seed_record(tmp_path: Path, interaction_id: str, *, created_at: datetime) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    store = JsonlStore.open(state_dir / "interactions.jsonl")
    store.append(
        Record(
            id=interaction_id,
            created_at=created_at,
            status="in_progress",
            agent="deep-research-preview-04-2026",
            query="Some query",
            output_dir=tmp_path / "reports" / "x",
        )
    )


class TestStatus:
    def test_completed_interaction(self, runner: CliRunner, tmp_path: Path, mocker: Any) -> None:
        _install_fake_sdk(
            mocker,
            got=SimpleNamespace(
                id="intdone-1",
                status="completed",
                outputs=[],
                usage=SimpleNamespace(total_tokens=5000),
            ),
        )
        result = runner.invoke(
            app,
            ["status", "intdone-1", "--api-key", "AIzaSy-test-key-XXXX"],
        )
        assert result.exit_code == 0, result.output
        assert "completed" in result.output
        assert "5000" in result.output

    def test_in_progress_interaction_includes_last_thought(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        _install_fake_sdk(
            mocker,
            got=SimpleNamespace(
                id="intrun-2",
                status="in_progress",
                outputs=[
                    SimpleNamespace(type="thought", summary="Reading pages..."),
                    SimpleNamespace(type="thought", summary="Synthesizing section 3"),
                ],
                usage=None,
            ),
        )
        result = runner.invoke(
            app,
            ["status", "intrun-2", "--api-key", "AIzaSy-test-key-XXXX"],
        )
        assert result.exit_code == 0, result.output
        assert "in_progress" in result.output
        assert "Synthesizing section 3" in result.output

    def test_elapsed_time_included_when_record_exists(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        _seed_record(
            tmp_path,
            "intlocal-3",
            created_at=datetime.now(_UTC).replace(microsecond=0),
        )
        _install_fake_sdk(
            mocker,
            got=SimpleNamespace(id="intlocal-3", status="in_progress", outputs=[], usage=None),
        )
        result = runner.invoke(
            app,
            ["status", "intlocal-3", "--api-key", "AIzaSy-test-key-XXXX"],
        )
        assert result.exit_code == 0, result.output
        assert "Elapsed" in result.output

    def test_failed_api_exits_five(self, runner: CliRunner, tmp_path: Path, mocker: Any) -> None:
        fake_interactions = MagicMock()
        fake_interactions.get.side_effect = RuntimeError("API down")
        fake_client = MagicMock()
        fake_client.interactions = fake_interactions
        mocker.patch("google.genai.Client", return_value=fake_client)
        result = runner.invoke(
            app,
            ["status", "intx", "--api-key", "AIzaSy-test-key-XXXX"],
        )
        assert result.exit_code == 5
        assert "API down" in result.output
