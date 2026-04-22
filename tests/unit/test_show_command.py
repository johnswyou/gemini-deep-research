"""Tests for ``gdr show``."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

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


def _seed_run(tmp_path: Path, interaction_id: str = "intshow123") -> Path:
    """Write a fake run directory with all artifacts + a matching record."""
    run_dir = tmp_path / "reports" / "run1"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "report.md").write_text("# Test Query\n\nA body paragraph.\n", encoding="utf-8")
    (run_dir / "sources.json").write_text(
        json.dumps(
            {
                "interaction_id": interaction_id,
                "sources": [{"type": "url_citation", "url": "https://a"}],
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "metadata.json").write_text(
        json.dumps({"interaction_id": interaction_id, "status": "completed"}),
        encoding="utf-8",
    )
    (run_dir / "transcript.json").write_text(
        json.dumps({"interaction_id": interaction_id, "outputs": []}),
        encoding="utf-8",
    )
    (run_dir / "images").mkdir(exist_ok=True)
    (run_dir / "images" / "image_001.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 10)

    # Seed the store.
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    store = JsonlStore.open(state_dir / "interactions.jsonl")
    store.append(
        Record(
            id=interaction_id,
            created_at=datetime(2026, 4, 22, 14, tzinfo=_UTC),
            finished_at=datetime(2026, 4, 22, 14, 5, tzinfo=_UTC),
            status="completed",
            agent="deep-research-preview-04-2026",
            query="Test Query",
            output_dir=run_dir,
            total_tokens=1234,
        )
    )
    return run_dir


class TestShow:
    def test_default_part_is_text(self, runner: CliRunner, tmp_path: Path) -> None:
        _seed_run(tmp_path)
        result = runner.invoke(app, ["show", "intshow123"])
        assert result.exit_code == 0
        assert "Test Query" in result.output
        assert "A body paragraph." in result.output

    def test_sources_part(self, runner: CliRunner, tmp_path: Path) -> None:
        _seed_run(tmp_path)
        result = runner.invoke(app, ["show", "intshow123", "--part", "sources"])
        assert result.exit_code == 0
        assert "https://a" in result.output

    def test_metadata_part(self, runner: CliRunner, tmp_path: Path) -> None:
        _seed_run(tmp_path)
        result = runner.invoke(app, ["show", "intshow123", "--part", "metadata"])
        assert result.exit_code == 0
        assert "completed" in result.output
        assert "intshow123" in result.output

    def test_transcript_part(self, runner: CliRunner, tmp_path: Path) -> None:
        _seed_run(tmp_path)
        result = runner.invoke(app, ["show", "intshow123", "--part", "transcript"])
        assert result.exit_code == 0
        assert "intshow123" in result.output

    def test_images_part_lists_files(self, runner: CliRunner, tmp_path: Path) -> None:
        run_dir = _seed_run(tmp_path)
        result = runner.invoke(app, ["show", "intshow123", "--part", "images"])
        assert result.exit_code == 0
        assert "image_001.png" in result.output
        # Full path present (or at least the filename).
        assert (
            str(run_dir / "images" / "image_001.png") in result.output
            or "image_001.png" in result.output
        )

    def test_images_part_empty_dir(self, runner: CliRunner, tmp_path: Path) -> None:
        run_dir = _seed_run(tmp_path)
        # Remove the images.
        for p in (run_dir / "images").iterdir():
            p.unlink()
        result = runner.invoke(app, ["show", "intshow123", "--part", "images"])
        assert result.exit_code == 0
        assert "No images" in result.output

    def test_unknown_id_exits_four(self, runner: CliRunner, tmp_path: Path) -> None:
        _seed_run(tmp_path)
        result = runner.invoke(app, ["show", "does-not-exist"])
        assert result.exit_code == 4
        assert "No record" in result.output

    def test_prefix_match_single(self, runner: CliRunner, tmp_path: Path) -> None:
        _seed_run(tmp_path, interaction_id="intshow123")
        result = runner.invoke(app, ["show", "intshow"])
        assert result.exit_code == 0
        assert "A body paragraph." in result.output

    def test_output_dir_missing_exits_four(self, runner: CliRunner, tmp_path: Path) -> None:
        run_dir = _seed_run(tmp_path)
        # Nuke the run directory so the record still exists but files don't.
        for p in run_dir.rglob("*"):
            if p.is_file():
                p.unlink()
        for sub in sorted(run_dir.rglob("*"), reverse=True):
            if sub.is_dir():
                sub.rmdir()
        run_dir.rmdir()
        result = runner.invoke(app, ["show", "intshow123"])
        assert result.exit_code == 4
        assert "output directory is missing" in result.output
