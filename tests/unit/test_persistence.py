"""Tests for `gdr.core.persistence`."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gdr.constants import AGENT_FAST
from gdr.core.models import Record
from gdr.core.persistence import JsonlStore, default_state_dir, default_store_path

_UTC = timezone.utc


def _record(
    *,
    id_: str = "rec-1",
    parent_id: str | None = None,
    status: str = "completed",
    created_at: datetime | None = None,
    query: str = "Q",
    output_dir: Path | None = None,
) -> Record:
    return Record(
        id=id_,
        parent_id=parent_id,
        created_at=created_at or datetime(2026, 4, 22, 14, 30, tzinfo=_UTC),
        status=status,
        agent=AGENT_FAST,
        query=query,
        output_dir=output_dir or Path("/tmp/gdr/x"),
    )


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


class TestPaths:
    def test_state_dir_respects_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GDR_STATE_DIR", "/tmp/gdr-state")
        assert default_state_dir() == Path("/tmp/gdr-state")

    def test_state_dir_uses_xdg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GDR_STATE_DIR", raising=False)
        monkeypatch.setenv("XDG_STATE_HOME", "/xdg/state")
        assert default_state_dir() == Path("/xdg/state/gdr")

    def test_state_dir_falls_back_to_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GDR_STATE_DIR", raising=False)
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        assert default_state_dir() == Path.home() / ".local" / "state" / "gdr"

    def test_default_store_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GDR_STATE_DIR", "/tmp/x")
        assert default_store_path() == Path("/tmp/x/interactions.jsonl")


# ---------------------------------------------------------------------------
# JsonlStore
# ---------------------------------------------------------------------------


class TestJsonlStore:
    def test_creates_parent_directory_on_open(self, tmp_path: Path) -> None:
        store_path = tmp_path / "nested" / "deep" / "store.jsonl"
        store = JsonlStore.open(store_path)
        assert store.path.parent.is_dir()
        assert len(store) == 0

    def test_append_and_find_by_id(self, tmp_path: Path) -> None:
        store = JsonlStore.open(tmp_path / "s.jsonl")
        rec = _record(id_="abc")
        store.append(rec)
        assert store.find_by_id("abc") == rec
        assert store.find_by_id("nope") is None
        assert len(store) == 1

    def test_append_persists_to_disk(self, tmp_path: Path) -> None:
        path = tmp_path / "s.jsonl"
        store = JsonlStore.open(path)
        store.append(_record(id_="abc"))
        # Re-open: new instance loads from disk.
        store2 = JsonlStore.open(path)
        assert store2.find_by_id("abc") is not None

    def test_appending_same_id_overwrites_index_entry(self, tmp_path: Path) -> None:
        store = JsonlStore.open(tmp_path / "s.jsonl")
        store.append(_record(id_="abc", status="in_progress"))
        store.append(_record(id_="abc", status="completed"))
        found = store.find_by_id("abc")
        assert found is not None
        assert found.status == "completed"
        # Writes stay append-only, so the file holds both lines until a
        # later `open()` compacts it (see TestCompaction). Consumers of
        # the raw file must treat the last entry per id as authoritative.

    def test_recent_sorts_by_created_at_descending(self, tmp_path: Path) -> None:
        store = JsonlStore.open(tmp_path / "s.jsonl")
        now = datetime(2026, 4, 22, 14, 30, tzinfo=_UTC)
        store.append(_record(id_="older", created_at=now - timedelta(minutes=10)))
        store.append(_record(id_="newer", created_at=now))
        ids = [r.id for r in store.recent()]
        assert ids == ["newer", "older"]

    def test_recent_respects_limit(self, tmp_path: Path) -> None:
        store = JsonlStore.open(tmp_path / "s.jsonl")
        base = datetime(2026, 4, 22, 14, 0, tzinfo=_UTC)
        for i in range(5):
            store.append(_record(id_=f"r{i}", created_at=base + timedelta(minutes=i)))
        assert [r.id for r in store.recent(limit=2)] == ["r4", "r3"]

    def test_recent_filters_by_status(self, tmp_path: Path) -> None:
        store = JsonlStore.open(tmp_path / "s.jsonl")
        store.append(_record(id_="a", status="completed"))
        store.append(_record(id_="b", status="failed"))
        completed = [r.id for r in store.recent(status="completed")]
        assert completed == ["a"]

    def test_recent_filters_by_since(self, tmp_path: Path) -> None:
        store = JsonlStore.open(tmp_path / "s.jsonl")
        early = datetime(2026, 4, 22, 10, 0, tzinfo=_UTC)
        late = datetime(2026, 4, 22, 15, 0, tzinfo=_UTC)
        store.append(_record(id_="old", created_at=early))
        store.append(_record(id_="new", created_at=late))
        since = [r.id for r in store.recent(since=datetime(2026, 4, 22, 12, 0, tzinfo=_UTC))]
        assert since == ["new"]

    def test_skips_unreadable_lines_on_load(self, tmp_path: Path) -> None:
        path = tmp_path / "s.jsonl"
        # Good record + malformed JSON + good record.
        good1 = _record(id_="a")
        good2 = _record(id_="b")
        with path.open("w", encoding="utf-8") as fh:
            fh.write(good1.model_dump_json() + "\n")
            fh.write("not-json-garbage\n")
            fh.write(good2.model_dump_json() + "\n")
        store = JsonlStore.open(path)
        assert store.find_by_id("a") is not None
        assert store.find_by_id("b") is not None
        assert len(store) == 2

    def test_skips_records_that_fail_schema_validation(self, tmp_path: Path) -> None:
        path = tmp_path / "s.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            fh.write(_record(id_="good").model_dump_json() + "\n")
            fh.write('{"malformed": true}\n')  # not a valid Record
        store = JsonlStore.open(path)
        assert len(store) == 1
        assert store.find_by_id("good") is not None

    def test_empty_lines_are_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "s.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            fh.write("\n\n")
            fh.write(_record(id_="x").model_dump_json() + "\n")
            fh.write("\n")
        store = JsonlStore.open(path)
        assert len(store) == 1


class TestSchemaEvolution:
    def test_rows_with_unknown_fields_still_load(self, tmp_path: Path) -> None:
        # Rows written by other gdr versions may carry fields this version
        # doesn't know (e.g. the retired `note`). History must not be
        # silently orphaned over that.
        store_path = tmp_path / "interactions.jsonl"
        row = {
            "id": "intcompat1",
            "parent_id": None,
            "created_at": "2026-07-01T00:00:00Z",
            "finished_at": None,
            "status": "completed",
            "agent": "deep-research-preview-04-2026",
            "query": "q",
            "output_dir": "/tmp/reports/x",
            "total_tokens": 10,
            "tools": [],
            "note": None,
            "field_from_the_future": {"nested": True},
        }
        store_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
        store = JsonlStore.open(store_path)
        assert store.find_by_id("intcompat1") is not None


# ---------------------------------------------------------------------------
# Compaction + retention
# ---------------------------------------------------------------------------


def _lines(path: Path) -> list[str]:
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _write_run(store: JsonlStore, id_: str, *, created_at: datetime) -> None:
    """Two appends, exactly like a real run: in_progress, then terminal."""
    store.append(_record(id_=id_, status="in_progress", created_at=created_at))
    store.append(_record(id_=id_, status="completed", created_at=created_at))


class TestCompaction:
    """The file must not grow forever.

    Every run appends twice (in_progress, then the terminal row), so the
    file grows at roughly double the rate of the history it represents,
    and nothing ever removed the superseded rows.
    """

    def test_reopening_drops_superseded_rows(self, tmp_path: Path) -> None:
        path = tmp_path / "s.jsonl"
        store = JsonlStore.open(path)
        base = datetime(2026, 4, 22, 14, 30, tzinfo=_UTC)
        for i in range(80):
            _write_run(store, f"run{i:03d}", created_at=base + timedelta(minutes=i))

        assert len(_lines(path)) == 160  # append-only during the run
        reopened = JsonlStore.open(path)
        assert len(_lines(path)) == 80  # one row per interaction
        assert len(reopened) == 80

    def test_compaction_keeps_the_latest_row_per_id(self, tmp_path: Path) -> None:
        path = tmp_path / "s.jsonl"
        store = JsonlStore.open(path)
        base = datetime(2026, 4, 22, 14, 30, tzinfo=_UTC)
        for i in range(80):
            _write_run(store, f"run{i:03d}", created_at=base + timedelta(minutes=i))

        reopened = JsonlStore.open(path)
        found = reopened.find_by_id("run007")
        assert found is not None
        assert found.status == "completed"
        # And the surviving row on disk says so too.
        rows = {json.loads(ln)["id"]: json.loads(ln)["status"] for ln in _lines(path)}
        assert rows["run007"] == "completed"

    def test_a_tight_file_is_left_alone(self, tmp_path: Path) -> None:
        # No dead rows worth reclaiming: don't rewrite the user's file
        # (or churn the disk) on every single command.
        path = tmp_path / "s.jsonl"
        store = JsonlStore.open(path)
        store.append(_record(id_="a"))
        store.append(_record(id_="b"))
        before = path.read_bytes()

        JsonlStore.open(path)
        assert path.read_bytes() == before

    def test_history_is_capped_at_max_records(self, tmp_path: Path) -> None:
        path = tmp_path / "s.jsonl"
        store = JsonlStore.open(path)
        base = datetime(2026, 4, 22, 14, 30, tzinfo=_UTC)
        for i in range(10):
            store.append(_record(id_=f"run{i}", created_at=base + timedelta(minutes=i)))

        reopened = JsonlStore.open(path, max_records=3)
        assert len(reopened) == 3
        assert [r.id for r in reopened.recent()] == ["run9", "run8", "run7"]
        assert len(_lines(path)) == 3
        # The oldest are gone from the index, not merely hidden.
        assert reopened.find_by_id("run0") is None

    def test_cap_of_zero_means_unlimited(self, tmp_path: Path) -> None:
        path = tmp_path / "s.jsonl"
        store = JsonlStore.open(path)
        base = datetime(2026, 4, 22, 14, 30, tzinfo=_UTC)
        for i in range(10):
            store.append(_record(id_=f"run{i}", created_at=base + timedelta(minutes=i)))

        assert len(JsonlStore.open(path, max_records=0)) == 10

    def test_a_file_that_wholly_failed_to_parse_is_never_truncated(self, tmp_path: Path) -> None:
        # An empty index next to a non-empty file means we could not read
        # it — the one situation where rewriting would destroy data we do
        # not understand.
        path = tmp_path / "s.jsonl"
        path.write_text("garbage\n" * 200, encoding="utf-8")
        JsonlStore.open(path, max_records=1)
        assert len(_lines(path)) == 200

    def test_compaction_leaves_no_temp_file_behind(self, tmp_path: Path) -> None:
        path = tmp_path / "s.jsonl"
        store = JsonlStore.open(path)
        base = datetime(2026, 4, 22, 14, 30, tzinfo=_UTC)
        for i in range(80):
            _write_run(store, f"run{i:03d}", created_at=base + timedelta(minutes=i))

        JsonlStore.open(path)
        assert sorted(p.name for p in tmp_path.iterdir()) == ["s.jsonl"]

    def test_rows_survive_a_round_trip_through_compaction(self, tmp_path: Path) -> None:
        path = tmp_path / "s.jsonl"
        store = JsonlStore.open(path)
        base = datetime(2026, 4, 22, 14, 30, tzinfo=_UTC)
        original = _record(id_="keeper", status="completed", created_at=base, query="Research TPUs")
        store.append(original)
        for i in range(80):
            _write_run(store, f"run{i:03d}", created_at=base + timedelta(minutes=i + 1))

        reopened = JsonlStore.open(path)
        assert reopened.find_by_id("keeper") == original
