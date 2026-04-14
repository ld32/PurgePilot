"""Tests for purge_pilot.store."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from purge_pilot.scanner import FileEntry, ScanResult
from purge_pilot.store import load_from_sqlite, save_to_sqlite


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
