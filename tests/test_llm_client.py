"""Tests for purge_pilot.llm_client."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from purge_pilot.llm_client import (
    PurgeEstimate,
    PurgeReport,
    _parse_estimates,
    estimate_purge_confidence,
)
from purge_pilot.scanner import FileEntry, ScanResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_scan_result(root: str = "/data", entries=None) -> ScanResult:
    if entries is None:
        entries = [
            FileEntry(
                path="old_backup.tar.gz",
                is_dir=False,
                size_bytes=1024 * 1024 * 500,
                modified_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
                depth=0,
            ),
            FileEntry(
                path="important_data.csv",
                is_dir=False,
                size_bytes=1024,
                modified_at=datetime(2024, 6, 1, tzinfo=timezone.utc),
                depth=0,
            ),
        ]
    return ScanResult(root=root, entries=entries)


def _llm_json_response(estimates: list) -> dict:
    """Build a minimal OpenAI-like chat completion response."""
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(estimates),
                }
            }
        ]
    }


# ---------------------------------------------------------------------------
# _parse_estimates tests
# ---------------------------------------------------------------------------


def test_parse_estimates_valid():
    raw = json.dumps([
        {"path": "foo.txt", "confidence": 0.9, "reason": "Old temp file"},
        {"path": "bar.csv", "confidence": 0.1, "reason": "Active dataset"},
    ])
    estimates = _parse_estimates(raw)
    assert len(estimates) == 2
    assert estimates[0].path == "foo.txt"
    assert estimates[0].confidence == 0.9
    assert estimates[1].confidence == 0.1


def test_parse_estimates_clamps_confidence():
    raw = json.dumps([
        {"path": "a", "confidence": 1.5, "reason": "over"},
        {"path": "b", "confidence": -0.3, "reason": "under"},
    ])
    estimates = _parse_estimates(raw)
    assert estimates[0].confidence == 1.0
    assert estimates[1].confidence == 0.0


def test_parse_estimates_strips_code_fences():
    inner = json.dumps([{"path": "x", "confidence": 0.5, "reason": "maybe"}])
    raw = f"```json\n{inner}\n```"
    estimates = _parse_estimates(raw)
    assert len(estimates) == 1
    assert estimates[0].path == "x"


def test_parse_estimates_extracts_array_from_wrapped_text():
    inner = json.dumps([{"path": "x", "confidence": 0.5, "reason": "maybe"}])
    raw = f"Here is the result you asked for:\n{inner}\nThanks."
    estimates = _parse_estimates(raw)
    assert len(estimates) == 1
    assert estimates[0].path == "x"


def test_parse_estimates_skips_unknown_paths():
    raw = json.dumps([
        {"path": "known.txt", "confidence": 0.8, "reason": "old"},
        {"path": "/invented/path", "confidence": 1.0, "reason": "fake"},
    ])
    estimates = _parse_estimates(raw, allowed_paths={"known.txt"})
    assert len(estimates) == 1
    assert estimates[0].path == "known.txt"


def test_parse_estimates_raises_on_non_json():
    with pytest.raises(ValueError, match="non-JSON"):
        _parse_estimates("This is not JSON at all.")


def test_parse_estimates_raises_on_non_array():
    with pytest.raises(ValueError, match="Expected a JSON array"):
        _parse_estimates(json.dumps({"path": "x", "confidence": 0.5}))


def test_parse_estimates_skips_malformed_items(caplog):
    import logging
    raw = json.dumps([
        {"path": "good.txt", "confidence": 0.8, "reason": "ok"},
        {"no_path_key": True},  # malformed
    ])
    with caplog.at_level(logging.WARNING, logger="purge_pilot.llm_client"):
        estimates = _parse_estimates(raw)
    assert len(estimates) == 1
    assert estimates[0].path == "good.txt"


# ---------------------------------------------------------------------------
# estimate_purge_confidence tests
# ---------------------------------------------------------------------------


def test_estimate_purge_confidence_success():
    scan = _make_scan_result()
    llm_response_body = _llm_json_response([
        {"path": "old_backup.tar.gz", "confidence": 0.95, "reason": "Old backup"},
        {"path": "important_data.csv", "confidence": 0.05, "reason": "Active data"},
    ])

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = llm_response_body
    mock_response.raise_for_status = MagicMock()

    with patch("purge_pilot.llm_client.requests.post", return_value=mock_response) as mock_post:
        report = estimate_purge_confidence(
            scan,
            api_url="http://localhost:11434/v1",
            model="llama3",
        )

    mock_post.assert_called_once()
    call_kwargs = mock_post.call_args
    # Verify endpoint
    assert call_kwargs[0][0].endswith("/chat/completions")
    # Verify model
    assert call_kwargs[1]["json"]["model"] == "llama3"

    assert report.root == "/data"
    assert len(report.estimates) == 2


def test_estimate_purge_confidence_sends_api_key():
    scan = _make_scan_result()
    llm_response_body = _llm_json_response([
        {"path": "old_backup.tar.gz", "confidence": 0.8, "reason": "Old"},
    ])

    mock_response = MagicMock()
    mock_response.json.return_value = llm_response_body
    mock_response.raise_for_status = MagicMock()

    with patch("purge_pilot.llm_client.requests.post", return_value=mock_response) as mock_post:
        estimate_purge_confidence(
            scan,
            api_url="https://api.openai.com/v1",
            model="gpt-4o",
            api_key="sk-test-key",
        )

    headers = mock_post.call_args[1]["headers"]
    assert headers.get("Authorization") == "Bearer sk-test-key"


def test_estimate_purge_confidence_no_api_key():
    scan = _make_scan_result()
    llm_response_body = _llm_json_response([])

    mock_response = MagicMock()
    mock_response.json.return_value = llm_response_body
    mock_response.raise_for_status = MagicMock()

    with patch("purge_pilot.llm_client.requests.post", return_value=mock_response) as mock_post:
        estimate_purge_confidence(
            scan,
            api_url="http://localhost:11434/v1",
            model="llama3",
            api_key=None,
        )

    headers = mock_post.call_args[1]["headers"]
    assert "Authorization" not in headers


def test_estimate_purge_confidence_http_error():
    scan = _make_scan_result()

    mock_response = MagicMock()
    mock_response.raise_for_status.side_effect = Exception("HTTP 500")

    with patch("purge_pilot.llm_client.requests.post", return_value=mock_response):
        with pytest.raises(Exception, match="HTTP 500"):
            estimate_purge_confidence(
                scan,
                api_url="http://localhost:11434/v1",
                model="llama3",
            )


def test_estimate_purge_confidence_repairs_malformed_response():
    scan = _make_scan_result()

    bad_response = MagicMock()
    bad_response.json.return_value = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "This data looks like cache files. Use Python to filter it.",
                }
            }
        ]
    }
    bad_response.raise_for_status = MagicMock()

    repaired_response = MagicMock()
    repaired_response.json.return_value = _llm_json_response([
        {"path": "old_backup.tar.gz", "confidence": 0.95, "reason": "Old backup"},
        {"path": "important_data.csv", "confidence": 0.05, "reason": "Active data"},
    ])
    repaired_response.raise_for_status = MagicMock()

    with patch(
        "purge_pilot.llm_client.requests.post",
        side_effect=[bad_response, repaired_response],
    ) as mock_post:
        report = estimate_purge_confidence(
            scan,
            api_url="http://localhost:11434/v1",
            model="llama3",
        )

    assert mock_post.call_count == 2
    assert len(report.estimates) == 2
    assert report.estimates[0].path == "old_backup.tar.gz"


def test_estimate_purge_confidence_discards_invented_repaired_paths():
    scan = _make_scan_result()

    bad_response = MagicMock()
    bad_response.json.return_value = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "Here is some Python code instead of JSON.",
                }
            }
        ]
    }
    bad_response.raise_for_status = MagicMock()

    repaired_response = MagicMock()
    repaired_response.json.return_value = _llm_json_response([
        {"path": "/some_directory", "confidence": 1.0, "reason": "invented"},
        {"path": "old_backup.tar.gz", "confidence": 0.9, "reason": "Old backup"},
    ])
    repaired_response.raise_for_status = MagicMock()

    with patch(
        "purge_pilot.llm_client.requests.post",
        side_effect=[bad_response, repaired_response],
    ):
        report = estimate_purge_confidence(
            scan,
            api_url="http://localhost:11434/v1",
            model="llama3",
        )

    assert len(report.estimates) == 1
    assert report.estimates[0].path == "old_backup.tar.gz"


# ---------------------------------------------------------------------------
# PurgeReport tests
# ---------------------------------------------------------------------------


def test_purge_report_high_confidence():
    report = PurgeReport(
        root="/data",
        estimates=[
            PurgeEstimate(path="a", confidence=0.9, reason="old"),
            PurgeEstimate(path="b", confidence=0.5, reason="maybe"),
            PurgeEstimate(path="c", confidence=0.3, reason="keep"),
        ],
    )
    high = report.high_confidence(threshold=0.7)
    assert len(high) == 1
    assert high[0].path == "a"


def test_purge_report_to_dict():
    report = PurgeReport(
        root="/data",
        estimates=[
            PurgeEstimate(path="x.log", confidence=0.8, reason="log file"),
        ],
    )
    d = report.to_dict()
    assert d["root"] == "/data"
    assert len(d["estimates"]) == 1
    assert d["estimates"][0]["confidence"] == 0.8
