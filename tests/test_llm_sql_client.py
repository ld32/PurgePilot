"""Tests for purge_pilot.llm_sql_client."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from purge_pilot.llm_client import PurgeReport
from purge_pilot.llm_sql_client import (
    _execute_queries,
    _get_db_stats,
    _is_safe_select,
    _parse_categories,
    estimate_purge_confidence_sql,
)
from purge_pilot.scanner import FileEntry, ScanResult
from purge_pilot.store import save_to_sqlite


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path, entries=None, root: str = "/home/user") -> Path:
    db = tmp_path / "scan.db"
    if entries is None:
        entries = [
            FileEntry(
                path=".cache/pip/packages",
                is_dir=True,
                size_bytes=0,
                modified_at=datetime(2022, 1, 1, tzinfo=timezone.utc),
                depth=2,
            ),
            FileEntry(
                path="project/main.py",
                is_dir=False,
                size_bytes=1024,
                modified_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
                depth=2,
            ),
        ]
    save_to_sqlite(ScanResult(root=root, entries=entries), db)
    return db


def _llm_categories_response(categories: list) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(categories),
                }
            }
        ]
    }


# ---------------------------------------------------------------------------
# _is_safe_select tests
# ---------------------------------------------------------------------------


def test_is_safe_select_accepts_plain_select():
    assert _is_safe_select("SELECT path FROM files")


def test_is_safe_select_accepts_select_with_trailing_semicolon():
    assert _is_safe_select("SELECT path FROM files WHERE is_dir = 1;")


def test_is_safe_select_accepts_case_insensitive():
    assert _is_safe_select("select path from files")
    assert _is_safe_select("Select path FROM files")


def test_is_safe_select_rejects_insert():
    assert not _is_safe_select("INSERT INTO files VALUES ('x', 0, 0, '', NULL, 0)")


def test_is_safe_select_rejects_drop():
    assert not _is_safe_select("DROP TABLE files")


def test_is_safe_select_rejects_delete():
    assert not _is_safe_select("DELETE FROM files")


def test_is_safe_select_rejects_update():
    assert not _is_safe_select("UPDATE files SET path = 'x'")


# ---------------------------------------------------------------------------
# _get_db_stats tests
# ---------------------------------------------------------------------------


def test_get_db_stats_returns_root_and_count(tmp_path):
    db = _make_db(tmp_path, root="/my/root")
    root, row_count = _get_db_stats(db)
    assert root == "/my/root"
    assert row_count == 2


def test_get_db_stats_empty_db(tmp_path):
    db = tmp_path / "empty.db"
    save_to_sqlite(ScanResult(root="/empty", entries=[]), db)
    root, row_count = _get_db_stats(db)
    assert root == "/empty"
    assert row_count == 0


# ---------------------------------------------------------------------------
# _parse_categories tests
# ---------------------------------------------------------------------------


def test_parse_categories_valid():
    raw = json.dumps([
        {"category": "pip cache", "confidence": 0.9, "sql": "SELECT path FROM files", "reason": "cached"},
    ])
    categories = _parse_categories(raw)
    assert len(categories) == 1
    assert categories[0]["category"] == "pip cache"


def test_parse_categories_strips_code_fences():
    inner = json.dumps([{"category": "c", "confidence": 0.8, "sql": "SELECT path FROM files", "reason": "r"}])
    raw = f"```json\n{inner}\n```"
    categories = _parse_categories(raw)
    assert len(categories) == 1


def test_parse_categories_raises_on_non_json():
    with pytest.raises(ValueError, match="non-JSON"):
        _parse_categories("not json at all")


def test_parse_categories_raises_on_non_array():
    with pytest.raises(ValueError, match="Expected a JSON array"):
        _parse_categories(json.dumps({"category": "x"}))


# ---------------------------------------------------------------------------
# _execute_queries tests
# ---------------------------------------------------------------------------


def test_execute_queries_returns_matching_paths(tmp_path):
    db = _make_db(tmp_path)
    categories = [
        {
            "category": "pip cache",
            "confidence": 0.9,
            "sql": "SELECT path FROM files WHERE path LIKE '%.cache%'",
            "reason": "pip cache is disposable",
        }
    ]
    estimates = _execute_queries(db, categories)
    assert len(estimates) == 1
    assert estimates[0].path == ".cache/pip/packages"
    assert estimates[0].confidence == 0.9


def test_execute_queries_skips_non_select(tmp_path):
    db = _make_db(tmp_path)
    categories = [
        {"category": "bad", "confidence": 0.9, "sql": "DELETE FROM files", "reason": "r"},
        {"category": "ok", "confidence": 0.8, "sql": "SELECT path FROM files WHERE is_dir = 1", "reason": "dirs"},
    ]
    estimates = _execute_queries(db, categories)
    # Only the SELECT query should execute
    assert len(estimates) == 1
    assert estimates[0].path == ".cache/pip/packages"


def test_execute_queries_deduplicates_keeping_highest_confidence(tmp_path):
    db = _make_db(tmp_path)
    categories = [
        {
            "category": "cat1",
            "confidence": 0.6,
            "sql": "SELECT path FROM files WHERE path LIKE '%.cache%'",
            "reason": "low confidence",
        },
        {
            "category": "cat2",
            "confidence": 0.95,
            "sql": "SELECT path FROM files WHERE path LIKE '%.cache%'",
            "reason": "high confidence",
        },
    ]
    estimates = _execute_queries(db, categories)
    assert len(estimates) == 1
    assert estimates[0].confidence == 0.95
    assert estimates[0].reason == "high confidence"


def test_execute_queries_skips_missing_path_column(tmp_path):
    db = _make_db(tmp_path)
    categories = [
        {
            "category": "bad",
            "confidence": 0.9,
            "sql": "SELECT size_bytes FROM files",
            "reason": "no path column",
        }
    ]
    estimates = _execute_queries(db, categories)
    assert estimates == []


def test_execute_queries_handles_sql_error(tmp_path):
    db = _make_db(tmp_path)
    categories = [
        {
            "category": "bad_sql",
            "confidence": 0.9,
            "sql": "SELECT path FROM nonexistent_table",
            "reason": "bad table",
        }
    ]
    # Should not raise; just log and skip
    estimates = _execute_queries(db, categories)
    assert estimates == []


def test_execute_queries_skips_malformed_item(tmp_path):
    db = _make_db(tmp_path)
    categories = [
        {"no_category_key": True},
    ]
    estimates = _execute_queries(db, categories)
    assert estimates == []


def test_execute_queries_clamps_confidence(tmp_path):
    db = _make_db(tmp_path)
    categories = [
        {
            "category": "c",
            "confidence": 1.5,
            "sql": "SELECT path FROM files",
            "reason": "r",
        }
    ]
    estimates = _execute_queries(db, categories)
    for est in estimates:
        assert est.confidence <= 1.0


# ---------------------------------------------------------------------------
# estimate_purge_confidence_sql integration tests
# ---------------------------------------------------------------------------


def test_estimate_purge_confidence_sql_success(tmp_path):
    db = _make_db(tmp_path)
    llm_body = _llm_categories_response([
        {
            "category": "pip cache",
            "confidence": 0.9,
            "sql": "SELECT path FROM files WHERE path LIKE '%.cache%'",
            "reason": "pip cache is disposable",
        }
    ])

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = llm_body
    mock_response.raise_for_status = MagicMock()

    with patch("purge_pilot.llm_sql_client.requests.post", return_value=mock_response):
        report = estimate_purge_confidence_sql(
            db,
            api_url="http://localhost:11434/v1",
            model="llama3",
        )

    assert isinstance(report, PurgeReport)
    assert report.root == "/home/user"
    assert len(report.estimates) == 1
    assert report.estimates[0].path == ".cache/pip/packages"
    assert report.estimates[0].confidence == 0.9


def test_estimate_purge_confidence_sql_sends_api_key(tmp_path):
    db = _make_db(tmp_path)
    llm_body = _llm_categories_response([])

    mock_response = MagicMock()
    mock_response.json.return_value = llm_body
    mock_response.raise_for_status = MagicMock()

    with patch("purge_pilot.llm_sql_client.requests.post", return_value=mock_response) as mock_post:
        estimate_purge_confidence_sql(
            db,
            api_url="https://api.openai.com/v1",
            model="gpt-4o",
            api_key="sk-test",
        )

    headers = mock_post.call_args[1]["headers"]
    assert headers.get("Authorization") == "Bearer sk-test"


def test_estimate_purge_confidence_sql_repairs_malformed_response(tmp_path):
    db = _make_db(tmp_path)

    bad_response = MagicMock()
    bad_response.json.return_value = {
        "choices": [{"message": {"role": "assistant", "content": "Not JSON at all."}}]
    }
    bad_response.raise_for_status = MagicMock()

    repaired_response = MagicMock()
    repaired_response.json.return_value = _llm_categories_response([
        {
            "category": "cache",
            "confidence": 0.85,
            "sql": "SELECT path FROM files WHERE path LIKE '%.cache%'",
            "reason": "repaired",
        }
    ])
    repaired_response.raise_for_status = MagicMock()

    with patch(
        "purge_pilot.llm_sql_client.requests.post",
        side_effect=[bad_response, repaired_response],
    ) as mock_post:
        report = estimate_purge_confidence_sql(
            db,
            api_url="http://localhost:11434/v1",
            model="llama3",
        )

    assert mock_post.call_count == 2
    assert len(report.estimates) == 1


def test_estimate_purge_confidence_sql_empty_result(tmp_path):
    db = _make_db(tmp_path)
    llm_body = _llm_categories_response([])

    mock_response = MagicMock()
    mock_response.json.return_value = llm_body
    mock_response.raise_for_status = MagicMock()

    with patch("purge_pilot.llm_sql_client.requests.post", return_value=mock_response):
        report = estimate_purge_confidence_sql(
            db,
            api_url="http://localhost:11434/v1",
            model="llama3",
        )

    assert report.estimates == []
