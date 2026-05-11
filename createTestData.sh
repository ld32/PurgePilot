#!/bin/bash
# Create a test folder/file structure for PurgePilot testing
# Usage: bash createTestData.sh [TARGET_DIR]

set -euo pipefail

TARGET_DIR="${1:-testdata}"
echo "Creating test data in $TARGET_DIR"

rm -rf "$TARGET_DIR"
mkdir -p "$TARGET_DIR"
cd "$TARGET_DIR"


# Create all folders from config.md Important Data
mkdir -p bin .config .gnupg .local/bin .kube
touch .bashrc .bash_profile .bash_history .profile .ssh .gitconfig .git-credentials

# Recycle Bin Data
mkdir -p Downloads archive old_projects results jobs

# Trash Data (folders only)
mkdir -p .conda/pkgs .cache/pip .cache/huggingface .cache/torch ondemand tmp __pycache__ .ipynb_checkpoints
mkdir -p .snakemake/tmp .snakemake/log .snakemake/incomplete .snakemake/metadata .nextflow/cache sample_STARtmp

# Top-level test folders
mkdir -p normal_dir nested/hidden_dir

# Files in root
head -c 1024 </dev/urandom > file1.bin
head -c 2048 </dev/urandom > file2.log

# Files in special dirs
head -c 512 </dev/urandom > bin/keepme.txt
head -c 512 </dev/urandom > Downloads/downloaded.zip
head -c 512 </dev/urandom > archive/archived.tar.gz
head -c 512 </dev/urandom > jobs/job1.out
head -c 512 </dev/urandom > jobs/job2.err
head -c 512 </dev/urandom > .cache/pip/pip_cache_file
head -c 512 </dev/urandom > __pycache__/module.pyc
head -c 512 </dev/urandom > .ipynb_checkpoints/checkpoint.ipynb
head -c 512 </dev/urandom > .snakemake/tmp/job123.tmp
head -c 512 </dev/urandom > .snakemake/log/snakemake.log
head -c 512 </dev/urandom > .snakemake/incomplete/sample.partial
head -c 512 </dev/urandom > .snakemake/metadata/rule.json
head -c 512 </dev/urandom > .nextflow/cache/task123.bin
head -c 512 </dev/urandom > .nextflow.log
head -c 512 </dev/urandom > sample_STARtmp/chunk_001.tmp

# Hidden files and dirs
mkdir -p .hidden_folder
head -c 128 </dev/urandom > .hidden_file
head -c 128 </dev/urandom > .hidden_folder/hidden_inside.txt

# Nested structure
mkdir -p nested/old_stuff
head -c 256 </dev/urandom > nested/old_stuff/ancient.dat

# Simulate old files (90+ days)
if touch -d '100 days ago' nested/old_stuff/ancient.dat 2>/dev/null; then
  touch -d '100 days ago' nested/old_stuff/ancient.dat
fi

# Simulate recent access
if touch -a -d '2 days ago' file1.bin 2>/dev/null; then
  touch -a -d '2 days ago' file1.bin
fi

# Add a symlink
ln -s file1.bin symlink_to_file1


# Extra important files/folders for testing 'never delete' logic
mkdir -p .vscode .jupyter .mozilla
# Add a marker file to .vscode to ensure it is not deleted
touch .vscode/DO_NOT_DELETE
head -c 256 </dev/urandom > .vscode/settings.json
head -c 256 </dev/urandom > .jupyter/jupyter_notebook_config.py

cd ..
echo "Test data created in $TARGET_DIR"
