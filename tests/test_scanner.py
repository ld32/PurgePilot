"""Tests for purge_pilot.scanner."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from purge_pilot.scanner import FileEntry, ScanResult, scan_directory


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def simple_tree(tmp_path: Path) -> Path:
    """
    Creates the following structure under tmp_path:

        root/
          file_a.txt       (content: "hello")
          subdir/
            file_b.log     (content: "world")
            .hidden_file   (content: "secret")
          empty_dir/
    """
    (tmp_path / "file_a.txt").write_text("hello")
    subdir = tmp_path / "subdir"
    subdir.mkdir()
    (subdir / "file_b.log").write_text("world")
    (subdir / ".hidden_file").write_text("secret")
    (tmp_path / "empty_dir").mkdir()
    return tmp_path


# ---------------------------------------------------------------------------
# scan_directory tests
# ---------------------------------------------------------------------------


def test_scan_returns_scan_result(simple_tree):
    result = scan_directory(simple_tree)
    assert isinstance(result, ScanResult)
    assert result.root == str(simple_tree)


def test_scan_finds_files_and_dirs(simple_tree):
    result = scan_directory(simple_tree)
    paths = {e.path for e in result.entries}
    assert "file_a.txt" in paths
    assert "subdir" in paths
    assert os.path.join("subdir", "file_b.log") in paths
    assert "empty_dir" in paths


def test_scan_excludes_hidden_by_default(simple_tree):
    result = scan_directory(simple_tree)
    paths = {e.path for e in result.entries}
    hidden = os.path.join("subdir", ".hidden_file")
    assert hidden not in paths


def test_scan_includes_hidden_when_requested(simple_tree):
    result = scan_directory(simple_tree, include_hidden=True)
    paths = {e.path for e in result.entries}
    hidden = os.path.join("subdir", ".hidden_file")
    assert hidden in paths


def test_scan_file_size(simple_tree):
    result = scan_directory(simple_tree)
    file_a = next(e for e in result.entries if e.path == "file_a.txt")
    assert file_a.size_bytes == len("hello")
    assert not file_a.is_dir


def test_scan_directory_entry_is_dir(simple_tree):
    result = scan_directory(simple_tree)
    subdir = next(e for e in result.entries if e.path == "subdir")
    assert subdir.is_dir
    assert subdir.size_bytes == 0


def test_scan_depth_field(simple_tree):
    result = scan_directory(simple_tree)
    entry_map = {e.path: e for e in result.entries}
    assert entry_map["file_a.txt"].depth == 0
    assert entry_map["subdir"].depth == 0
    assert entry_map[os.path.join("subdir", "file_b.log")].depth == 1


def test_scan_max_depth_zero(simple_tree):
    result = scan_directory(simple_tree, max_depth=0)
    paths = {e.path for e in result.entries}
    # Only top-level entries; nothing inside subdir
    assert "file_a.txt" in paths
    assert "subdir" in paths
    assert os.path.join("subdir", "file_b.log") not in paths


def test_scan_total_size_bytes(simple_tree):
    result = scan_directory(simple_tree)
    expected = len("hello") + len("world")  # hidden file excluded
    assert result.total_size_bytes == expected


def test_scan_raises_for_non_directory(tmp_path):
    f = tmp_path / "afile.txt"
    f.write_text("x")
    with pytest.raises(NotADirectoryError):
        scan_directory(f)


def test_file_entry_to_dict(simple_tree):
    result = scan_directory(simple_tree)
    file_a = next(e for e in result.entries if e.path == "file_a.txt")
    d = file_a.to_dict()
    assert d["path"] == "file_a.txt"
    assert d["is_dir"] is False
    assert d["size_bytes"] == len("hello")
    assert "modified_at" in d
    assert d["depth"] == 0


def test_scan_result_to_dict(simple_tree):
    result = scan_directory(simple_tree)
    d = result.to_dict()
    assert d["root"] == str(simple_tree)
    assert d["entry_count"] == len(result.entries)
    assert isinstance(d["entries"], list)


def test_file_entry_from_dict_round_trip(simple_tree):
    result = scan_directory(simple_tree)
    file_a = next(e for e in result.entries if e.path == "file_a.txt")
    d = file_a.to_dict()
    recreated = FileEntry.from_dict(d)
    assert recreated.path == file_a.path
    assert recreated.is_dir == file_a.is_dir
    assert recreated.size_bytes == file_a.size_bytes
    assert recreated.depth == file_a.depth


def test_scan_result_from_dict_round_trip(simple_tree):
    result = scan_directory(simple_tree)
    d = result.to_dict()
    recreated = ScanResult.from_dict(d)
    assert recreated.root == result.root
    assert len(recreated.entries) == len(result.entries)
    assert recreated.total_size_bytes == result.total_size_bytes
