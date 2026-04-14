"""Tests for purge_pilot.store."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from purge_pilot.scanner import FileEntry, ScanResult
from purge_pilot.store import load_from_sqlite, save_to_sqlite, upsert_scan_result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_scan_result(root: str = "/home/user", entries=None) -> ScanResult:
    if entries is None:
        entries = [
            FileEntry(
                path="cache/pip",
                is_dir=True,
                size_bytes=0,
                modified_at=datetime(2023, 6, 1, tzinfo=timezone.utc),
                accessed_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
                depth=1,
            ),
            FileEntry(
                path="project/main.py",
                is_dir=False,
                size_bytes=2048,
                modified_at=datetime(2024, 3, 15, tzinfo=timezone.utc),
                accessed_at=None,
                depth=2,
            ),
        ]
    return ScanResult(root=root, entries=entries)


# ---------------------------------------------------------------------------
# save_to_sqlite tests
# ---------------------------------------------------------------------------


def test_save_creates_db_file(tmp_path):
    db = tmp_path / "scan.db"
    save_to_sqlite(_make_scan_result(), db)
    assert db.exists()


def test_save_writes_correct_row_count(tmp_path):
    db = tmp_path / "scan.db"
    save_to_sqlite(_make_scan_result(), db)
    conn = sqlite3.connect(str(db))
    count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    conn.close()
    assert count == 2


def test_save_stores_root_in_meta(tmp_path):
    db = tmp_path / "scan.db"
    save_to_sqlite(_make_scan_result(root="/my/root"), db)
    conn = sqlite3.connect(str(db))
    row = conn.execute("SELECT value FROM meta WHERE key = 'root'").fetchone()
    conn.close()
    assert row[0] == "/my/root"


def test_save_overwrites_previous_data(tmp_path):
    db = tmp_path / "scan.db"
    # Save once with 2 entries
    save_to_sqlite(_make_scan_result(), db)
    # Save again with 1 entry
    single = ScanResult(
        root="/new/root",
        entries=[
            FileEntry(
                path="only.txt",
                is_dir=False,
                size_bytes=10,
                modified_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
                depth=0,
            )
        ],
    )
    save_to_sqlite(single, db)

    conn = sqlite3.connect(str(db))
    count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    root = conn.execute("SELECT value FROM meta WHERE key = 'root'").fetchone()[0]
    conn.close()
    assert count == 1
    assert root == "/new/root"


def test_save_stores_accessed_at_null_when_absent(tmp_path):
    db = tmp_path / "scan.db"
    entry = FileEntry(
        path="file.txt",
        is_dir=False,
        size_bytes=1,
        modified_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        accessed_at=None,
        depth=0,
    )
    save_to_sqlite(ScanResult(root="/r", entries=[entry]), db)

    conn = sqlite3.connect(str(db))
    accessed_at = conn.execute("SELECT accessed_at FROM files").fetchone()[0]
    conn.close()
    assert accessed_at is None


# ---------------------------------------------------------------------------
# load_from_sqlite tests
# ---------------------------------------------------------------------------


def test_roundtrip_preserves_root(tmp_path):
    db = tmp_path / "scan.db"
    original = _make_scan_result(root="/data/user42")
    save_to_sqlite(original, db)
    loaded = load_from_sqlite(db)
    assert loaded.root == "/data/user42"


def test_roundtrip_preserves_entry_count(tmp_path):
    db = tmp_path / "scan.db"
    original = _make_scan_result()
    save_to_sqlite(original, db)
    loaded = load_from_sqlite(db)
    assert len(loaded.entries) == len(original.entries)


def test_roundtrip_preserves_fields(tmp_path):
    db = tmp_path / "scan.db"
    original = _make_scan_result()
    save_to_sqlite(original, db)
    loaded = load_from_sqlite(db)

    by_path = {e.path: e for e in loaded.entries}

    pip_entry = by_path["cache/pip"]
    assert pip_entry.is_dir is True
    assert pip_entry.size_bytes == 0
    assert pip_entry.depth == 1
    assert pip_entry.accessed_at is not None

    py_entry = by_path["project/main.py"]
    assert py_entry.is_dir is False
    assert py_entry.size_bytes == 2048
    assert py_entry.accessed_at is None


def test_roundtrip_preserves_timestamps(tmp_path):
    db = tmp_path / "scan.db"
    dt = datetime(2023, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    entry = FileEntry(
        path="f.txt",
        is_dir=False,
        size_bytes=0,
        modified_at=dt,
        depth=0,
    )
    save_to_sqlite(ScanResult(root="/r", entries=[entry]), db)
    loaded = load_from_sqlite(db)
    assert loaded.entries[0].modified_at == dt


def test_load_empty_scan(tmp_path):
    db = tmp_path / "scan.db"
    save_to_sqlite(ScanResult(root="/empty", entries=[]), db)
    loaded = load_from_sqlite(db)
    assert loaded.root == "/empty"
    assert loaded.entries == []


def test_load_nonexistent_file_raises(tmp_path):
    with pytest.raises(Exception):
        load_from_sqlite(tmp_path / "does_not_exist.db")


def test_load_is_readonly(tmp_path):
    """load_from_sqlite should open the DB read-only; writes must fail."""
    db = tmp_path / "scan.db"
    save_to_sqlite(_make_scan_result(), db)

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO files VALUES ('x', 0, 0, '2024-01-01', NULL, 0)")
    conn.close()


# ---------------------------------------------------------------------------
# upsert_scan_result tests
# ---------------------------------------------------------------------------

def _entry(path: str, size: int = 100, ts: datetime | None = None, is_dir: bool = False, depth: int = 0) -> FileEntry:
    return FileEntry(
        path=path,
        is_dir=is_dir,
        size_bytes=size,
        modified_at=ts or datetime(2024, 1, 1, tzinfo=timezone.utc),
        accessed_at=None,
        depth=depth,
    )


def test_upsert_creates_db_on_first_call(tmp_path):
    db = tmp_path / "scan.db"
    upsert_scan_result(ScanResult(root="/r", entries=[_entry("a.txt")]), db)
    assert db.exists()


def test_upsert_skips_unchanged_entry(tmp_path):
    db = tmp_path / "scan.db"
    entry = _entry("a.txt", size=512)
    save_to_sqlite(ScanResult(root="/r", entries=[entry]), db)

    # Re-run upsert with the identical entry
    upsert_scan_result(ScanResult(root="/r", entries=[entry]), db)

    conn = sqlite3.connect(str(db))
    rows = conn.execute("SELECT size_bytes FROM files WHERE path='a.txt'").fetchall()
    conn.close()
    # Still exactly one row with the original size
    assert len(rows) == 1
    assert rows[0][0] == 512


def test_upsert_updates_changed_entry(tmp_path):
    db = tmp_path / "scan.db"
    original = _entry("b.txt", size=100, ts=datetime(2024, 1, 1, tzinfo=timezone.utc))
    save_to_sqlite(ScanResult(root="/r", entries=[original]), db)

    updated = _entry("b.txt", size=999, ts=datetime(2024, 6, 1, tzinfo=timezone.utc))
    upsert_scan_result(ScanResult(root="/r", entries=[updated]), db)

    conn = sqlite3.connect(str(db))
    row = conn.execute("SELECT size_bytes, modified_at FROM files WHERE path='b.txt'").fetchone()
    conn.close()
    assert row[0] == 999
    assert "2024-06-01" in row[1]


def test_upsert_inserts_new_entry(tmp_path):
    db = tmp_path / "scan.db"
    save_to_sqlite(ScanResult(root="/r", entries=[_entry("old.txt")]), db)

    upsert_scan_result(ScanResult(root="/r", entries=[_entry("old.txt"), _entry("new.txt")]), db)

    conn = sqlite3.connect(str(db))
    count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    conn.close()
    assert count == 2


def test_upsert_removes_deleted_entry_by_default(tmp_path):
    db = tmp_path / "scan.db"
    save_to_sqlite(ScanResult(root="/r", entries=[_entry("gone.txt"), _entry("stay.txt")]), db)

    # Re-scan without "gone.txt"
    upsert_scan_result(ScanResult(root="/r", entries=[_entry("stay.txt")]), db)

    conn = sqlite3.connect(str(db))
    paths = {r[0] for r in conn.execute("SELECT path FROM files").fetchall()}
    conn.close()
    assert paths == {"stay.txt"}


def test_upsert_keeps_deleted_entry_when_remove_deleted_false(tmp_path):
    db = tmp_path / "scan.db"
    save_to_sqlite(ScanResult(root="/r", entries=[_entry("keep.txt"), _entry("also.txt")]), db)

    # Re-scan without "keep.txt", but don't remove deleted
    upsert_scan_result(
        ScanResult(root="/r", entries=[_entry("also.txt")]),
        db,
        remove_deleted=False,
    )

    conn = sqlite3.connect(str(db))
    count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    conn.close()
    assert count == 2


def test_upsert_updates_meta_root(tmp_path):
    db = tmp_path / "scan.db"
    save_to_sqlite(ScanResult(root="/old/root", entries=[]), db)
    upsert_scan_result(ScanResult(root="/new/root", entries=[]), db)

    conn = sqlite3.connect(str(db))
    root = conn.execute("SELECT value FROM meta WHERE key='root'").fetchone()[0]
    conn.close()
    assert root == "/new/root"
