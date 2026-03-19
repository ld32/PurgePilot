"""Additional CLI tests for purge_pilot.main."""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

from purge_pilot.llm_client import PurgeReport
from purge_pilot.main import main


def test_main_uses_environment_defaults(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PURGE_PILOT_API_URL", "http://env-server/v1")
    monkeypatch.setenv("PURGE_PILOT_MODEL", "env-model")
    monkeypatch.setenv("PURGE_PILOT_API_KEY", "env-key")

    scan_file = tmp_path / "scan.json"
    scan_file.write_text(
        json.dumps(
            {
                "root": str(tmp_path),
                "entries": [
                    {
                        "path": "old.tar.gz",
                        "is_dir": False,
                        "size_bytes": 123,
                        "modified_at": "2024-01-01T00:00:00+00:00",
                        "depth": 0,
                    }
                ],
            }
        )
    )

    report = PurgeReport(root=str(tmp_path), estimates=[])

    with (
        patch("purge_pilot.main.estimate_purge_confidence", return_value=report) as mock_estimate,
    ):
        rc = main(["query", str(scan_file)])

    assert rc == 0
    _, kwargs = mock_estimate.call_args
    assert kwargs["api_url"] == "http://env-server/v1"
    assert kwargs["model"] == "env-model"
    assert kwargs["api_key"] == "env-key"
    assert "(no estimates returned by LLM)" in capsys.readouterr().out


def test_main_processes_multiple_directories_independently(tmp_path, capsys):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    scan_results = [
        MagicMock(entries=[], total_size_bytes=0),
        MagicMock(entries=[], total_size_bytes=0),
    ]
    with patch("purge_pilot.main.scan_directory", side_effect=scan_results) as mock_scan:
        rc = main(["scan", str(first), str(second)])

    assert rc == 0
    assert mock_scan.call_count == 2
    output = capsys.readouterr().out
    assert "Scan summary for" in output
