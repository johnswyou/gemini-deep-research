"""Local interaction store.

Records are appended one-per-line as JSON to an append-only file at
``$XDG_STATE_HOME/gdr/interactions.jsonl`` (fallback: ``~/.local/state/gdr/
interactions.jsonl``). On load we build an in-memory index keyed by
interaction id — O(1) lookup, cheap even at tens of thousands of rows.

Writes are append-only, but ``open()`` compacts: superseded rows are
dropped (a run writes its id twice — ``in_progress``, then terminal) and
the history is trimmed to :data:`MAX_RECORDS`. Without that the file
grows forever and every command pays to parse it.

This module exposes a :class:`Store` Protocol so callers depend on a small,
testable interface. Phase 3 ships a single :class:`JsonlStore` implementation
behind it; a SQLite-backed variant can drop in later without any caller
changes. That's the contract the plan committed to.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from gdr.core.models import Record

_STORE_FILENAME = "interactions.jsonl"

# Hard cap on retained records, applied when the store is opened. The
# store is a local *index*, not a ledger: pruning it never touches the
# artifacts on disk, it only means `gdr show`/`gdr ls` stop resolving
# very old ids. 5000 Deep Research runs is far beyond any real history.
# 0 disables the cap.
MAX_RECORDS = 5000

# One row per interaction is the steady state, but a run appends twice
# (in_progress as soon as the id is known, then the terminal row), so
# dead rows accrue at roughly one per run. Rewriting the whole file on
# every open would be pointless churn — wait until enough have piled up.
_DEAD_ROW_FLOOR = 64


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def default_state_dir() -> Path:
    """Canonical state dir: ``$GDR_STATE_DIR`` → ``$XDG_STATE_HOME/gdr/`` →
    ``~/.local/state/gdr/``.

    We do not use ``platformdirs`` for the same reason we avoid it in
    ``config.py``: predictability over platform idiom. Terminal tools are
    expected at XDG-style paths.
    """
    override = os.environ.get("GDR_STATE_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "gdr"
    return Path.home() / ".local" / "state" / "gdr"


def default_store_path() -> Path:
    return default_state_dir() / _STORE_FILENAME


# ---------------------------------------------------------------------------
# Store Protocol
# ---------------------------------------------------------------------------


class Store(Protocol):
    """Minimal interface every store implementation must satisfy.

    Keeping this tight lets us swap JsonlStore → SQLiteStore in v1.2 by
    adding one file; no command handler needs to change.
    """

    def append(self, record: Record) -> None: ...

    def find_by_id(self, id_: str) -> Record | None: ...

    def recent(
        self,
        *,
        limit: int | None = None,
        status: str | None = None,
        since: datetime | None = None,
    ) -> list[Record]: ...


# ---------------------------------------------------------------------------
# JSONL implementation
# ---------------------------------------------------------------------------


@dataclass
class JsonlStore:
    """Append-only JSONL store with an in-memory index.

    ``path`` is the file containing records; creating the parent directories
    is the store's responsibility so callers don't have to.

    Records are loaded once at construction. Subsequent ``append`` calls
    write to disk AND update the index so the object stays coherent for the
    life of the process. Concurrent writers are not supported — gdr is a
    single-process CLI, and layering file locking here would be churn ahead
    of a need.
    """

    path: Path
    _index: dict[str, Record] = field(default_factory=dict, repr=False)

    # -- construction --------------------------------------------------

    @classmethod
    def open(cls, path: Path | None = None, *, max_records: int = MAX_RECORDS) -> JsonlStore:
        """Open or create the store. Missing parent dirs are created.

        Opening is also when the file gets tidied: superseded rows are
        dropped and the history is trimmed to ``max_records`` (0 keeps
        everything). See :meth:`_compact_if_needed`.
        """
        target = path if path is not None else default_store_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        store = cls(path=target)
        rows = store._load()
        store._compact_if_needed(rows, max_records=max_records)
        return store

    def _load(self) -> int:
        """Rebuild the index from disk; return the number of rows read.

        The count includes rows that failed to parse — they occupy the
        file just the same, and compaction is what reclaims them.
        """
        self._index.clear()
        if not self.path.exists():
            return 0
        rows = 0
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                rows += 1
                try:
                    data = json.loads(stripped)
                except json.JSONDecodeError:
                    # Skip unreadable lines rather than crash on a single
                    # corrupt row — the store is best-effort, not a ledger.
                    continue
                try:
                    record = Record.model_validate(data)
                except (ValueError, TypeError):
                    continue
                self._index[record.id] = record
        return rows

    # -- compaction ----------------------------------------------------

    def _compact_if_needed(self, rows_on_disk: int, *, max_records: int) -> None:
        """Rewrite the file from the index when it has gone slack.

        Two triggers: accumulated dead rows (superseded ids and lines
        that no longer parse) and the retention cap.

        Never runs when the index is empty but the file is not. That
        means every row failed to parse, and truncating a file we could
        not read would be the worst possible response to not
        understanding it.
        """
        if not self._index:
            return
        over_cap = 0 < max_records < len(self._index)
        if not over_cap and rows_on_disk - len(self._index) < _DEAD_ROW_FLOOR:
            return
        self._rewrite(max_records=max_records if over_cap else 0)

    def _rewrite(self, *, max_records: int) -> None:
        """Replace the file with one line per retained record, atomically.

        Best-effort by design, like every other write here: a read-only
        state dir, a full disk, or a race with another gdr process must
        not break the command the user actually ran.
        """
        tmp = self.path.with_name(self.path.name + ".compact")
        try:
            ordered = sorted(self._index.values(), key=lambda r: r.created_at)
            if max_records > 0:
                ordered = ordered[-max_records:]
                self._index = {record.id: record for record in ordered}
            with tmp.open("w", encoding="utf-8") as fh:
                for record in ordered:
                    fh.write(record.model_dump_json() + "\n")
            tmp.replace(self.path)
        except (OSError, TypeError):
            # TypeError: a mix of naive and aware `created_at` values makes
            # the sort uncomparable. Leave the file exactly as it was.
            tmp.unlink(missing_ok=True)

    # -- mutators ------------------------------------------------------

    def append(self, record: Record) -> None:
        """Append a record to the store. Idempotent on id collision (overwrites
        the in-memory slot with the latest value)."""
        serialized = record.model_dump_json()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(serialized + "\n")
        self._index[record.id] = record

    # -- accessors -----------------------------------------------------

    def find_by_id(self, id_: str) -> Record | None:
        return self._index.get(id_)

    def recent(
        self,
        *,
        limit: int | None = None,
        status: str | None = None,
        since: datetime | None = None,
    ) -> list[Record]:
        """Return records in reverse chronological order by created_at."""
        records: Iterable[Record] = self._index.values()
        if status is not None:
            records = (r for r in records if r.status == status)
        if since is not None:
            records = (r for r in records if r.created_at >= since)
        sorted_records = sorted(records, key=lambda r: r.created_at, reverse=True)
        if limit is not None:
            sorted_records = sorted_records[:limit]
        return sorted_records

    # -- introspection -------------------------------------------------

    def __len__(self) -> int:
        return len(self._index)
