#!/usr/bin/env python3
import argparse
import json
from pathlib import Path


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
    args = parser.parse_args()

    log_path = Path(args.path)
    if not log_path.exists():
        print(f"ERROR: File not found: {log_path}")
        return 1

    for i, line in enumerate(log_path.read_text(encoding="utf-8").splitlines(), 1):
        obj = json.loads(line)
        print(f"\n--- entry {i} ---")
        print(json.dumps(obj, indent=2, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
