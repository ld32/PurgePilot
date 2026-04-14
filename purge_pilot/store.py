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
