"""Local interaction store.

Records are appended one-per-line as JSON to an append-only file at
``$XDG_STATE_HOME/gdr/interactions.jsonl`` (fallback: ``~/.local/state/gdr/
interactions.jsonl``). On load we build an in-memory index keyed by
interaction id — O(1) lookup, cheap even at tens of thousands of rows.

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

    def list_children(self, parent_id: str) -> list[Record]: ...


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
    _loaded: bool = field(default=False, repr=False)

    # -- construction --------------------------------------------------

    @classmethod
    def open(cls, path: Path | None = None) -> JsonlStore:
        """Open or create the store. Missing parent dirs are created."""
        target = path if path is not None else default_store_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        store = cls(path=target)
        store._load()
        return store

    def _load(self) -> None:
        self._index.clear()
        if not self.path.exists():
            self._loaded = True
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
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
                _ = lineno  # reserved for future error messages
        self._loaded = True

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

    def list_children(self, parent_id: str) -> list[Record]:
        """Return records whose parent_id equals the given interaction id."""
        return sorted(
            (r for r in self._index.values() if r.parent_id == parent_id),
            key=lambda r: r.created_at,
        )

    # -- introspection -------------------------------------------------

    def __len__(self) -> int:
        return len(self._index)
