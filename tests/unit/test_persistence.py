"""Tests for `gdr.core.persistence`."""

from __future__ import annotations

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
        # The file retains both lines (append-only), but the in-memory index
        # reflects the latest value. Consumers of the file should treat the
        # last entry per id as authoritative.

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

    def test_list_children_filters_by_parent(self, tmp_path: Path) -> None:
        store = JsonlStore.open(tmp_path / "s.jsonl")
        store.append(_record(id_="parent"))
        store.append(_record(id_="child-1", parent_id="parent"))
        store.append(_record(id_="child-2", parent_id="parent"))
        store.append(_record(id_="unrelated", parent_id="other"))
        kids = [r.id for r in store.list_children("parent")]
        assert set(kids) == {"child-1", "child-2"}

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
