"""CLI entry point for PurgePilot."""

from __future__ import annotations

import argparse
import fnmatch
import json
import logging
import os
import re
import shlex
import sys
from pathlib import Path
from typing import List

from .llm_client import PurgeEstimate, estimate_purge_confidence, _SYSTEM_PROMPT
from .scanner import ScanResult, scan_directory


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
            "Typical usage: purgep scan ~ --save-scan home_scan.json, "
            "then purgep query home_scan.json --api-url http://localhost:11434/v1 "
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
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose/debug logging.",
    )
    parser.add_argument(
        "--config",
        default="config.md",
        help="Path to the configuration markdown file (default: config.md).",
    )
    parser.add_argument(
        "--scan-only",
        action="store_true",
        help="Only scan directories and output scan JSON (no LLM query).",
    )
    parser.add_argument(
        "--save-scan",
        metavar="FILE",
        help="Write the scan JSON to a file (single directory only).",
    )
    parser.add_argument(
        "--from-scan",
        nargs="+",
        metavar="FILE",
        help="Load one or more scan JSON files and only run the LLM query step.",
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
    scan_parser.add_argument("--include-hidden", action="store_true")
    scan_parser.add_argument("--output", choices=["text", "json"], default="text")
    scan_parser.add_argument("--save-scan", metavar="FILE")
    scan_parser.add_argument("--save-commands", metavar="FILE")
    scan_parser.add_argument("--config", default="config.md")
    scan_parser.add_argument("-v", "--verbose", action="store_true")

    query_parser = subparsers.add_parser(
        "query",
        help="Query the LLM using one or more saved scan JSON files.",
    )
    query_parser.add_argument("scan_files", metavar="FILE", nargs="+")
    query_parser.add_argument(
        "--api-url",
        default=os.environ.get("PURGE_PILOT_API_URL", "http://localhost:11434/v1"),
    )
    query_parser.add_argument(
        "--model",
        default=os.environ.get("PURGE_PILOT_MODEL", "llama3"),
    )
    query_parser.add_argument(
        "--api-key",
        default=os.environ.get("PURGE_PILOT_API_KEY"),
    )
    query_parser.add_argument("--threshold", type=float, default=0.7, metavar="FLOAT")
    query_parser.add_argument("--output", choices=["text", "json"], default="text")
    query_parser.add_argument("--timeout", type=int, default=120, metavar="SECONDS")
    query_parser.add_argument("--save-commands", metavar="FILE")
    query_parser.add_argument("--config", default="config.md")
    query_parser.add_argument("-v", "--verbose", action="store_true")

    return parser


def _load_scan_result(path: Path) -> ScanResult:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise ValueError(f"Scan file must contain a JSON object: {path}")
    return ScanResult.from_dict(raw)


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
    print(
        f"  Found {len(scan_result.entries)} entries "
        f"({scan_result.total_size_bytes:,} bytes). "
        f"Querying LLM …",
        file=sys.stderr,
    )

    report = estimate_purge_confidence(
        scan_result,
        api_url=args.api_url,
        model=args.model,
        api_key=args.api_key,
        timeout=args.timeout,
        system_prompt=system_prompt,
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


def main(argv: List[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    if argv and argv[0] in {"scan", "query"}:
        subcommand_parser = _build_subcommand_parser()
        subcommand_args = subcommand_parser.parse_args(argv)

        translated_argv: List[str]
        if subcommand_args.command == "scan":
            translated_argv = [*subcommand_args.directories, "--scan-only"]
            translated_argv.extend(["--max-depth", str(subcommand_args.max_depth)])
            translated_argv.extend(["--output", subcommand_args.output])
            translated_argv.extend(["--config", subcommand_args.config])
            if subcommand_args.save_commands:
                translated_argv.extend(["--save-commands", subcommand_args.save_commands])
            if subcommand_args.include_hidden:
                translated_argv.append("--include-hidden")
            if subcommand_args.save_scan:
                translated_argv.extend(["--save-scan", subcommand_args.save_scan])
            if subcommand_args.verbose:
                translated_argv.append("--verbose")
        else:
            translated_argv = ["--from-scan", *subcommand_args.scan_files]
            translated_argv.extend(["--api-url", subcommand_args.api_url])
            translated_argv.extend(["--model", subcommand_args.model])
            translated_argv.extend(["--threshold", str(subcommand_args.threshold)])
            translated_argv.extend(["--output", subcommand_args.output])
            translated_argv.extend(["--timeout", str(subcommand_args.timeout)])
            translated_argv.extend(["--config", subcommand_args.config])
            if subcommand_args.save_commands:
                translated_argv.extend(["--save-commands", subcommand_args.save_commands])
            if subcommand_args.api_key:
                translated_argv.extend(["--api-key", subcommand_args.api_key])
            if subcommand_args.verbose:
                translated_argv.append("--verbose")

        argv = translated_argv

    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.scan_only and args.from_scan:
        print("ERROR: --scan-only cannot be combined with --from-scan.", file=sys.stderr)
        return 1

    if args.from_scan and args.directories:
        print("ERROR: DIR arguments cannot be used with --from-scan.", file=sys.stderr)
        return 1

    if args.save_scan and args.from_scan:
        print("ERROR: --save-scan is only valid while scanning directories.", file=sys.stderr)
        return 1

    if args.scan_only and args.save_commands:
        print("ERROR: --save-commands requires an LLM query step and cannot be combined with --scan-only.", file=sys.stderr)
        return 1

    if args.directories and not args.scan_only and not args.from_scan:
        print(
            "ERROR: Split workflow is the default. Run `purgep scan ~ --save-scan home_scan.json` "
            "then `purgep query home_scan.json` in serial.",
            file=sys.stderr,
        )
        return 1

    if not args.from_scan and not args.directories:
        parser.error("At least one DIR is required unless --from-scan is used.")

    exit_code = 0
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"ERROR: Config file not found: {config_path}", file=sys.stderr)
        return 1
    config = parse_config(config_path)
    system_prompt = config.get("prompt", _SYSTEM_PROMPT)
    command_sections: list[list[str]] = []

    if args.from_scan:
        for scan_file in args.from_scan:
            scan_path = Path(scan_file)
            if not scan_path.exists():
                print(f"ERROR: Scan file not found: {scan_path}", file=sys.stderr)
                exit_code = 1
                continue

            print(f"Loading scan data from {scan_path.resolve()} …", file=sys.stderr)
            try:
                full_scan_result = _load_scan_result(scan_path)
                ai_scan_result = _filter_ai_scan_entries(full_scan_result, config)
                report = _query_scan_result(args, ai_scan_result, system_prompt)
            except Exception as exc:  # noqa: BLE001
                print(f"ERROR: Failed to query from scan file {scan_file}: {exc}", file=sys.stderr)
                exit_code = 1
                continue

            _ensure_rule_based_entries_in_report(report, full_scan_result, config)
            _apply_config_overrides(report, config)
            if args.save_commands:
                command_sections.append(
                    _build_review_commands(report, full_scan_result, config, threshold=args.threshold)
                )

            if args.output == "json":
                print(json.dumps(report.to_dict(), indent=2))
            else:
                _print_text_report(report, threshold=args.threshold)

        if args.save_commands:
            command_file = Path(args.save_commands)
            action_count = _write_review_commands(command_file, command_sections)
            print(
                f"Saved {action_count} review commands to {command_file.resolve()}",
                file=sys.stderr,
            )

        return exit_code

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
            )
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: Failed to scan {directory}: {exc}", file=sys.stderr)
            exit_code = 1
            continue

        scan_results.append(scan_result)

        if args.scan_only:
            continue

        try:
            ai_scan_result = _filter_ai_scan_entries(scan_result, config)
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

    if args.scan_only:
        if args.save_scan:
            if len(scan_results) != 1:
                print("ERROR: --save-scan requires exactly one DIR.", file=sys.stderr)
                return 1
            out_path = Path(args.save_scan)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(scan_results[0].to_dict(), f, indent=2)
            print(f"Saved scan JSON to {out_path.resolve()}", file=sys.stderr)

        if args.output == "json":
            if len(scan_results) == 1:
                print(json.dumps(scan_results[0].to_dict(), indent=2))
            else:
                print(json.dumps([result.to_dict() for result in scan_results], indent=2))
        else:
            for result in scan_results:
                print(
                    f"Scan summary for {result.root}: "
                    f"{len(result.entries)} entries, "
                    f"{result.total_size_bytes:,} bytes"
                )

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
