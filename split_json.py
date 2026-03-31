import json
import math
import sys
from pathlib import Path

# Usage: python split_json.py input.json [max_tokens_per_chunk]
# Default max_tokens_per_chunk is 3000

def estimate_tokens(text):
    # Rough estimate: 1 token ≈ 4 chars (for English, OpenAI models)
    return max(1, len(text) // 4)

def main():
    if len(sys.argv) < 2:
        print("Usage: python split_json.py input.json [max_tokens_per_chunk]")
        sys.exit(1)
    input_path = Path(sys.argv[1])
    max_tokens = int(sys.argv[2]) if len(sys.argv) > 2 else 3000

    with open(input_path, 'r') as f:
        data = json.load(f)

    # Accept either a top-level array or an object with an 'entries' array
    if isinstance(data, list):
        entries = data
        prefix = None
    elif isinstance(data, dict) and 'entries' in data and isinstance(data['entries'], list):
        entries = data['entries']
        prefix = {k: v for k, v in data.items() if k != 'entries'}
    else:
        print("Error: JSON must be a list or an object with an 'entries' array.")
        sys.exit(1)

    chunks = []
    current_chunk = []
    current_tokens = 0
    for item in entries:
        item_str = json.dumps(item)
        item_tokens = estimate_tokens(item_str)
        if current_tokens + item_tokens > max_tokens and current_chunk:
            chunks.append(current_chunk)
            current_chunk = []
            current_tokens = 0
        current_chunk.append(item)
        current_tokens += item_tokens
    if current_chunk:
        chunks.append(current_chunk)

    base = input_path.stem
    for i, chunk in enumerate(chunks):
        out_path = input_path.parent / f"{base}_part{i+1}.json"
        if prefix is not None:
            out_obj = dict(prefix)
            out_obj['entries'] = chunk
            with open(out_path, 'w') as f:
                json.dump(out_obj, f, indent=2)
            print(f"Wrote {out_path} ({len(chunk)} entries, with prefix)")
        else:
            with open(out_path, 'w') as f:
                json.dump(chunk, f, indent=2)
            print(f"Wrote {out_path} ({len(chunk)} items)")

if __name__ == "__main__":
    main()
