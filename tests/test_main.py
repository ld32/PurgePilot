"""Tests for purge_pilot.main (CLI)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from purge_pilot.llm_client import PurgeEstimate, PurgeReport
from purge_pilot.main import main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_report(root: str, estimates=None) -> PurgeReport:
    if estimates is None:
        estimates = [
            PurgeEstimate(path="old.tar.gz", confidence=0.95, reason="Old archive"),
            PurgeEstimate(path="data.csv", confidence=0.1, reason="Active dataset"),
        ]
    return PurgeReport(root=root, estimates=estimates)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_main_text_output(tmp_path, capsys):
    report = _mock_report(str(tmp_path))
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
    with (
        patch("purge_pilot.main.estimate_purge_confidence", return_value=report),
    ):
        rc = main(["query", str(scan_file), "--api-url", "http://localhost:11434/v1", "--model", "llama3"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "old.tar.gz" in out
    assert "0.95" in out
    assert "Old archive" in out


def test_main_json_output(tmp_path, capsys):
    report = _mock_report(str(tmp_path))
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
    with (
        patch("purge_pilot.main.estimate_purge_confidence", return_value=report),
    ):
        rc = main(["query", str(scan_file), "--output", "json", "--api-url", "http://x/v1", "--model", "m"])

    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["root"] == str(tmp_path)
    assert len(data["estimates"]) == 2


def test_main_nonexistent_directory(tmp_path, capsys):
    rc = main(["scan", str(tmp_path / "does_not_exist")])
    assert rc == 1
    err = capsys.readouterr().err
    assert "not found" in err.lower()


def test_main_path_is_file_not_dir(tmp_path, capsys):
    f = tmp_path / "file.txt"
    f.write_text("x")
    rc = main(["scan", str(f)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "not a directory" in err.lower()


def test_main_llm_error_returns_nonzero(tmp_path, capsys):
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
    with (
        patch(
            "purge_pilot.main.estimate_purge_confidence",
            side_effect=RuntimeError("connection refused"),
        ),
    ):
        rc = main(["query", str(scan_file), "--api-url", "http://bad/v1", "--model", "x"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "connection refused" in err.lower()


def test_main_scan_passes_max_depth(tmp_path):
    with patch("purge_pilot.main.scan_directory") as mock_scan:
        mock_scan.return_value = MagicMock(entries=[], total_size_bytes=0)
        main(["scan", str(tmp_path), "--max-depth", "3"])

    _, kwargs = mock_scan.call_args
    assert kwargs["max_depth"] == 3


def test_main_scan_passes_include_hidden(tmp_path):
    with patch("purge_pilot.main.scan_directory") as mock_scan:
        mock_scan.return_value = MagicMock(entries=[], total_size_bytes=0)
        main(["scan", str(tmp_path), "--include-hidden"])

    _, kwargs = mock_scan.call_args
    assert kwargs["include_hidden"] is True


def test_main_scan_only_saves_scan_file(tmp_path, capsys):
    scan_json = tmp_path / "scan.json"
    mocked_scan = MagicMock(entries=[], total_size_bytes=0)
    mocked_scan.to_dict.return_value = {
        "root": str(tmp_path),
        "total_size_bytes": 0,
        "entry_count": 0,
        "entries": [],
    }

    with patch("purge_pilot.main.scan_directory", return_value=mocked_scan):
        rc = main([str(tmp_path), "--scan-only", "--save-scan", str(scan_json), "--output", "json"])

    assert rc == 0
    assert scan_json.exists()
    data = json.loads(scan_json.read_text())
    assert data["root"] == str(tmp_path)
    assert data["entries"] == []
    assert "Saved scan JSON" in capsys.readouterr().err


def test_main_rejects_save_commands_with_scan_only(tmp_path, capsys):
    commands_file = tmp_path / "review.sh"

    rc = main([str(tmp_path), "--scan-only", "--save-commands", str(commands_file)])

    assert rc == 1
    assert "cannot be combined with --scan-only" in capsys.readouterr().err


def test_main_query_from_scan_file(tmp_path, capsys):
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

    report = _mock_report(str(tmp_path), estimates=[
        PurgeEstimate(path="old.tar.gz", confidence=0.95, reason="Old archive")
    ])

    with patch("purge_pilot.main.estimate_purge_confidence", return_value=report):
        rc = main(["--from-scan", str(scan_file), "--api-url", "http://localhost:11434/v1", "--model", "llama3"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "old.tar.gz" in out


def test_main_rejects_dirs_with_from_scan(tmp_path, capsys):
    scan_file = tmp_path / "scan.json"
    scan_file.write_text(json.dumps({"root": str(tmp_path), "entries": []}))
    rc = main([str(tmp_path), "--from-scan", str(scan_file)])
    assert rc == 1
    assert "cannot be used with --from-scan" in capsys.readouterr().err


def test_main_rejects_implicit_combined_mode(tmp_path, capsys):
    rc = main([str(tmp_path)])
    assert rc == 1
    assert "Split workflow is the default" in capsys.readouterr().err


def test_main_scan_subcommand_saves_scan_file(tmp_path, capsys):
    scan_json = tmp_path / "scan-subcommand.json"
    mocked_scan = MagicMock(entries=[], total_size_bytes=0)
    mocked_scan.to_dict.return_value = {
        "root": str(tmp_path),
        "total_size_bytes": 0,
        "entry_count": 0,
        "entries": [],
    }

    with patch("purge_pilot.main.scan_directory", return_value=mocked_scan):
        rc = main(["scan", str(tmp_path), "--save-scan", str(scan_json), "--output", "json"])

    assert rc == 0
    assert scan_json.exists()
    assert "Saved scan JSON" in capsys.readouterr().err


def test_main_query_subcommand_uses_scan_file(tmp_path, capsys):
    scan_file = tmp_path / "scan-subcommand.json"
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

    report = _mock_report(str(tmp_path), estimates=[
        PurgeEstimate(path="old.tar.gz", confidence=0.95, reason="Old archive")
    ])

    with patch("purge_pilot.main.estimate_purge_confidence", return_value=report):
        rc = main(["query", str(scan_file), "--api-url", "http://localhost:11434/v1", "--model", "llama3"])

    assert rc == 0
    assert "old.tar.gz" in capsys.readouterr().out


def test_main_query_excludes_important_and_trash_from_ai_input(tmp_path, capsys):
    scan_file = tmp_path / "scan-rule-based.json"
    scan_file.write_text(
        json.dumps(
            {
                "root": str(tmp_path),
                "entries": [
                    {
                        "path": "README.md",
                        "is_dir": False,
                        "size_bytes": 10,
                        "modified_at": "2024-01-01T00:00:00+00:00",
                        "depth": 0,
                    },
                    {
                        "path": "build/output.bin",
                        "is_dir": False,
                        "size_bytes": 100,
                        "modified_at": "2024-01-01T00:00:00+00:00",
                        "depth": 1,
                    },
                    {
                        "path": "normal.txt",
                        "is_dir": False,
                        "size_bytes": 5,
                        "modified_at": "2024-01-01T00:00:00+00:00",
                        "depth": 0,
                    },
                    {
                        "path": "old_exports/data.zip",
                        "is_dir": False,
                        "size_bytes": 7,
                        "modified_at": "2024-01-01T00:00:00+00:00",
                        "depth": 1,
                    },
                ],
            }
        )
    )

    custom_config = tmp_path / "config.md"
    custom_config.write_text(
        """# PurgePilot Configuration

## AI Prompt

```
test prompt
```

## Important Data (Never Purge)

- README.md

## Trash Data (Always Delete)

- build/ (entire directory)

## Recycle Bin Data (Move to Recycle Bin)

- old_exports/

## Recycle Bin Path

- .purgepilot/recycle_bin
""",
        encoding="utf-8",
    )

    report = _mock_report(str(tmp_path), estimates=[
        PurgeEstimate(path="normal.txt", confidence=0.4, reason="from ai")
    ])

    with patch("purge_pilot.main.estimate_purge_confidence", return_value=report) as mock_estimate:
        rc = main([
            "query",
            str(scan_file),
            "--config",
            str(custom_config),
            "--api-url",
            "http://localhost:11434/v1",
            "--model",
            "llama3",
            "--output",
            "json",
        ])

    assert rc == 0

    scan_arg = mock_estimate.call_args[0][0]
    ai_paths = {entry.path for entry in scan_arg.entries}
    assert ai_paths == {"normal.txt"}

    data = json.loads(capsys.readouterr().out)
    by_path = {item["path"]: item for item in data["estimates"]}
    assert by_path["README.md"]["confidence"] == 0.0
    assert by_path["build/output.bin"]["confidence"] == 1.0
    assert by_path["old_exports/data.zip"]["confidence"] == 0.9
    assert "Move to recycle bin as per config" in by_path["old_exports/data.zip"]["reason"]
    assert ".purgepilot/recycle_bin" in by_path["old_exports/data.zip"]["reason"]


def test_main_query_saves_review_commands(tmp_path, capsys):
    scan_file = tmp_path / "scan-review.json"
    commands_file = tmp_path / "review.sh"
    scan_file.write_text(
        json.dumps(
            {
                "root": str(tmp_path),
                "entries": [
                    {
                        "path": "README.md",
                        "is_dir": False,
                        "size_bytes": 10,
                        "modified_at": "2024-01-01T00:00:00+00:00",
                        "depth": 0,
                    },
                    {
                        "path": "build/output.bin",
                        "is_dir": False,
                        "size_bytes": 100,
                        "modified_at": "2024-01-01T00:00:00+00:00",
                        "depth": 1,
                    },
                    {
                        "path": "old.tar.gz",
                        "is_dir": False,
                        "size_bytes": 123,
                        "modified_at": "2024-01-01T00:00:00+00:00",
                        "depth": 0,
                    },
                    {
                        "path": "old_exports/data.zip",
                        "is_dir": False,
                        "size_bytes": 7,
                        "modified_at": "2024-01-01T00:00:00+00:00",
                        "depth": 1,
                    },
                    {
                        "path": "normal.txt",
                        "is_dir": False,
                        "size_bytes": 5,
                        "modified_at": "2024-01-01T00:00:00+00:00",
                        "depth": 0,
                    }
                ],
            }
        )
    )

    custom_config = tmp_path / "config.md"
    custom_config.write_text(
        f"""# PurgePilot Configuration

## AI Prompt

```
test prompt
```

## Important Data (Never Purge)

- README.md

## Recycle Bin Data (Move to Recycle Bin)

- old_exports/

## Recycle Bin Path

- {tmp_path / 'recycle_bin'}

## Trash Data (Always Delete)

- build/ (entire directory)
""",
        encoding="utf-8",
    )

    report = _mock_report(
        str(tmp_path),
        estimates=[
            PurgeEstimate(path="old.tar.gz", confidence=0.95, reason="Old archive"),
            PurgeEstimate(path="normal.txt", confidence=0.4, reason="Keep it"),
        ],
    )

    with patch("purge_pilot.main.estimate_purge_confidence", return_value=report):
        rc = main([
            "query",
            str(scan_file),
            "--config",
            str(custom_config),
            "--save-commands",
            str(commands_file),
            "--api-url",
            "http://localhost:11434/v1",
            "--model",
            "llama3",
        ])

    assert rc == 0
    script = commands_file.read_text(encoding="utf-8")
    assert "Generated by PurgePilot; it does not execute automatically." in script
    assert "README.md" not in script
    assert "normal.txt" not in script
    assert "rm -f --" in script
    assert str(tmp_path / "build" / "output.bin") in script
    assert "mv -n --" in script
    assert str(tmp_path / "old.tar.gz") in script
    assert str(tmp_path / "recycle_bin" / "old.tar.gz") in script
    assert str(tmp_path / "recycle_bin" / "old_exports" / "data.zip") in script
    assert "Saved" in capsys.readouterr().err
