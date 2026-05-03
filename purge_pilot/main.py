"""CLI entry point for PurgePilot."""

from __future__ import annotations

import argparse
import collections
import fnmatch
import json
import logging
import os
import re
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from .llm_client import PurgeEstimate, PurgeReport, estimate_purge_confidence, _SYSTEM_PROMPT
from .llm_sql_client import estimate_purge_confidence_sql
from .scanner import FileEntry, ScanResult, scan_directory
from .store import load_from_sqlite, save_to_sqlite, upsert_scan_result


PERMISSION_ERROR_ENTRIES_FILE = "permissionErrorEntries.txt"


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return parsed


def _default_scan_db_path(directory: Path, *, multiple_directories: bool) -> Path:
    """Return the default SQLite output path for scan results."""
    if not multiple_directories:
        return Path("scan.db")

    name = directory.resolve().name or "scan"
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "scan"
    return Path(f"{safe_name}_scan.db")


def parse_config(config_path: Path) -> dict:
    """Parse the markdown config file."""
    with open(config_path, encoding='utf-8') as f:
        content = f.read()

    config = {}
    # Find sections
    sections = re.split(r'^##\s+', content, flags=re.MULTILINE)
    for section in sections:
        lines = section.strip().split('\n')
        if not lines:
            continue
        title = lines[0].strip()
        body = '\n'.join(lines[1:]).strip()
        if title == 'AI Prompt':
            # Find code block
            match = re.search(r'```\s*\n(.*?)\n\s*```', body, re.DOTALL)
            if match:
                config['prompt'] = match.group(1).strip()
        elif 'Important Data' in title:
            items = [re.sub(r'^\s*-\s*', '', line).strip().strip('`') for line in body.split('\n') if re.match(r'^\s*-\s*', line)]
            config['important'] = items
        elif 'Trash Data' in title:
            items = [re.sub(r'^\s*-\s*', '', line).strip().strip('`') for line in body.split('\n') if re.match(r'^\s*-\s*', line)]
            config['trash'] = items
        elif 'Recycle Bin Data' in title:
            items = [re.sub(r'^\s*-\s*', '', line).strip().strip('`') for line in body.split('\n') if re.match(r'^\s*-\s*', line)]
            config['recycle_bin'] = items
        elif 'Recycle Bin Path' in title:
            path_value = next(
                (
                    re.sub(r'^\s*-\s*', '', line).strip().strip('`')
                    for line in body.split('\n')
                    if re.match(r'^\s*-\s*', line)
                ),
                '',
            )
            if path_value:
                config['recycle_bin_path'] = path_value
    return config


def _clean_config_pattern(pattern: str) -> str:
    cleaned = pattern.strip().strip("`")
    cleaned = re.sub(r"\s*\([^)]*\)\s*$", "", cleaned)
    cleaned = cleaned.strip()
    return cleaned


def _to_posix(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def _matches_config_pattern(path: str, pattern: str) -> bool:
    path_norm = _to_posix(path)
    pattern_norm = _to_posix(_clean_config_pattern(pattern))
    if not pattern_norm:
        return False

    is_dir_pattern = pattern_norm.endswith("/")
    base_pattern = pattern_norm.rstrip("/")

    if is_dir_pattern:
        if path_norm == base_pattern or path_norm.startswith(base_pattern + "/"):
            return True
        if "/" not in base_pattern and base_pattern in path_norm.split("/"):
            return True
        return False

    has_glob = any(char in base_pattern for char in "*?[]")
    if has_glob:
        return fnmatch.fnmatch(path_norm, base_pattern) or fnmatch.fnmatch(path_norm.split("/")[-1], base_pattern)

    if "/" in base_pattern:
        return path_norm == base_pattern

    return path_norm.split("/")[-1] == base_pattern


def _is_important_path(path: str, config: dict) -> bool:
    return any(_matches_config_pattern(path, pattern) for pattern in config.get("important", []))


def _is_trash_path(path: str, config: dict) -> bool:
    return any(_matches_config_pattern(path, pattern) for pattern in config.get("trash", []))


def _is_recycle_bin_path(path: str, config: dict) -> bool:
    return any(_matches_config_pattern(path, pattern) for pattern in config.get("recycle_bin", []))


def _filter_ai_scan_entries(scan_result: ScanResult, config: dict) -> ScanResult:
    filtered_entries = [
        entry
        for entry in scan_result.entries
        if not _is_important_path(entry.path, config)
        and not _is_trash_path(entry.path, config)
        and not _is_recycle_bin_path(entry.path, config)
    ]
    return ScanResult(root=scan_result.root, entries=filtered_entries)


def _build_directory_summary_scan(scan_result: ScanResult) -> ScanResult:
    """Collapse file-level scan entries into directory-level summary entries."""
    dir_entries = [entry for entry in scan_result.entries if entry.is_dir]
    if not dir_entries:
        return scan_result

    now = datetime.now(timezone.utc)
    by_path = {
        entry.path: {
            "entry": entry,
            "file_count": 0,
            "total_file_bytes": 0,
            "older_than_30_days": 0,
            "older_than_90_days": 0,
            "accessed_within_30_days": 0,
            "accessed_within_90_days": 0,
            "most_recent_access": None,
            "extensions": collections.Counter(),
        }
        for entry in dir_entries
    }

    for entry in scan_result.entries:
        if entry.is_dir:
            continue

        modified_at = entry.modified_at
        if modified_at.tzinfo is None:
            modified_at = modified_at.replace(tzinfo=timezone.utc)
        age_days = max(0, (now - modified_at).days)

        accessed_at = entry.accessed_at
        if accessed_at is not None and accessed_at.tzinfo is None:
            accessed_at = accessed_at.replace(tzinfo=timezone.utc)
        access_age_days = max(0, (now - accessed_at).days) if accessed_at is not None else None

        suffix = Path(entry.path).suffix.lower() or "<no_ext>"
        parts = Path(entry.path).parts
        for idx in range(1, len(parts)):
            parent = str(Path(*parts[:idx]))
            stats = by_path.get(parent)
            if stats is None:
                continue
            stats["file_count"] += 1
            stats["total_file_bytes"] += entry.size_bytes
            if age_days >= 30:
                stats["older_than_30_days"] += 1
            if age_days >= 90:
                stats["older_than_90_days"] += 1
            if access_age_days is not None:
                if access_age_days < 30:
                    stats["accessed_within_30_days"] += 1
                if access_age_days < 90:
                    stats["accessed_within_90_days"] += 1
                prev = stats["most_recent_access"]
                if prev is None or accessed_at > prev:
                    stats["most_recent_access"] = accessed_at
            stats["extensions"][suffix] += 1

    summary_entries = [] 
    for path, stats in by_path.items():
        ext_counter = stats["extensions"]
        top_extensions = [
            {"ext": ext, "count": count}
            for ext, count in sorted(ext_counter.items(), key=lambda item: (-item[1], item[0]))[:5]
        ]
        metadata = {
            "summary_type": "directory_stats",
            "file_count": stats["file_count"],
            "total_file_bytes": stats["total_file_bytes"],
            "older_than_30_days": stats["older_than_30_days"],
            "older_than_90_days": stats["older_than_90_days"],
            "accessed_within_30_days": stats["accessed_within_30_days"],
            "accessed_within_90_days": stats["accessed_within_90_days"],
            "top_extensions": top_extensions,
        }
        if stats["most_recent_access"] is not None:
            metadata["most_recently_accessed_days_ago"] = max(0, (now - stats["most_recent_access"]).days)
        if stats["file_count"] > 0 and len(ext_counter) == 1:
            metadata["all_files_extension"] = top_extensions[0]["ext"]

        summary_entries.append(
            FileEntry(
                path=path,
                is_dir=True,
                size_bytes=stats["total_file_bytes"],
                modified_at=stats["entry"].modified_at,
                accessed_at=stats["entry"].accessed_at,
                depth=stats["entry"].depth,
                metadata=metadata,
            )
        )

    # Keep root-level files as-is so files directly under the scan root are not dropped.
    root_level_files = [
        entry for entry in scan_result.entries if not entry.is_dir and "/" not in entry.path
    ]
    summary_entries.extend(root_level_files)

    summary_entries.sort(key=lambda entry: (entry.depth, entry.path))
    return ScanResult(root=scan_result.root, entries=summary_entries)


def _ensure_rule_based_entries_in_report(report, full_scan_result: ScanResult, config: dict) -> None:
    seen = {estimate.path for estimate in report.estimates}
    for estimate in report.estimates:
        if _is_important_path(estimate.path, config):
            estimate.confidence = 0.0
            estimate.reason = "Never purge as per config"
            continue
        if _is_trash_path(estimate.path, config):
            estimate.confidence = 1.0
            estimate.reason = "Always delete as per config"
            continue
        if _is_recycle_bin_path(estimate.path, config):
            estimate.confidence = 0.9
            recycle_bin_path = config.get("recycle_bin_path", ".purgepilot/recycle_bin")
            estimate.reason = f"Move to recycle bin as per config ({recycle_bin_path})"

    for entry in full_scan_result.entries:
        if entry.path in seen:
            continue
        if _is_important_path(entry.path, config):
            report.estimates.append(
                PurgeEstimate(
                    path=entry.path,
                    confidence=0.0,
                    reason="Never purge as per config",
                )
            )
            continue
        if _is_trash_path(entry.path, config):
            report.estimates.append(
                PurgeEstimate(
                    path=entry.path,
                    confidence=1.0,
                    reason="Always delete as per config",
                )
            )
            continue
        if _is_recycle_bin_path(entry.path, config):
            recycle_bin_path = config.get("recycle_bin_path", ".purgepilot/recycle_bin")
            report.estimates.append(
                PurgeEstimate(
                    path=entry.path,
                    confidence=0.9,
                    reason=f"Move to recycle bin as per config ({recycle_bin_path})",
                )
            )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="purgep",
        description=(
            "Scan a Linux home directory on an HPC cluster and use an LLM server "
            "to estimate how confidently each file or sub-folder can be purged, "
            "helping you reclaim home quota without deleting important files. "
            "Typical usage: purgep scan ~, "
            "then purgep sqlquery scan.db --api-url http://localhost:11434/v1 "
            "--model phi3:mini --save-commands review_purge.sh"
        ),
    )
    parser.add_argument(
        "directories",
        metavar="DIR",
        nargs="*",
        help="One or more directories to scan (e.g. ~ for your home directory).",
    )
    parser.add_argument(
        "--api-url",
        default=os.environ.get("PURGE_PILOT_API_URL", "http://localhost:11434/v1"),
        help=(
            "Base URL of an OpenAI-compatible API endpoint. "
            "Defaults to $PURGE_PILOT_API_URL or http://localhost:11434/v1."
        ),
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("PURGE_PILOT_MODEL", "llama3"),
        help="Model name to use (default: $PURGE_PILOT_MODEL or llama3).",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("PURGE_PILOT_API_KEY"),
        help="Bearer token for the LLM API (default: $PURGE_PILOT_API_KEY).",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.7,
        metavar="FLOAT",
        help="Confidence threshold for highlighting high-risk entries (default: 0.7).",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=10,
        metavar="INT",
        help="Maximum recursion depth when scanning directories (default: 10).",
    )
    parser.add_argument(
        "--processes",
        type=_positive_int,
        default=1,
        metavar="INT",
        help="Number of worker processes used while scanning (default: 1).",
    )
    parser.add_argument(
        "--include-hidden",
        action="store_true",
        help="Include hidden files and directories (names starting with '.').",
    )
    parser.add_argument(
        "--output",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        metavar="SECONDS",
        help="HTTP request timeout in seconds (default: 120).",
    )
    parser.add_argument(
        "--batch-size",
        type=_positive_int,
        default=50,
        metavar="INT",
        help="Number of entries per LLM request (default: 50). Reduce for models with small context windows.",
    )
    parser.add_argument(
        "--num-ctx",
        type=_positive_int,
        default=None,
        metavar="INT",
        help="Ollama num_ctx option: context window size in tokens (e.g. 8192). Uses model default if unset.",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        default=True,
        help="Enable verbose/debug logging (enabled by default).",
    )
    parser.add_argument(
        "--config",
        default="config.md",
        help="Path to the configuration markdown file (default: config.md).",
    )
    parser.add_argument(
        "--folders-only",
        action="store_true",
        help="Only scan and report directories, not files.",
    )
    parser.add_argument(
        "--save-commands",
        metavar="FILE",
        help="Write suggested review commands to a shell script instead of touching data.",
    )
    return parser


def _build_subcommand_parser() -> argparse.ArgumentParser:
    """Build a dedicated parser for explicit subcommands."""
    parser = argparse.ArgumentParser(
        prog="purgep",
        description="Run scan and query as separate steps.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser(
        "scan",
        help="Scan directories and optionally save scan JSON.",
    )
    scan_parser.add_argument(
        "directories",
        metavar="DIR",
        nargs="+",
        help="One or more directories to scan (e.g. ~ for your home directory).",
    )
    scan_parser.add_argument("--max-depth", type=int, default=10, metavar="INT")
    scan_parser.add_argument("--processes", type=_positive_int, default=1, metavar="INT")
    scan_parser.add_argument("--include-hidden", action="store_true")
    scan_parser.add_argument("--output", choices=["text", "json"], default="text")
    scan_parser.add_argument(
        "--save-db",
        metavar="FILE",
        default=None,
        help=(
            "Save scan data to a SQLite database file. "
            "Defaults to scan.db when omitted (or <dirname>_scan.db for multi-directory scans)."
        ),
    )
    scan_parser.add_argument(
        "--incremental",
        action="store_true",
        help=(
            "When saving to a database with --save-db, skip unchanged entries, "
            "update changed ones, insert new ones, and remove deleted ones "
            "instead of replacing all data."
        ),
    )
    scan_parser.add_argument("--save-commands", metavar="FILE")
    scan_parser.add_argument("--config", default="config.md")
    scan_parser.add_argument("--folders-only", action="store_true", help="Only scan and report directories, not files.")
    scan_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=True,
        help="Enable verbose/debug logging (enabled by default).",
    )

    sqlquery_parser = subparsers.add_parser(
        "sqlquery",
        help=(
            "Query the LLM using a SQLite database produced by 'purgep scan --save-db'. "
            "The LLM generates SQL SELECT queries instead of receiving the full file list, "
            "which drastically reduces token usage for large scans."
        ),
    )
    sqlquery_parser.add_argument(
        "db_file",
        metavar="DB",
        help="SQLite database file produced by 'purgep scan --save-db'.",
    )
    sqlquery_parser.add_argument(
        "--api-url",
        default=os.environ.get("PURGE_PILOT_API_URL", "http://localhost:11434/v1"),
    )
    sqlquery_parser.add_argument(
        "--model",
        default=os.environ.get("PURGE_PILOT_MODEL", "llama3"),
    )
    sqlquery_parser.add_argument(
        "--api-key",
        default=os.environ.get("PURGE_PILOT_API_KEY"),
    )
    sqlquery_parser.add_argument("--threshold", type=float, default=0.7, metavar="FLOAT")
    sqlquery_parser.add_argument("--output", choices=["text", "json"], default="text")
    sqlquery_parser.add_argument("--timeout", type=int, default=120, metavar="SECONDS")
    sqlquery_parser.add_argument("--num-ctx", type=_positive_int, default=None, metavar="INT")
    sqlquery_parser.add_argument("--save-commands", metavar="FILE")
    sqlquery_parser.add_argument("--config", default="config.md")
    sqlquery_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=True,
        help="Enable verbose/debug logging (enabled by default).",
    )

    return parser


def _apply_config_overrides(report, config: dict) -> None:
    for est in report.estimates:
        if _is_important_path(est.path, config):
            est.confidence = 0.0
            est.reason = "Never purge as per config"
        elif _is_trash_path(est.path, config):
            est.confidence = 1.0
            est.reason = "Always delete as per config"
        elif _is_recycle_bin_path(est.path, config):
            est.confidence = 0.9
            recycle_bin_path = config.get("recycle_bin_path", ".purgepilot/recycle_bin")
            est.reason = f"Move to recycle bin as per config ({recycle_bin_path})"


def _query_scan_result(args, scan_result: ScanResult, system_prompt: str):
    batch_size = getattr(args, "batch_size", 50)
    num_ctx = getattr(args, "num_ctx", None)
    n_batches = max(1, -(-len(scan_result.entries) // batch_size))
    print(
        f"  Found {len(scan_result.entries)} entries "
        f"({scan_result.total_size_bytes:,} bytes). "
        f"Querying LLM in {n_batches} batch(es) of up to {batch_size} entries …",
        file=sys.stderr,
    )

    report = estimate_purge_confidence(
        scan_result,
        api_url=args.api_url,
        model=args.model,
        api_key=args.api_key,
        timeout=args.timeout,
        system_prompt=system_prompt,
        batch_size=batch_size,
        num_ctx=num_ctx,
    )
    return report


def _resolve_recycle_bin_root(scan_root: str, config: dict) -> Path:
    recycle_bin_root = Path(os.path.expanduser(config.get("recycle_bin_path", ".purgepilot/recycle_bin")))
    if recycle_bin_root.is_absolute():
        return recycle_bin_root
    return Path(scan_root) / recycle_bin_root


def _build_review_commands(report, scan_result: ScanResult, config: dict, *, threshold: float) -> list[str]:
    entry_by_path = {entry.path: entry for entry in scan_result.entries}
    recycle_bin_root = _resolve_recycle_bin_root(report.root, config)
    commands = [f"# Root: {report.root}"]
    selected = 0

    for estimate in sorted(report.estimates, key=lambda item: (item.confidence, item.path), reverse=True):
        if estimate.confidence < threshold or _is_important_path(estimate.path, config):
            continue

        entry = entry_by_path.get(estimate.path)
        source_path = Path(report.root) / estimate.path

        commands.append(f"# {estimate.path}")
        commands.append(f"# confidence={estimate.confidence:.2f} reason={estimate.reason}")

        if _is_trash_path(estimate.path, config):
            delete_flag = "-rf" if entry and entry.is_dir else "-f"
            commands.append(f"rm {delete_flag} -- {shlex.quote(str(source_path))}")
        else:
            target_path = recycle_bin_root / Path(estimate.path)
            commands.append(f"mkdir -p -- {shlex.quote(str(target_path.parent))}")
            commands.append(
                f"mv -n -- {shlex.quote(str(source_path))} {shlex.quote(str(target_path))}"
            )

        commands.append("")
        selected += 1

    if selected == 0:
        commands.append(f"# No entries met threshold {threshold:.2f} for this root.")
        commands.append("")

    return commands


def _write_review_commands(command_file: Path, command_sections: list[list[str]]) -> int:
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        "# Review this file before running it.",
        "# Generated by PurgePilot; it does not execute automatically.",
        "",
    ]
    action_count = 0

    for section in command_sections:
        lines.extend(section)
        for line in section:
            if line.startswith("rm ") or line.startswith("mv "):
                action_count += 1

    command_file.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    os.chmod(command_file, 0o755)
    return action_count


def _write_permission_error_entries(scan_results: list[ScanResult]) -> int:
    full_paths = sorted(
        {
            path
            for result in scan_results
            for path in result.permission_error_entries
        }
    )
    if not full_paths:
        return 0

    output_path = Path(PERMISSION_ERROR_ENTRIES_FILE)
    output_path.write_text("\n".join(full_paths) + "\n", encoding="utf-8")
    return len(full_paths)


def main(argv: List[str] | None = None) -> int:

    if argv is None:
        argv = sys.argv[1:]

    # Dispatch to subcommand parser if first arg is a known/legacy subcommand token.
    if argv and argv[0] in {"scan", "query", "sqlquery"}:
        parser = _build_subcommand_parser()
        args = parser.parse_args(argv)
        logging.basicConfig(
            level=logging.DEBUG if getattr(args, "verbose", False) else logging.WARNING,
            format="%(levelname)s %(name)s: %(message)s",
        )
        if args.command == "scan":
            exit_code = 0
            config_path = Path(args.config)
            if not config_path.exists():
                print(f"ERROR: Config file not found: {config_path}", file=sys.stderr)
                return 1
            config = parse_config(config_path)
            command_sections: list[list[str]] = []
            scan_results: List[ScanResult] = []
            for directory in args.directories:
                dir_path = Path(directory)
                if not dir_path.exists():
                    print(f"ERROR: Directory not found: {directory}", file=sys.stderr)
                    exit_code = 1
                    continue
                if not dir_path.is_dir():
                    print(f"ERROR: Not a directory: {directory}", file=sys.stderr)
                    exit_code = 1
                    continue
                print(f"Scanning {dir_path.resolve()} …", file=sys.stderr)
                try:
                    scan_result = scan_directory(
                        dir_path,
                        max_depth=args.max_depth,
                        include_hidden=args.include_hidden,
                        processes=args.processes,
                        folders_only=getattr(args, "folders_only", False),
                    )
                except Exception as exc:
                    print(f"ERROR: Failed to scan {directory}: {exc}", file=sys.stderr)
                    exit_code = 1
                    continue
                # If --folders-only, filter out files from entries (defensive, in case scanner missed any)
                if getattr(args, "folders_only", False):
                    scan_result.entries = [e for e in scan_result.entries if getattr(e, "is_dir", False)]
                scan_results.append(scan_result)
                # Save to SQLite by default. If --save-db is omitted, pick a default path.
                save_db_path: Path | None
                if getattr(args, "save_db", None):
                    if len(args.directories) > 1:
                        print("ERROR: --save-db only supports a single directory.", file=sys.stderr)
                        exit_code = 1
                        save_db_path = None
                    else:
                        save_db_path = Path(args.save_db)
                else:
                    save_db_path = _default_scan_db_path(
                        dir_path,
                        multiple_directories=len(args.directories) > 1,
                    )

                if save_db_path is not None:
                    if getattr(args, "incremental", False):
                        upsert_scan_result(scan_result, save_db_path)
                    else:
                        save_to_sqlite(scan_result, save_db_path)
                    print(f"Saved scan database to {save_db_path.resolve()}", file=sys.stderr)
                # Optionally print scan summary
                if args.output == "json":
                    print(json.dumps(scan_result.to_dict(), indent=2))
                else:
                    print(f"Scanned {directory}: {len(scan_result.entries)} entries")
            if args.save_commands and scan_results:
                command_file = Path(args.save_commands)
                action_count = _write_review_commands(command_file, command_sections)
                print(
                    f"Saved {action_count} review commands to {command_file.resolve()}",
                    file=sys.stderr,
                )
            return exit_code
        elif args.command == "sqlquery":
            exit_code = 0
            config_path = Path(args.config)
            if not config_path.exists():
                print(f"ERROR: Config file not found: {config_path}", file=sys.stderr)
                return 1
            config = parse_config(config_path)

            db_path = Path(args.db_file)
            if not db_path.exists():
                print(f"ERROR: Database file not found: {db_path}", file=sys.stderr)
                return 1

            try:
                scan_result = load_from_sqlite(db_path)
                if not scan_result.entries:
                    print(
                        f"  Loaded 0 entries from {db_path}. Skipping LLM query (SQL mode).",
                        file=sys.stderr,
                    )
                    report = PurgeReport(root=scan_result.root, estimates=[])
                else:
                    print(
                        f"  Loaded {len(scan_result.entries)} entries from {db_path}. "
                        "Querying LLM (SQL mode) …",
                        file=sys.stderr,
                    )
                    report = estimate_purge_confidence_sql(
                        db_path,
                        api_url=args.api_url,
                        model=args.model,
                        api_key=args.api_key,
                        timeout=args.timeout,
                        num_ctx=getattr(args, "num_ctx", None),
                    )
            except Exception as exc:
                print(f"ERROR: SQL query mode failed: {exc}", file=sys.stderr)
                return 1

            _ensure_rule_based_entries_in_report(report, scan_result, config)
            _apply_config_overrides(report, config)

            if args.save_commands:
                command_sections: list[list[str]] = [
                    _build_review_commands(report, scan_result, config, threshold=args.threshold)
                ]
                command_file = Path(args.save_commands)
                action_count = _write_review_commands(command_file, command_sections)
                print(
                    f"Saved {action_count} review commands to {command_file.resolve()}",
                    file=sys.stderr,
                )

            if args.output == "json":
                print(json.dumps(report.to_dict(), indent=2))
            else:
                _print_text_report(report, threshold=args.threshold)

            return exit_code
        else:
            print(f"Unknown subcommand: {args.command}", file=sys.stderr)
            return 2

    # Default: legacy parser (no subcommand)
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    # If only --help is requested, argparse will handle it and exit before this point.

    if not args.directories:
        parser.error("At least one DIR is required.")

    exit_code = 0
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"ERROR: Config file not found: {config_path}", file=sys.stderr)
        return 1
    config = parse_config(config_path)
    system_prompt = config.get("prompt", _SYSTEM_PROMPT)
    command_sections: list[list[str]] = []

    scan_results: List[ScanResult] = []

    for directory in args.directories:
        dir_path = Path(directory)
        if not dir_path.exists():
            print(f"ERROR: Directory not found: {directory}", file=sys.stderr)
            exit_code = 1
            continue
        if not dir_path.is_dir():
            print(f"ERROR: Not a directory: {directory}", file=sys.stderr)
            exit_code = 1
            continue

        print(f"Scanning {dir_path.resolve()} …", file=sys.stderr)
        try:
            scan_result = scan_directory(
                dir_path,
                max_depth=args.max_depth,
                include_hidden=args.include_hidden,
                processes=args.processes,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: Failed to scan {directory}: {exc}", file=sys.stderr)
            exit_code = 1
            continue

        scan_results.append(scan_result)

        try:
            ai_scan_result = _filter_ai_scan_entries(scan_result, config)
            ai_scan_result = _build_directory_summary_scan(ai_scan_result)
            report = _query_scan_result(args, ai_scan_result, system_prompt)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: LLM request failed for {directory}: {exc}", file=sys.stderr)
            exit_code = 1
            continue

        _ensure_rule_based_entries_in_report(report, scan_result, config)
        _apply_config_overrides(report, config)
        if args.save_commands:
            command_sections.append(
                _build_review_commands(report, scan_result, config, threshold=args.threshold)
            )

        if args.output == "json":
            print(json.dumps(report.to_dict(), indent=2))
        else:
            _print_text_report(report, threshold=args.threshold)



    if args.save_commands and command_sections:
        command_file = Path(args.save_commands)
        action_count = _write_review_commands(command_file, command_sections)
        print(
            f"Saved {action_count} review commands to {command_file.resolve()}",
            file=sys.stderr,
        )

    return exit_code


def _print_text_report(report, *, threshold: float) -> None:
    print(f"\nPurge confidence report for: {report.root}")
    print("-" * 72)
    if not report.estimates:
        print("  (no estimates returned by LLM)")
        return

    for est in sorted(report.estimates, key=lambda e: e.confidence, reverse=True):
        flag = "🔴" if est.confidence >= threshold else "🟢"
        bar_len = int(est.confidence * 20)
        bar = "█" * bar_len + "░" * (20 - bar_len)
        print(f"{flag}  [{bar}] {est.confidence:.2f}  {est.path}")
        print(f"        {est.reason}")
    print()

    high = report.high_confidence(threshold)
    print(
        f"Summary: {len(high)} of {len(report.estimates)} entries "
        f"above confidence threshold {threshold:.2f}"
    )


if __name__ == "__main__":
    sys.exit(main())
