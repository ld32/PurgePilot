"""SQLite persistence for ScanResult."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from .scanner import FileEntry, ScanResult

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_CREATE_FILES_TABLE = """
CREATE TABLE IF NOT EXISTS files (
    path        TEXT    NOT NULL,
    is_dir      INTEGER NOT NULL,
    size_bytes  INTEGER NOT NULL,
    modified_at TEXT    NOT NULL,
    accessed_at TEXT,
    depth       INTEGER NOT NULL
);
"""

_CREATE_META_TABLE = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def save_to_sqlite(scan_result: ScanResult, db_path: str | Path) -> None:
    """Persist *scan_result* to a SQLite database at *db_path*.

    Any existing data in the database is replaced.
    """
    db_path = Path(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        cur.executescript(_CREATE_FILES_TABLE + _CREATE_META_TABLE)
        cur.execute("DELETE FROM files")
        cur.execute("DELETE FROM meta")
        cur.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("root", scan_result.root),
        )
        cur.executemany(
            "INSERT INTO files (path, is_dir, size_bytes, modified_at, accessed_at, depth)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    entry.path,
                    int(entry.is_dir),
                    entry.size_bytes,
                    entry.modified_at.isoformat(),
                    entry.accessed_at.isoformat() if entry.accessed_at is not None else None,
                    entry.depth,
                )
                for entry in scan_result.entries
            ],
        )
        conn.commit()
    finally:
        conn.close()


def upsert_scan_result(
    scan_result: ScanResult,
    db_path: str | Path,
    *,
    remove_deleted: bool = True,
) -> None:
    """Incrementally update a SQLite database with *scan_result*.

    Compared to :func:`save_to_sqlite` this function:

    * **Skips** entries whose ``path``, ``size_bytes``, and ``modified_at``
      are identical to what is already stored.
    * **Updates** entries whose ``size_bytes`` or ``modified_at`` changed.
    * **Inserts** entries that are not yet in the database.
    * **Deletes** entries that are in the database but absent from the new
      scan when *remove_deleted* is ``True`` (the default).

    The ``meta.root`` value is always updated to ``scan_result.root``.
    """
    db_path = Path(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        cur.executescript(_CREATE_FILES_TABLE + _CREATE_META_TABLE)

        # Load existing rows: path -> (size_bytes, modified_at_iso)
        cur.execute("SELECT path, size_bytes, modified_at FROM files")
        existing: dict[str, tuple[int, str]] = {
            row[0]: (row[1], row[2]) for row in cur.fetchall()
        }

        new_paths: set[str] = set()
        to_insert: list[tuple] = []
        to_update: list[tuple] = []

        for entry in scan_result.entries:
            new_paths.add(entry.path)
            modified_iso = entry.modified_at.isoformat()
            accessed_iso = entry.accessed_at.isoformat() if entry.accessed_at is not None else None

            if entry.path not in existing:
                to_insert.append((
                    entry.path,
                    int(entry.is_dir),
                    entry.size_bytes,
                    modified_iso,
                    accessed_iso,
                    entry.depth,
                ))
            else:
                stored_size, stored_modified = existing[entry.path]
                if stored_size != entry.size_bytes or stored_modified != modified_iso:
                    to_update.append((
                        int(entry.is_dir),
                        entry.size_bytes,
                        modified_iso,
                        accessed_iso,
                        entry.depth,
                        entry.path,
                    ))
                # else: unchanged — skip

        if to_insert:
            cur.executemany(
                "INSERT INTO files (path, is_dir, size_bytes, modified_at, accessed_at, depth)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                to_insert,
            )

        if to_update:
            cur.executemany(
                "UPDATE files SET is_dir=?, size_bytes=?, modified_at=?, accessed_at=?, depth=?"
                " WHERE path=?",
                to_update,
            )

        if remove_deleted:
            deleted_paths = set(existing.keys()) - new_paths
            if deleted_paths:
                cur.executemany(
                    "DELETE FROM files WHERE path=?",
                    [(p,) for p in deleted_paths],
                )

        cur.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("root", scan_result.root),
        )
        conn.commit()
    finally:
        conn.close()


def load_from_sqlite(db_path: str | Path) -> ScanResult:
    """Load a :class:`~purge_pilot.scanner.ScanResult` from a SQLite database.

    The database must have been created by :func:`save_to_sqlite`.
    """
    db_path = Path(db_path)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = conn.cursor()
        cur.execute("SELECT value FROM meta WHERE key = 'root'")
        row = cur.fetchone()
        root = row[0] if row else ""

        cur.execute(
            "SELECT path, is_dir, size_bytes, modified_at, accessed_at, depth FROM files"
        )
        entries: list[FileEntry] = []
        for path, is_dir, size_bytes, modified_at, accessed_at, depth in cur.fetchall():
            entries.append(
                FileEntry(
                    path=path,
                    is_dir=bool(is_dir),
                    size_bytes=size_bytes,
                    modified_at=datetime.fromisoformat(modified_at),
                    accessed_at=datetime.fromisoformat(accessed_at) if accessed_at else None,
                    depth=depth,
                )
            )
        return ScanResult(root=root, entries=entries)
    finally:
        conn.close()
