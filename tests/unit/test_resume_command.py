"""Tests for ``gdr resume``."""

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


def _write_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        f'output_dir = "{tmp_path / "reports"}"\nconfirm_max = false\n',
        encoding="utf-8",
    )
    return path


def _seed_record(tmp_path: Path, interaction_id: str = "intresume-1") -> tuple[Path, Record]:
    output_dir = tmp_path / "reports" / "original"
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    store = JsonlStore.open(state_dir / "interactions.jsonl")
    record = Record(
        id=interaction_id,
        created_at=datetime(2026, 4, 22, 14, 0, tzinfo=_UTC),
        status="in_progress",
        agent="deep-research-preview-04-2026",
        query="My query",
        output_dir=output_dir,
    )
    store.append(record)
    return output_dir, record


def _completed_interaction(id_: str = "intresume-1") -> SimpleNamespace:
    return SimpleNamespace(
        id=id_,
        status="completed",
        outputs=[
            SimpleNamespace(
                type="text",
                text="Finished report body.",
                annotations=[],
            )
        ],
        usage=SimpleNamespace(total_tokens=900),
    )


def _install_fake_sdk(mocker: Any, *, got: Any) -> MagicMock:
    fake_interactions = MagicMock()
    fake_interactions.get.return_value = got
    fake_client = MagicMock()
    fake_client.interactions = fake_interactions
    mocker.patch("google.genai.Client", return_value=fake_client)
    return fake_interactions


class TestResume:
    def test_terminal_interaction_writes_artifacts(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path)
        _seed_record(tmp_path, "intresume-1")
        _install_fake_sdk(mocker, got=_completed_interaction("intresume-1"))

        result = runner.invoke(
            app,
            [
                "resume",
                "intresume-1",
                "--config",
                str(cfg),
                "--api-key",
                "AIzaSy-test-key-XXXX",
            ],
        )
        assert result.exit_code == 0, result.output
        # Artifacts written to original dir (it was empty).
        assert (tmp_path / "reports" / "original" / "report.md").is_file()
        assert "Resumed" in result.output

    def test_missing_record_exits_four(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path)
        mocker.patch("google.genai.Client")
        result = runner.invoke(
            app,
            [
                "resume",
                "intnever-seen",
                "--config",
                str(cfg),
                "--api-key",
                "AIzaSy-test-key-XXXX",
            ],
        )
        assert result.exit_code == 4
        assert "No local record" in result.output

    def test_existing_nonempty_dir_suffixed_without_force(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path)
        output_dir, _ = _seed_record(tmp_path, "intresume-2")
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "preserved.txt").write_text("keep me", encoding="utf-8")

        _install_fake_sdk(mocker, got=_completed_interaction("intresume-2"))

        result = runner.invoke(
            app,
            [
                "resume",
                "intresume-2",
                "--config",
                str(cfg),
                "--api-key",
                "AIzaSy-test-key-XXXX",
            ],
        )
        assert result.exit_code == 0, result.output
        # Original file preserved.
        assert (output_dir / "preserved.txt").read_text(encoding="utf-8") == "keep me"
        # A sibling dir with a _resumed_ suffix was created.
        siblings = [
            p
            for p in output_dir.parent.iterdir()
            if p.is_dir() and p.name.startswith("original_resumed_")
        ]
        assert len(siblings) == 1
        assert (siblings[0] / "report.md").is_file()

    def test_force_overwrites_original_dir(
        self, runner: CliRunner, tmp_path: Path, mocker: Any
    ) -> None:
        cfg = _write_config(tmp_path)
        output_dir, _ = _seed_record(tmp_path, "intresume-3")
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "old.txt").write_text("stale", encoding="utf-8")

        _install_fake_sdk(mocker, got=_completed_interaction("intresume-3"))

        result = runner.invoke(
            app,
            [
                "resume",
                "intresume-3",
                "--force",
                "--config",
                str(cfg),
                "--api-key",
                "AIzaSy-test-key-XXXX",
            ],
        )
        assert result.exit_code == 0, result.output
        assert (output_dir / "report.md").is_file()
        # Sibling _resumed_ dir was NOT created.
        siblings = [
            p
            for p in output_dir.parent.iterdir()
            if p.is_dir() and p.name.startswith("original_resumed_")
        ]
        assert len(siblings) == 0
