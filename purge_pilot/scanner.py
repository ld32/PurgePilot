"""Scan one or more directories and collect file/folder metadata."""

from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, List


@dataclass
class FileEntry:
    """Metadata for a single file or directory discovered during a scan."""

    path: str
    is_dir: bool
    size_bytes: int
    modified_at: datetime
    depth: int

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "is_dir": self.is_dir,
            "size_bytes": self.size_bytes,
            "modified_at": self.modified_at.isoformat(),
            "depth": self.depth,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "FileEntry":
        return cls(
            path=str(data["path"]),
            is_dir=bool(data["is_dir"]),
            size_bytes=int(data["size_bytes"]),
            modified_at=datetime.fromisoformat(str(data["modified_at"])),
            depth=int(data["depth"]),
        )


@dataclass
class ScanResult:
    """Collection of all entries found under a root directory."""

    root: str
    entries: List[FileEntry] = field(default_factory=list)

    @property
    def total_size_bytes(self) -> int:
        return sum(e.size_bytes for e in self.entries)

    def to_dict(self) -> dict:
        return {
            "root": self.root,
            "total_size_bytes": self.total_size_bytes,
            "entry_count": len(self.entries),
            "entries": [e.to_dict() for e in self.entries],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ScanResult":
        entries = [FileEntry.from_dict(item) for item in data.get("entries", [])]
        return cls(root=str(data["root"]), entries=entries)


def scan_directory(
    root: str | Path,
    *,
    max_depth: int = 10,
    include_hidden: bool = False,
    processes: int = 1,
) -> ScanResult:
    """Recursively scan *root* and return a :class:`ScanResult`.

    Parameters
    ----------
    root:
        Directory to scan.
    max_depth:
        Maximum recursion depth (0 = root level only).
    include_hidden:
        When *False* (default) entries whose name starts with '.' are skipped.
    processes:
        Number of worker processes used when scanning. ``1`` keeps single-process
        behavior. Values greater than ``1`` parallelize per-subdirectory scans.
    """
    root_path = Path(root).resolve()
    if not root_path.is_dir():
        raise NotADirectoryError(f"Not a directory: {root_path}")
    if processes < 1:
        raise ValueError("processes must be >= 1")

    result = ScanResult(root=str(root_path))

    if processes == 1:
        for entry in _walk(root_path, root_path, max_depth=max_depth, include_hidden=include_hidden):
            result.entries.append(entry)
        return result

    result.entries.extend(
        _walk_parallel(
            root_path,
            max_depth=max_depth,
            include_hidden=include_hidden,
            processes=processes,
        )
    )

    return result


def _walk_parallel(
    root_path: Path,
    *,
    max_depth: int,
    include_hidden: bool,
    processes: int,
) -> list[FileEntry]:
    try:
        children = list(root_path.iterdir())
    except PermissionError:
        return []

    entries: list[FileEntry] = []
    subdirs: list[Path] = []

    for child in sorted(children, key=lambda p: (p.is_file(), p.name.lower())):
        if not include_hidden and child.name.startswith("."):
            continue

        try:
            stat = child.stat()
        except (OSError, PermissionError):
            continue

        modified_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        is_dir = child.is_dir()
        size = 0 if is_dir else stat.st_size

        entries.append(
            FileEntry(
                path=str(child.relative_to(root_path)),
                is_dir=is_dir,
                size_bytes=size,
                modified_at=modified_at,
                depth=0,
            )
        )

        if is_dir and max_depth > 0:
            subdirs.append(child)

    if not subdirs:
        return entries

    max_workers = min(processes, len(subdirs))
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(
                _walk_subtree_worker,
                str(root_path),
                str(subdir),
                max_depth,
                include_hidden,
            )
            for subdir in subdirs
        ]
        for future in futures:
            entries.extend(future.result())

    return entries


def _walk_subtree_worker(base_path: str, subtree_path: str, max_depth: int, include_hidden: bool) -> list[FileEntry]:
    base = Path(base_path)
    subtree = Path(subtree_path)
    return list(
        _walk(
            base,
            subtree,
            max_depth=max_depth,
            include_hidden=include_hidden,
            depth=1,
        )
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _walk(
    base: Path,
    current: Path,
    *,
    max_depth: int,
    include_hidden: bool,
    depth: int = 0,
) -> Iterator[FileEntry]:
    """Yield :class:`FileEntry` objects by walking *current* recursively."""
    try:
        children = list(current.iterdir())
    except PermissionError:
        return

    for child in sorted(children, key=lambda p: (p.is_file(), p.name.lower())):
        if not include_hidden and child.name.startswith("."):
            continue

        try:
            stat = child.stat()
        except (OSError, PermissionError):
            continue

        modified_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        is_dir = child.is_dir()
        size = 0 if is_dir else stat.st_size

        yield FileEntry(
            path=str(child.relative_to(base)),
            is_dir=is_dir,
            size_bytes=size,
            modified_at=modified_at,
            depth=depth,
        )

        if is_dir and depth < max_depth:
            yield from _walk(
                base,
                child,
                max_depth=max_depth,
                include_hidden=include_hidden,
                depth=depth + 1,
            )
