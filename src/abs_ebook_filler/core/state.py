"""SQLite-backed item state shared by the CLI, web UI and worker."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from .models import Book

STATUSES = ("missing", "queued", "downloading", "done", "skipped", "failed")
ACTIVE = ("queued", "downloading")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    item_id     TEXT PRIMARY KEY,
    library_id  TEXT NOT NULL DEFAULT '',
    title       TEXT NOT NULL DEFAULT '',
    clean_title TEXT NOT NULL DEFAULT '',
    query       TEXT NOT NULL DEFAULT '',
    author      TEXT NOT NULL DEFAULT '',
    series      TEXT NOT NULL DEFAULT '',
    path        TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'missing',
    progress    REAL,
    message     TEXT NOT NULL DEFAULT '',
    release     TEXT,
    ebook_path  TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
CREATE TABLE IF NOT EXISTS prefs (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS search_cache (
    item_id    TEXT NOT NULL,
    query      TEXT NOT NULL,
    created_at REAL NOT NULL,
    complete   INTEGER NOT NULL,   -- 0 when a source failed (e.g. rate-limited) -> results may be partial
    data       TEXT NOT NULL,      -- {"cands": [...], "warnings": [...]}
    PRIMARY KEY (item_id, query)
);
"""


def source_key(label: str) -> str:
    return (label or "").strip().lower()


class State:
    def __init__(self, db_path: str | Path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(items)")}
        if "queued_at" not in cols:
            # When the book entered the download queue; keeps the Activity list in queue order.
            self._conn.execute("ALTER TABLE items ADD COLUMN queued_at REAL")

    def close(self) -> None:
        self._conn.close()

    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    @staticmethod
    def _row(r: sqlite3.Row | None) -> dict[str, Any] | None:
        if r is None:
            return None
        d = dict(r)
        d["release"] = json.loads(d["release"]) if d.get("release") else None
        return d

    # ---- sync with ABS -----------------------------------------------------------
    def sync_missing(self, books: list[Book]) -> dict[str, int]:
        """Insert newly-missing books, refresh metadata, and drop rows ABS no longer reports.

        Rows that are done/queued/downloading are never dropped.
        """
        now = time.time()
        seen = {b.item_id for b in books}
        with self._lock:
            for b in books:
                self._conn.execute(
                    """INSERT INTO items (item_id, library_id, title, clean_title, query, author, series,
                                          path, status, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?, 'missing', ?, ?)
                       ON CONFLICT(item_id) DO UPDATE SET
                         library_id=excluded.library_id, title=excluded.title,
                         clean_title=excluded.clean_title, author=excluded.author,
                         series=excluded.series, path=excluded.path""",
                    (b.item_id, b.library_id, b.title, b.clean_title, "", b.author, b.series,
                     b.path, now, now),
                )
            existing = [r[0] for r in self._conn.execute(
                "SELECT item_id FROM items WHERE status IN ('missing','skipped','failed')")]
            stale = [i for i in existing if i not in seen]
            self._conn.executemany("DELETE FROM items WHERE item_id=?", [(i,) for i in stale])
            self._conn.commit()
        return {"found": len(books), "removed": len(stale)}

    # ---- reads -------------------------------------------------------------------
    def get(self, item_id: str) -> dict[str, Any] | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM items WHERE item_id=?", (item_id,)).fetchone()
        return self._row(r)

    def list(self, status: str | None = None, library_id: str | None = None,
             q: str | None = None, limit: int = 1000) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM items WHERE 1=1", []
        if status:
            sql += " AND status=?"
            params.append(status)
        if library_id:
            sql += " AND library_id=?"
            params.append(library_id)
        if q:
            sql += " AND (title LIKE ? OR author LIKE ? OR clean_title LIKE ?)"
            params += [f"%{q}%"] * 3
        sql += " ORDER BY author COLLATE NOCASE, series COLLATE NOCASE, title COLLATE NOCASE LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row(r) for r in rows]

    def active(self) -> list[dict[str, Any]]:
        """Queue in processing order: whatever is downloading first, then the rest as queued.

        Deliberately not ordered by updated_at, which changes on every progress update.
        """
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM items WHERE status IN ('queued','downloading')
                   ORDER BY CASE status WHEN 'downloading' THEN 0 ELSE 1 END,
                            COALESCE(queued_at, created_at), item_id""").fetchall()
        return [self._row(r) for r in rows]

    def recent(self, limit: int = 25) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM items WHERE status IN ('done','failed') ORDER BY updated_at DESC LIMIT ?",
                (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT status, COUNT(*) FROM items GROUP BY status").fetchall()
        out = {s: 0 for s in STATUSES}
        out.update({r[0]: r[1] for r in rows})
        return out

    # ---- preferences -------------------------------------------------------------
    def get_pref(self, key: str, default: Any = None) -> Any:
        with self._lock:
            r = self._conn.execute("SELECT value FROM prefs WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def set_pref(self, key: str, value: Any) -> None:
        self._exec(
            "INSERT INTO prefs (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    def disabled_sources(self) -> set[str]:
        """Release source labels (e.g. "myanonamouse") hidden from search results, lower-cased."""
        return set(self.get_pref("disabled_sources", []))

    def set_source_enabled(self, label: str, enabled: bool) -> set[str]:
        disabled = self.disabled_sources()
        (disabled.discard if enabled else disabled.add)(source_key(label))
        self.set_pref("disabled_sources", sorted(disabled))
        return disabled

    # ---- queue order -------------------------------------------------------------
    def reorder_queue(self, item_ids: list[str]) -> None:
        """Put these queued books in this order by handing their existing queued_at values back out
        sorted. Books not listed keep their position; nothing outside the group moves."""
        if not item_ids:
            return
        marks = ",".join("?" * len(item_ids))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT item_id, COALESCE(queued_at, created_at) FROM items "
                f"WHERE status='queued' AND item_id IN ({marks})", item_ids).fetchall()
            present = {r[0] for r in rows}
            times = sorted(r[1] for r in rows)
            ordered = [i for i in dict.fromkeys(item_ids) if i in present]
            # Equal timestamps would make the order ambiguous; nudge duplicates apart.
            for n in range(1, len(times)):
                if times[n] <= times[n - 1]:
                    times[n] = times[n - 1] + 1e-6
            self._conn.executemany("UPDATE items SET queued_at=? WHERE item_id=?", zip(times, ordered))
            self._conn.commit()

    # ---- saved search results (pre-search / reuse across restarts) ----------------
    def save_search(self, item_id: str, query: str, data: dict[str, Any], complete: bool,
                    max_age: float | None = None) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO search_cache (item_id, query, created_at, complete, data) VALUES (?,?,?,?,?)
                   ON CONFLICT(item_id, query) DO UPDATE SET
                     created_at=excluded.created_at, complete=excluded.complete, data=excluded.data""",
                (item_id, query, now, int(complete), json.dumps(data)),
            )
            if max_age:
                self._conn.execute("DELETE FROM search_cache WHERE created_at < ?", (now - max_age,))
            self._conn.commit()

    def load_search(self, item_id: str, query: str, max_age: float) -> tuple[dict[str, Any], float] | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT data, created_at FROM search_cache WHERE item_id=? AND query=? AND created_at >= ?",
                (item_id, query, time.time() - max_age),
            ).fetchone()
        return (json.loads(r[0]), r[1]) if r else None

    def searched_ids(self, max_age: float, complete_only: bool = False) -> set[str]:
        """Items with saved search results newer than ``max_age`` seconds."""
        sql = "SELECT DISTINCT item_id FROM search_cache WHERE created_at >= ?"
        if complete_only:
            sql += " AND complete=1"
        with self._lock:
            rows = self._conn.execute(sql, (time.time() - max_age,)).fetchall()
        return {r[0] for r in rows}

    # ---- writes ------------------------------------------------------------------
    def update(self, item_id: str, **fields: Any) -> None:
        if not fields:
            return
        if fields.get("release") is not None:
            fields["release"] = json.dumps(fields["release"])  # None clears it (stored as NULL)
        if "status" in fields and fields["status"] not in STATUSES:
            raise ValueError(f"bad status {fields['status']}")
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE items SET {cols} WHERE item_id=?", [*fields.values(), item_id])

    def add_book(self, b: Book) -> None:
        """Insert a single book if unknown (used by `cli run --item`)."""
        now = time.time()
        self._exec(
            """INSERT INTO items (item_id, library_id, title, clean_title, author, series, path,
                                  status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?, 'missing', ?, ?)
               ON CONFLICT(item_id) DO NOTHING""",
            (b.item_id, b.library_id, b.title, b.clean_title, b.author, b.series, b.path, now, now),
        )
