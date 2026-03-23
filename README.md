# PurgePilot

Scan a Linux **home directory** on an HPC cluster and use a local or remote
**LLM server** to estimate how confidently each file or sub-folder can be
safely purged — helping you stay within your home quota without accidentally
deleting something important.

---

## Why HPC home directories fill up

HPC home directories are typically small (50–500 GB) and shared across login
and compute nodes via network filesystems (NFS/Lustre/GPFS).  Common
offenders that silently consume quota:

- **conda / pip caches** – `~/.conda/pkgs/`, `~/.cache/pip/`, unused envs
- **compiled objects** – `.o`, `.pyc`, `__pycache__` directories from builds
- **job output files** – `*.out`, `*.err`, core dumps left by old batch jobs
- **downloaded archives** – tarballs, zip files that were never cleaned up
- **old module builds** – stale `~/.local/lib/` installs from previous projects
- **Jupyter/notebook artefacts** – `.ipynb_checkpoints/`, large output cells saved to disk

PurgePilot gives you an AI-ranked list of what to delete, move to scratch
space, or keep — without making any changes itself unless you explicitly
approve and run the generated shell script.

---

## How it works

1. **Scan** – PurgePilot walks your home directory (or any subdirectory) and
  collects metadata (path, size, last-modified timestamp, last-accessed timestamp) for every file and
  sub-folder.
2. **Ask** – The file list is sent as a prompt to any
   [OpenAI-compatible](https://platform.openai.com/docs/api-reference/chat)
   chat-completions endpoint (local [Ollama](https://ollama.com), OpenAI, etc.).
3. **Estimate** – The LLM returns a confidence score (`0.0` = keep,
   `1.0` = definitely purge) and a short reason for each entry, informed by
   HPC-specific patterns in `config.md`.
4. **Report** – PurgePilot prints a ranked text report (or machine-readable
   JSON) and optionally writes a ready-to-review shell script that moves items
   to your scratch recycle bin or deletes known-safe junk.

No files are touched until you inspect and run the generated script.

---

## Installation

HPC login nodes typically do not allow `sudo`.  Use one of the user-space
methods below.

### Recommended: conda (installs Ollama automatically)

```bash
git clone https://github.com/ld32/PurgePilot.git
cd PurgePilot
conda env create -f environment.yml
conda activate purge-pilot
```

The `environment.yml` file creates a self-contained conda environment that
includes **Ollama** (from [conda-forge](https://conda-forge.org/)) together
with all Python dependencies and the `purge-pilot` package itself.

### Alternative: pip only (bring your own Ollama)

```bash
git clone https://github.com/ld32/PurgePilot.git
cd PurgePilot
pip install .
```

Or install with development dependencies (pytest, etc.):

```bash
pip install ".[dev]"
```

---

## Local LLM server (Ollama – CPU-only, no root required)

HPC login nodes rarely have a GPU available for interactive use, so the
instructions below cover a **CPU-only** setup that runs entirely in your
user space.

### 1 – Install Ollama

**Using conda (recommended)** – Ollama is installed automatically when you
create the conda environment (see [Installation](#installation) above).
Once the environment is active you can run `ollama` directly.

**Manual installation into `~/.local` (no root):**

```bash
curl -fsSL https://ollama.com/install.sh | OLLAMA_INSTALL_DIR=~/.local sh
```

This places the `ollama` binary in `~/.local/bin` (add it to `$PATH` if
needed).

### 2 – Choose a model that fits your available RAM

On a login node the model weights must fit in the RAM allocated to your
session.  Check your cluster's login-node memory policy before pulling a
large model.

| Available RAM | Recommended model | Approx. size on disk |
|---|---|---|
| 8 GB | `phi3:mini` (3.8 B) | ~2.3 GB |
| 16 GB | `llama3.2:3b` (3 B) | ~2.0 GB |
| 32 GB | `llama3.1:8b` (8 B) | ~4.7 GB |
| 64 GB+ | `llama3.1:70b-instruct-q4_K_M` (70 B, 4-bit) | ~40 GB |

> **Tip:** 4-bit quantised models (the default `q4_K_M` variants) use
> roughly half the RAM of their full-precision counterparts and run at
> an acceptable speed on modern CPUs.

Pull the model before first use:

```bash
ollama pull phi3:mini          # replace with your chosen model
```

> **Quota note:** Ollama stores model weights under `~/.ollama/models/` by
> default, which counts against your home quota.  Point it at scratch space
> instead:
> ```bash
> export OLLAMA_MODELS=/n/scratch/users/${USER:0:1}/$USER/ollama_models
> mkdir -p "$OLLAMA_MODELS"
> ```

### 3 – Tune memory usage

If you use the provided conda environment, PurgePilot sets these defaults
automatically to reduce RAM usage:

- `OLLAMA_MAX_LOADED_MODELS=1`
- `OLLAMA_NUM_PARALLEL=1`
- `OLLAMA_KEEP_ALIVE=5m`

If you are not using conda, add these to your shell profile or set them
before calling `ollama serve`:

```bash
export OLLAMA_MAX_LOADED_MODELS=1
export OLLAMA_NUM_PARALLEL=1
export OLLAMA_KEEP_ALIVE=5m
```

### 4 – Start the server

```bash
conda activate purge-pilot   # if using conda
ollama serve &               # runs in the background on the login node
```

Ollama listens on `http://localhost:11434` by default.

### 5 – Point PurgePilot at the local server

```bash
purgep scan ~ --save-scan home_scan.json
purgep query home_scan.json \
  --api-url http://localhost:11434/v1 \
  --model phi3:mini \
  --save-commands review_purge.sh
```

Review `review_purge.sh`, then run it when satisfied:

```bash
bash review_purge.sh
```

---

## Usage

```
purgep scan DIR [DIR ...] [SCAN_OPTIONS]
purgep query FILE [FILE ...] [QUERY_OPTIONS]
```

The two-step (split) workflow is the default: scan first, query separately.
This lets you inspect what was found before sending it to an LLM, and reuse
the same scan with different models or thresholds.

### Typical HPC workflow

```bash
# 1 – Scan your home directory and save results
purgep scan ~ --save-scan home_scan.json

# 2 – Query the LLM and write a review script
purgep query home_scan.json \
  --api-url http://localhost:11434/v1 \
  --model phi3:mini \
  --save-commands review_purge.sh

# 3 – Read the script, edit as needed, then run it
less review_purge.sh
bash review_purge.sh
```

> **Context window tip:** Query mode sends directory-level summary stats by
> default (counts, age buckets, top extensions), which keeps prompts smaller.
> For very large scans, also use `--num-ctx` and tune `--batch-size`:
> ```bash
> purgep query home_scan.json \
>   --api-url http://localhost:11434/v1 \
>   --model phi3:mini \
>   --num-ctx 8192 \
>   --batch-size 80 \
>   --save-commands review_purge.sh
> ```

### Scan a specific subdirectory

```bash
purgep scan ~/projects/old_project --save-scan old_project_scan.json
purgep query old_project_scan.json \
  --api-url http://localhost:11434/v1 \
  --model llama3.2:3b
```

### JSON output (for scripting)

```bash
purgep query home_scan.json \
  --api-url http://localhost:11434/v1 \
  --model phi3:mini \
  --output json | jq '.estimates[] | select(.confidence > 0.8)'
```

---

## Configuration (`config.md`)

Edit `config.md` to customise PurgePilot's behaviour for your cluster:

| Section | Purpose |
|---|---|
| **AI Prompt** | Custom instructions given to the LLM |
| **Important Data** | Paths that are **never** purged (e.g. `~/.ssh`, `~/bin`) |
| **Recycle Bin Data** | Paths moved to scratch space before deletion |
| **Recycle Bin Path** | Scratch destination (default: cluster scratch under `$USER`) |
| **Trash Data** | Paths that are **always** deleted (caches, temp files) |

Pass a custom config file with `--config /path/to/my_config.md`.

---

## Safety notes

- **PurgePilot never deletes or moves files on its own.**  It only generates
  a shell script for you to review.
- The generated script uses `mv -n` (no-clobber) when moving to the recycle
  bin and `rm -f` only for entries explicitly listed in **Trash Data**.
- Items in the recycle bin path on scratch space are not automatically
  cleaned up — remove them manually once you are confident they are safe to
  discard.

### Split scan and AI query (CPU/GPU separation)

Run the filesystem scan on a CPU machine, then run the LLM query later on a GPU machine.
Paths listed in `config.md` under Important, Trash, and Recycle Bin are
handled by rules and are not sent to the AI query.

1. Scan only and save JSON:

```bash
purgep scan /path/to/data --save-scan scan.json --output json
```

2. Query from the saved scan JSON:

```bash
purgep query scan.json \
  --api-url http://localhost:11434/v1 \
  --model llama3
```

3. Generate a review script instead of touching data:

```bash
purgep query scan.json \
  --api-url http://localhost:11434/v1 \
  --model llama3 \
  --save-commands review-purge.sh
```

`--save-commands` writes a ready-to-review `bash` script.  For each entry
above the confidence threshold the script contains:
- A comment line showing the path, confidence score and reason
- Either `rm -f/-rf` for entries listed under **Trash Data** in `config.md`
- Or `mkdir -p` + `mv -n` to move the item into your recycle-bin path

The script is **not executed automatically**; inspect it with `less` or your
editor, remove any lines you disagree with, then run it yourself.

```bash
less review-purge.sh   # inspect
bash review-purge.sh   # run when satisfied
```

### Examples

Scan a single directory using a local Ollama server:

```bash
purgep /mnt/data/backups --api-url http://localhost:11434/v1 --model llama3
```

Scan multiple directories and output JSON:

```bash
purgep /tmp/logs /var/cache \
  --api-url http://localhost:11434/v1 \
  --model llama3 \
  --output json
```

Use the OpenAI API with an API key from an environment variable:

```bash
export PURGE_PILOT_API_KEY="sk-..."
purgep ~/Downloads --api-url https://api.openai.com/v1 --model gpt-4o
```

### Environment variables

| Variable | Description |
|---|---|
| `PURGE_PILOT_API_URL` | Default value for `--api-url` |
| `PURGE_PILOT_MODEL` | Default value for `--model` |
| `PURGE_PILOT_API_KEY` | Default value for `--api-key` |

### All options

| Option | Default | Description |
|---|---|---|
| `DIR` | *(conditional)* | One or more directories to scan (required unless `--from-scan` is used) |
| `--scan-only` | *(off)* | Only scan directories and output scan data (skip LLM query) |
| `--save-scan FILE` | *(none)* | Save scan JSON to a file (single directory only) |
| `--from-scan FILE [FILE ...]` | *(none)* | Load saved scan JSON and run only the LLM query step |
| `--save-commands FILE` | *(none)* | Write suggested `mv`/`rm` review commands to a shell script; inspect before running |
| `--api-url URL` | `http://localhost:11434/v1` | OpenAI-compatible API base URL |
| `--model NAME` | `llama3` | LLM model name |
| `--api-key TOKEN` | *(none)* | Bearer token for the API |
| `--threshold FLOAT` | `0.7` | Confidence cut-off for "high risk" summary |
| `--max-depth INT` | `10` | Maximum recursion depth |
| `--processes INT` | `1` | Number of worker processes used while scanning |
| `--include-hidden` | *(off)* | Include hidden files/dirs (`.` prefix) |
| `--output text\|json` | `text` | Output format |
| `--timeout SECONDS` | `120` | HTTP request timeout |
| `--batch-size INT` | `50` | Entries per LLM request; reduce for small-context models |
| `--num-ctx INT` | *(model default)* | Ollama context window size in tokens (e.g. `8192`); passed via `options.num_ctx` |
| `-v, --verbose` | *(off)* | Enable debug logging |

---

## Sample output

```
Scanning /mnt/data/backups …
  Found 42 entries (15,728,640,000 bytes). Querying LLM …

Purge confidence report for: /mnt/data/backups
------------------------------------------------------------------------
🔴  [████████████████████] 0.97  2019-full-backup.tar.gz
        4-year-old full backup, almost certainly superseded by newer snapshots.
🔴  [███████████████░░░░░] 0.82  logs/access.log.2021
        Rotated log file from 2021, no longer needed for operations.
🟢  [███░░░░░░░░░░░░░░░░░] 0.18  datasets/current_month.csv
        Actively-used dataset modified recently.
...

Summary: 15 of 42 entries above confidence threshold 0.70
```

---

## Running tests

```bash
pytest
```

---

## Project layout

```
purge_pilot/
  __init__.py      – package marker
  scanner.py       – recursive directory walker
  llm_client.py    – OpenAI-compatible LLM API client
  main.py          – CLI entry point
tests/
  test_scanner.py
  test_llm_client.py
  test_main.py
environment.yml    – conda environment (includes Ollama)
pyproject.toml
```
