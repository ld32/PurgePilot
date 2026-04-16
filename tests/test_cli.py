"""Additional CLI tests for purge_pilot.main."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

from purge_pilot.llm_client import PurgeReport
from purge_pilot.main import main
from purge_pilot.scanner import FileEntry, ScanResult
from purge_pilot.store import save_to_sqlite


def test_sqlquery_uses_environment_defaults(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PURGE_PILOT_API_URL", "http://env-server/v1")
    monkeypatch.setenv("PURGE_PILOT_MODEL", "env-model")
    monkeypatch.setenv("PURGE_PILOT_API_KEY", "env-key")

    db_file = tmp_path / "scan.db"
    save_to_sqlite(
        ScanResult(
            root=str(tmp_path),
            entries=[
                FileEntry(
                    path="cache.tmp",
                    is_dir=False,
                    size_bytes=10,
                    modified_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
                    depth=0,
                )
            ],
        ),
        db_file,
    )

    report = PurgeReport(root=str(tmp_path), estimates=[])

    with patch("purge_pilot.main.estimate_purge_confidence_sql", return_value=report) as mock_estimate:
        rc = main(["sqlquery", str(db_file)])

    assert rc == 0
    _, kwargs = mock_estimate.call_args
    assert kwargs["api_url"] == "http://env-server/v1"
    assert kwargs["model"] == "env-model"
    assert kwargs["api_key"] == "env-key"
    assert "Purge confidence report for:" in capsys.readouterr().out


def test_main_processes_multiple_directories_independently(tmp_path, capsys):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    scan_results = [
        ScanResult(root=str(first), entries=[]),
        ScanResult(root=str(second), entries=[]),
    ]
    with patch("purge_pilot.main.scan_directory", side_effect=scan_results) as mock_scan:
        rc = main(["scan", str(first), str(second)])

    assert rc == 0
    assert mock_scan.call_count == 2
    output = capsys.readouterr().out
    assert f"Scanned {first}" in output
    assert f"Scanned {second}" in output
