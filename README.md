# PurgePilot

PurgePilot helps you reclaim HPC home-directory quota safely.
It scans files and folders, asks an OpenAI-compatible LLM for purge confidence,
and generates a review script. Nothing is deleted automatically.

## At a glance

- Works with local Ollama or remote OpenAI-compatible endpoints
- Uses a two-step workflow: scan first, query later
- Uses SQL query mode by default (scan to SQLite, then query with `sqlquery`)
- Supports rule-based keep/delete/move behavior via config
- Includes conservative bioinformatics temporary-file defaults

## Why this exists

HPC home directories are usually small and fill up with regenerable data:

- package caches and build artifacts
- old job logs and core dumps
- notebook checkpoints
- temporary workflow data

Typical bioinformatics temporary patterns include:

- .snakemake/tmp/, .snakemake/log/, .snakemake/incomplete/, .snakemake/metadata/
- .nextflow/cache/, .nextflow.log*
- *_STARtmp*, *.samtools.tmp*, *.sort.tmp.*

## How it works

1. Scan: walk directories and collect metadata (path, size, mtime, atime).
2. Query: use metadata to query an LLM endpoint.
3. Score: receive confidence per path (0.0 keep, 1.0 purge).
4. Review: optionally write a shell script with suggested move/delete commands.

SQL query mode is the default workflow. It keeps prompts small by sending
schema and row count instead of the full list.

## Install

### Conda (recommended)

```bash
git clone https://github.com/ld32/PurgePilot.git
cd PurgePilot
conda env create -f environment.yml
conda activate purge-pilot
```

### Pip

```bash
git clone https://github.com/ld32/PurgePilot.git
cd PurgePilot
pip install .
```

Developer extras:

```bash
pip install ".[dev]"
```

## Quick start

```bash
# 1) Scan (creates scan.db by default)
purgep scan ~

# 2) Query from SQLite and write review commands
purgep sqlquery scan.db \
  --api-url http://localhost:11434/v1 \
  --model phi3:mini \
  --save-commands review_purge.sh

# 3) Inspect and run only if you agree
less review_purge.sh
bash review_purge.sh
```

## Local Ollama setup (short)

If Ollama is already installed, skip this section.

```bash
curl -fsSL https://ollama.com/install.sh | OLLAMA_INSTALL_DIR=~/.local sh
ollama pull phi3:mini
ollama serve &
```

Useful low-memory defaults:

```bash
export OLLAMA_MAX_LOADED_MODELS=1
export OLLAMA_NUM_PARALLEL=1
export OLLAMA_KEEP_ALIVE=5m
```

Optional scratch location for model files:

```bash
export OLLAMA_MODELS=/n/scratch/users/${USER:0:1}/$USER/ollama_models
mkdir -p "$OLLAMA_MODELS"
```

## CLI usage

```text
purgep scan DIR [DIR ...] [SCAN_OPTIONS]
purgep sqlquery DB [SQLQUERY_OPTIONS]
purgep dbquery DB
```

### Common commands

Scan directories only (faster):

```bash
purgep scan ~ --folders-only --save-db scan.db
```

Interactive DB exploration (no LLM):

```bash
purgep dbquery scan.db
```

JSON output for scripting:

```bash
purgep sqlquery scan.db \
  --api-url http://localhost:11434/v1 \
  --model phi3:mini \
  --output json | jq '.estimates[] | select(.confidence > 0.8)'
```

## Configuration

Default configuration is in config.md. You can pass a custom file:

```bash
purgep scan ~ --config /path/to/my_config.md
```

### Config sections

- AI Prompt: LLM behavior guidance
- Important Data: never purge
- Recycle Bin Data: move to recycle bin path
- Recycle Bin Path: destination for moved items
- Trash Data: always delete (known-safe junk)

### Bioinformatics defaults

The default config includes conservative temporary workflow patterns, including:

- .snakemake/tmp/, .snakemake/log/, .snakemake/incomplete/, .snakemake/metadata/
- .nextflow/cache/, .nextflow.log*
- *_STARtmp*, *.samtools.tmp*, *.sort.tmp.*

Research outputs are treated cautiously unless clearly temporary.

## Safety and data handling

- PurgePilot does not delete or move files by itself.
- Generated scripts are for manual review and execution.
- Trash Data entries can be removed with rm commands in the review script.
- Recycle Bin Data entries are moved with mv -n to avoid clobbering.
- In split workflows, Important/Trash/Recycle-bin rule matches are handled by
  rules and excluded from AI query input.

## Split workflow (CPU scan, GPU query)

```bash
# On CPU machine
purgep scan /path/to/data --save-db scan.db

# On GPU machine (or different host with model access)
purgep sqlquery scan.db \
  --api-url http://localhost:11434/v1 \
  --model llama3 \
  --save-commands review-purge.sh
```

## Environment variables

- PURGE_PILOT_API_URL
- PURGE_PILOT_MODEL
- PURGE_PILOT_API_KEY
- PURGE_PILOT_LOG_CONVERSATION
- PURGE_PILOT_CONVERSATION_LOG
- PURGE_PILOT_RUN_ID

## Option highlights

Scan options:

- --folders-only
- --save-db FILE
- --save-commands FILE
- --max-depth INT
- --processes INT
- --include-hidden
- --output text|json
- --config FILE

Sqlquery options:

- --api-url URL
- --model NAME
- --api-key TOKEN
- --threshold FLOAT
- --num-ctx INT
- --save-commands FILE
- --output text|json
- --timeout SECONDS
- --config FILE

Use --help for the full option reference:

```bash
purgep scan --help
purgep sqlquery --help
purgep dbquery --help
```

## Running tests

```bash
pytest
```

## Project layout

```text
purge_pilot/
  scanner.py
  llm_client.py
  llm_sql_client.py
  store.py
  main.py
tests/
  test_scanner.py
  test_llm_client.py
  test_llm_sql_client.py
  test_store.py
  test_main.py
```

## Developer run from source

```bash
python -m purge_pilot.main scan ~
python -m purge_pilot.main sqlquery scan.db \
  --api-url http://localhost:11434/v1 \
  --model phi3:mini
```

If needed:

```bash
PYTHONPATH=. python -m purge_pilot.main --help
```
