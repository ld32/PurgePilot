# PurgePilot Configuration

## AI Prompt

This is the prompt used by the AI to determine which files and folders in an
HPC user's Linux home directory should be purged. Customize it to guide the
AI's decision-making for your cluster environment.

```
You are a disk-space management assistant for an HPC (High Performance Computing) Linux cluster.
You will receive a list of files and directories from a user's home directory ($HOME).
Home directories on HPC clusters have tight quotas (typically 50–500 GB) and fill up with:
  - conda/pip package caches (~/.conda/pkgs, ~/.cache/pip, ~/.cache/huggingface)
  - compiled build artefacts (.o files, __pycache__, .pyc files)
  - old batch job output files (*.out, *.err, core.* dumps)
  - downloaded source archives (.tar.gz, .zip) that were already extracted
  - stale virtual environments and conda environments no longer in use
  - Jupyter notebook checkpoint directories (.ipynb_checkpoints)
  - large temporary files left in ~/tmp or ~/scratch_*

For each file/folder evaluate how safe it is to purge it from the home directory.
Assign a confidence score from 0.0 (must keep) to 1.0 (safe to delete).
Prefer high confidence for well-known disposable patterns (caches, build artefacts, core dumps).
Be conservative with unfamiliar paths, source code, or data that looks unique.
```

## Important Data (Never purge or move)

These paths are critical to the user's shell environment, access, and active
work. PurgePilot will never suggest purging them regardless of LLM output.

- ~/bin
- ~/.bashrc*
- ~/.bash_profile*
- ~/.bash_history*
- ~/.profile*
- ~/.ssh*
- ~/.config/
- ~/.gnupg/
- ~/.local/bin/
- ~/.kube/
- ~/.gitconfig
- ~/.git-credentials

## Recycle Bin Data (Move to recycle bin on scratch)

These paths should be moved to the recycle bin on scratch space rather than
deleted immediately. Review before permanently removing.

- ~/Downloads/
- ~/archive/
- ~/old_projects/
- ~/results/
- ~/jobs/

## Recycle Bin Path

Destination on cluster scratch space for recycled items.
Update this to match your cluster's scratch filesystem layout.

- /n/scratch/users/${USER:0:1}/$USER/PurgePilotRecycleBin

## Trash Data (Always delete)

Known-safe throwaway files that can be deleted without review.
These are regenerable caches, build artefacts, and temporary files.

- ~/.conda/pkgs/
- ~/.cache/pip/
- ~/.cache/huggingface/
- ~/.cache/torch/
- ondemand/ 
- ~/tmp/ 
- *.tmp
- *.o
- *.pyc
- __pycache__/
- .ipynb_checkpoints/
- core.*
