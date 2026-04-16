"""Tests for purge_pilot.main (CLI)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from purge_pilot.llm_client import PurgeEstimate, PurgeReport
from purge_pilot.main import main
from purge_pilot.scanner import FileEntry, ScanResult
from purge_pilot.store import save_to_sqlite


def test_main_nonexistent_directory(tmp_path, capsys):
    rc = main(["scan", str(tmp_path / "does_not_exist")])
    assert rc == 1
    assert "not found" in capsys.readouterr().err.lower()


def test_main_path_is_file_not_dir(tmp_path, capsys):
    file_path = tmp_path / "file.txt"
    file_path.write_text("x", encoding="utf-8")

    rc = main(["scan", str(file_path)])
    assert rc == 1
    assert "not a directory" in capsys.readouterr().err.lower()


def test_main_scan_passes_max_depth(tmp_path):
    with patch("purge_pilot.main.scan_directory") as mock_scan:
        mock_scan.return_value = ScanResult(root=str(tmp_path), entries=[])
        main(["scan", str(tmp_path), "--max-depth", "3"])

    _, kwargs = mock_scan.call_args
    assert kwargs["max_depth"] == 3


def test_main_scan_passes_include_hidden(tmp_path):
    with patch("purge_pilot.main.scan_directory") as mock_scan:
        mock_scan.return_value = ScanResult(root=str(tmp_path), entries=[])
        main(["scan", str(tmp_path), "--include-hidden"])

    _, kwargs = mock_scan.call_args
    assert kwargs["include_hidden"] is True


def test_main_scan_passes_processes(tmp_path):
    with patch("purge_pilot.main.scan_directory") as mock_scan:
        mock_scan.return_value = ScanResult(root=str(tmp_path), entries=[])
        main(["scan", str(tmp_path), "--processes", "3"])

    _, kwargs = mock_scan.call_args
    assert kwargs["processes"] == 3


def test_scan_saves_default_db_when_save_db_omitted(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config_file = tmp_path / "config.md"
    config_file.write_text("## AI Prompt\n\n```\ntest\n```\n", encoding="utf-8")

    with patch("purge_pilot.main.scan_directory", return_value=ScanResult(root=str(tmp_path), entries=[])):
        rc = main(["scan", str(tmp_path), "--config", str(config_file)])

    assert rc == 0
    assert (tmp_path / "scan.db").exists()
    assert "scan database" in capsys.readouterr().err


def test_scan_multi_directory_saves_default_db_per_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config_file = tmp_path / "config.md"
    config_file.write_text("## AI Prompt\n\n```\ntest\n```\n", encoding="utf-8")

    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    def _scan_side_effect(directory, **_kwargs):
        return ScanResult(root=str(directory), entries=[])

    with patch("purge_pilot.main.scan_directory", side_effect=_scan_side_effect):
        rc = main(["scan", str(first), str(second), "--config", str(config_file)])

    assert rc == 0
    assert (tmp_path / "first_scan.db").exists()
    assert (tmp_path / "second_scan.db").exists()


def test_scan_save_db_creates_sqlite_file(tmp_path, capsys):
    db_file = tmp_path / "scan.db"

    with patch("purge_pilot.main.scan_directory", return_value=ScanResult(root=str(tmp_path), entries=[])):
        rc = main(["scan", str(tmp_path), "--save-db", str(db_file)])

    assert rc == 0
    assert db_file.exists()
    assert "scan database" in capsys.readouterr().err


def test_scan_rejects_explicit_save_db_with_multiple_directories(tmp_path, capsys):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    with patch(
        "purge_pilot.main.scan_directory",
        side_effect=[ScanResult(root=str(first), entries=[]), ScanResult(root=str(second), entries=[])],
    ):
        rc = main(["scan", str(first), str(second), "--save-db", str(tmp_path / "all.db")])

    assert rc == 1
    assert "--save-db only supports a single directory" in capsys.readouterr().err


def test_sqlquery_success(tmp_path, capsys):
    db_file = tmp_path / "scan.db"
    save_to_sqlite(
        ScanResult(
            root=str(tmp_path),
            entries=[
                FileEntry(
                    path=".cache/pip",
                    is_dir=True,
                    size_bytes=0,
                    modified_at=datetime(2022, 1, 1, tzinfo=timezone.utc),
                    depth=1,
                )
            ],
        ),
        db_file,
    )

    report = PurgeReport(
        root=str(tmp_path),
        estimates=[PurgeEstimate(path=".cache/pip", confidence=0.9, reason="pip cache")],
    )

    with patch("purge_pilot.main.estimate_purge_confidence_sql", return_value=report):
        rc = main(["sqlquery", str(db_file), "--api-url", "http://localhost/v1", "--model", "llama3"])

    assert rc == 0
    assert ".cache/pip" in capsys.readouterr().out


def test_sqlquery_json_output(tmp_path, capsys):
    db_file = tmp_path / "scan.db"
    save_to_sqlite(ScanResult(root=str(tmp_path), entries=[]), db_file)

    report = PurgeReport(
        root=str(tmp_path),
        estimates=[PurgeEstimate(path="old.tar.gz", confidence=0.85, reason="archive")],
    )

    with patch("purge_pilot.main.estimate_purge_confidence_sql", return_value=report):
        rc = main([
            "sqlquery",
            str(db_file),
            "--api-url",
            "http://localhost/v1",
            "--model",
            "llama3",
            "--output",
            "json",
        ])

    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["root"] == str(tmp_path)
    assert len(data["estimates"]) == 1


def test_sqlquery_nonexistent_db(tmp_path, capsys):
    rc = main(["sqlquery", str(tmp_path / "missing.db"), "--api-url", "http://x/v1", "--model", "m"])
    assert rc == 1
    assert "not found" in capsys.readouterr().err.lower()


def test_sqlquery_llm_error(tmp_path, capsys):
    db_file = tmp_path / "scan.db"
    save_to_sqlite(ScanResult(root=str(tmp_path), entries=[]), db_file)

    with patch("purge_pilot.main.estimate_purge_confidence_sql", side_effect=RuntimeError("timeout")):
        rc = main(["sqlquery", str(db_file), "--api-url", "http://bad/v1", "--model", "x"])

    assert rc == 1
    assert "timeout" in capsys.readouterr().err.lower()


def test_sqlquery_saves_commands(tmp_path):
    db_file = tmp_path / "scan.db"
    commands_file = tmp_path / "review.sh"
    save_to_sqlite(
        ScanResult(
            root=str(tmp_path),
            entries=[
                FileEntry(
                    path="old.tar.gz",
                    is_dir=False,
                    size_bytes=100,
                    modified_at=datetime(2022, 1, 1, tzinfo=timezone.utc),
                    depth=0,
                )
            ],
        ),
        db_file,
    )

    report = PurgeReport(
        root=str(tmp_path),
        estimates=[PurgeEstimate(path="old.tar.gz", confidence=0.9, reason="archive")],
    )

    with patch("purge_pilot.main.estimate_purge_confidence_sql", return_value=report):
        rc = main([
            "sqlquery",
            str(db_file),
            "--api-url",
            "http://localhost/v1",
            "--model",
            "llama3",
            "--save-commands",
            str(commands_file),
        ])

    assert rc == 0
    assert commands_file.exists()
    assert "old.tar.gz" in commands_file.read_text(encoding="utf-8")
