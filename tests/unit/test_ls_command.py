"""Tests for ``gdr ls``."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)


def _seed_records(tmp_path: Path, *records: Record) -> Path:
    """Write records directly to the state dir the CLI will read from."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    store_path = state_dir / "interactions.jsonl"
    store = JsonlStore.open(store_path)
    for r in records:
        store.append(r)
    return store_path


def _record(
    id_: str,
    *,
    created_at: datetime,
    status: str = "completed",
    agent: str = "deep-research-preview-04-2026",
    query: str = "A query",
    total_tokens: int = 1000,
) -> Record:
    return Record(
        id=id_,
        created_at=created_at,
        finished_at=created_at + timedelta(minutes=5),
        status=status,
        agent=agent,
        query=query,
        output_dir=Path("/tmp/reports/x"),
        total_tokens=total_tokens,
    )


# ---------------------------------------------------------------------------
# Basic behavior
# ---------------------------------------------------------------------------


class TestLs:
    def test_empty_store_prints_friendly_message(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["ls"])
        assert result.exit_code == 0
        assert "No matching interactions" in result.output

    def test_shows_recent_first(self, runner: CliRunner, tmp_path: Path) -> None:
        older = _record("intold-1", created_at=datetime(2026, 4, 20, 10, 0, tzinfo=_UTC))
        newer = _record("intnew-2", created_at=datetime(2026, 4, 22, 10, 0, tzinfo=_UTC))
        _seed_records(tmp_path, older, newer)

        result = runner.invoke(app, ["ls"])
        assert result.exit_code == 0
        # Newer id appears before older in the output.
        assert result.output.index("intnew-2") < result.output.index("intold-1")

    def test_limit_caps_results(self, runner: CliRunner, tmp_path: Path) -> None:
        recs = [
            _record(f"int-{i:02d}", created_at=datetime(2026, 4, 22, i, 0, tzinfo=_UTC))
            for i in range(5)
        ]
        _seed_records(tmp_path, *recs)

        result = runner.invoke(app, ["ls", "--limit", "2"])
        assert result.exit_code == 0
        # Should show 2 of 5.
        shown = sum(1 for i in range(5) if f"int-{i:02d}"[:12] in result.output)
        assert shown == 2

    def test_status_filter(self, runner: CliRunner, tmp_path: Path) -> None:
        ok = _record("intok-abc", created_at=datetime(2026, 4, 22, tzinfo=_UTC), status="completed")
        fail = _record(
            "intfail-xyz", created_at=datetime(2026, 4, 21, tzinfo=_UTC), status="failed"
        )
        _seed_records(tmp_path, ok, fail)

        result = runner.invoke(app, ["ls", "--status", "failed"])
        assert result.exit_code == 0
        assert "intfail" in result.output
        assert "intok" not in result.output

    def test_since_relative(self, runner: CliRunner, tmp_path: Path) -> None:
        now = datetime.now(_UTC)
        recent = _record("intrecent-1", created_at=now - timedelta(hours=1))
        old = _record("intold-2", created_at=now - timedelta(days=30))
        _seed_records(tmp_path, recent, old)

        result = runner.invoke(app, ["ls", "--since", "7d"])
        assert result.exit_code == 0
        assert "intrecent" in result.output
        assert "intold-2" not in result.output

    def test_since_invalid_value_exits_four(self, runner: CliRunner, tmp_path: Path) -> None:
        _seed_records(
            tmp_path,
            _record("int-x", created_at=datetime(2026, 4, 22, tzinfo=_UTC)),
        )
        result = runner.invoke(app, ["ls", "--since", "not-a-date"])
        assert result.exit_code == 4
        assert "not a recognized date" in result.output

    def test_full_id_flag(self, runner: CliRunner, tmp_path: Path) -> None:
        long_id = "intabcdefghijklmnopqrstuvwxyz"
        _seed_records(tmp_path, _record(long_id, created_at=datetime(2026, 4, 22, tzinfo=_UTC)))
        default = runner.invoke(app, ["ls"])
        full = runner.invoke(app, ["ls", "--full-id"])
        assert default.exit_code == full.exit_code == 0
        assert long_id not in default.output
        assert long_id in full.output
