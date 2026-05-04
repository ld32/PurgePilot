#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path


def _print_jsonl(path: Path, *, interactive: bool) -> int:
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        obj = json.loads(line)
        print(f"\n--- entry {i} ---")
        print(json.dumps(obj, indent=2, ensure_ascii=False))

        if interactive:
            try:
                choice = input("\nPress Enter for next entry, or type 'q' to quit: ").strip().lower()
            except EOFError:
                print("\nInput stream closed. Exiting interactive mode.")
                break
            if choice in {"q", "quit"}:
                break

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pretty-print a JSONL conversation log file."
    )
    parser.add_argument(
        "path",
        nargs="?",
        default="conversation_log.jsonl",
        help="Path to the JSONL log file (default: conversation_log.jsonl)",
    )
    parser.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help="Pause between entries and allow quitting early",
    )
    args = parser.parse_args()

    target_path = Path(args.path)
    if not target_path.exists():
        print(f"ERROR: File not found: {target_path}")
        return 1

    if args.interactive and not sys.stdin.isatty():
        print("ERROR: Interactive mode requires a TTY stdin")
        return 1

    return _print_jsonl(target_path, interactive=args.interactive)


if __name__ == "__main__":
    raise SystemExit(main())
