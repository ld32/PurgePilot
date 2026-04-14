"""LLM-driven SQL query mode for PurgePilot.

Instead of sending the entire file list as JSON, this module asks the LLM to
generate SQL SELECT queries against a SQLite database produced by
:mod:`purge_pilot.store`.  This keeps the prompt tiny (schema + row count)
regardless of how many files were scanned.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from .llm_client import (
    PurgeEstimate,
    PurgeReport,
    _extract_json_array,
    _normalize_content,
    _repair_completion,
    _request_completion,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_SQL_SYSTEM_PROMPT = """\
You are a disk-space management assistant for an HPC (High Performance Computing) \
Linux cluster.
You will receive metadata about a scanned home directory stored in a SQLite database.
Your task is to write SQL SELECT queries that identify files and directories safe to purge.

The SQLite database has a single table:

  files (
    path        TEXT     -- relative path of the file or directory
    is_dir      INTEGER  -- 1 if directory, 0 if file
    size_bytes  INTEGER  -- file size in bytes (0 for directories)
    modified_at TEXT     -- ISO-8601 UTC timestamp of last modification
    accessed_at TEXT     -- ISO-8601 UTC timestamp of last access (may be NULL)
    depth       INTEGER  -- depth relative to the scan root (0 = top level)
  )

Scan root : {root}
Rows      : {row_count}

Write queries targeting well-known disposable HPC items such as conda/pip caches,
compiled build artefacts (.o, .pyc, __pycache__), old job output files (*.out, *.err,
core dumps), downloaded archives, stale virtual environments, and Jupyter checkpoint
directories.

Respond with ONLY a JSON array.  Each element must have exactly these keys:
  "category"   – short label for this group of purgeable items (e.g. "pip cache")
  "confidence" – float between 0.0 (keep) and 1.0 (definitely purge)
  "sql"        – a single SQL SELECT statement that returns a column named "path"
  "reason"     – one concise sentence explaining why these items are safe to purge

Rules:
- Only write SELECT statements.  Never use INSERT, UPDATE, DELETE, DROP, or any DDL.
- Every query must include "path" in its SELECT list.
- Use only the columns listed above.  Use SQLite-compatible SQL syntax.
- Use LIKE patterns for path matching (e.g. path LIKE '%__pycache__%').
- Dates stored in modified_at / accessed_at are ISO-8601 strings; compare them with
  SQLite date/time functions, e.g. modified_at < datetime('now', '-90 days').
- Assign confidence >= 0.8 for well-known caches, build artefacts, and temporary files.
- Assign confidence <= 0.3 for anything that might be active project data.
- Omit categories where you are not confident matching items exist in this scan.

Respond with ONLY the JSON array and nothing else.\
"""

_SQL_REPAIR_SYSTEM_PROMPT = """\
You repair malformed model output.
You will receive a prior assistant response that should have been a JSON array.

Return ONLY a valid JSON array.
Each element in the array must have exactly these keys:
  "category"   – short label string
  "confidence" – float between 0.0 and 1.0
  "sql"        – a SQL SELECT statement returning a "path" column
  "reason"     – one concise sentence

Do not include markdown, code fences, or explanatory text.
If the prior response does not contain enough information to build the array, return [].\
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def estimate_purge_confidence_sql(
    db_path: str | Path,
    *,
    api_url: str,
    model: str,
    api_key: Optional[str] = None,
    timeout: int = 120,
    num_ctx: Optional[int] = None,
) -> PurgeReport:
    """Use LLM-generated SQL queries to build a :class:`~purge_pilot.llm_client.PurgeReport`.

    Parameters
    ----------
    db_path:
        Path to a SQLite database previously created by
        :func:`~purge_pilot.store.save_to_sqlite`.
    api_url:
        Base URL of an OpenAI-compatible chat-completions endpoint.
    model:
        Model name to use, e.g. ``"llama3"`` or ``"gpt-4o"``.
    api_key:
        Optional bearer token.
    timeout:
        HTTP request timeout in seconds.
    num_ctx:
        Ollama ``num_ctx`` option (context window size in tokens).
    """
    db_path = Path(db_path)
    root, row_count = _get_db_stats(db_path)

    logger.debug("DB stats: root=%s, rows=%d", root, row_count)

    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    endpoint = api_url.rstrip("/") + "/chat/completions"

    system_prompt = _SQL_SYSTEM_PROMPT.format(root=root, row_count=row_count)
    user_message = (
        f"Please generate SQL SELECT queries for the database at {db_path}."
    )

    payload: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "temperature": 0.2,
    }
    if num_ctx is not None:
        payload["options"] = {"num_ctx": num_ctx}

    logger.debug("POST %s  model=%s  (SQL query mode)", endpoint, model)
    content = _request_completion(
        endpoint=endpoint,
        headers=headers,
        payload=payload,
        timeout=timeout,
    )

    try:
        categories = _parse_categories(content)
    except ValueError as exc:
        logger.debug("LLM returned malformed categories JSON; requesting repair: %s", exc)
        repaired_content = _repair_categories(
            endpoint=endpoint,
            headers=headers,
            model=model,
            timeout=timeout,
            original_content=content,
        )
        categories = _parse_categories(repaired_content)

    estimates = _execute_queries(db_path, categories)
    return PurgeReport(root=root, estimates=estimates)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_db_stats(db_path: Path) -> tuple[str, int]:
    """Return (root, row_count) from the database."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = conn.cursor()
        cur.execute("SELECT value FROM meta WHERE key = 'root'")
        row = cur.fetchone()
        root = row[0] if row else ""
        cur.execute("SELECT COUNT(*) FROM files")
        row_count = cur.fetchone()[0]
        return root, row_count
    finally:
        conn.close()


def _is_safe_select(sql: str) -> bool:
    """Return True iff *sql* looks like a single SELECT statement."""
    stripped = sql.strip().rstrip(";").strip()
    return bool(re.match(r"select\b", stripped, re.IGNORECASE))


def _parse_categories(content: str) -> list[dict]:
    """Parse the LLM response into a list of category dicts."""
    content = content.strip()

    if content.startswith("```"):
        lines = [line for line in content.splitlines() if not line.startswith("```")]
        content = "\n".join(lines)

    extracted = _extract_json_array(content)
    if extracted is not None:
        content = extracted

    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM returned non-JSON content: {content!r}") from exc

    if not isinstance(data, list):
        raise ValueError(f"Expected a JSON array from LLM, got: {type(data).__name__}")

    return data


def _repair_categories(
    *,
    endpoint: str,
    headers: Dict[str, str],
    model: str,
    timeout: int,
    original_content: str,
) -> str:
    """Ask the model to convert its prior response into a strict JSON array."""
    payload: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SQL_REPAIR_SYSTEM_PROMPT},
            {"role": "user", "content": original_content},
        ],
        "temperature": 0,
    }
    return _request_completion(
        endpoint=endpoint,
        headers=headers,
        payload=payload,
        timeout=timeout,
    )


def _execute_queries(
    db_path: Path,
    categories: list[dict],
) -> List[PurgeEstimate]:
    """Execute validated SQL queries and return deduplicated :class:`PurgeEstimate` objects.

    For paths matched by multiple queries, the highest confidence is kept.
    """
    # path -> best PurgeEstimate so far
    by_path: dict[str, PurgeEstimate] = {}

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        for item in categories:
            try:
                category = str(item["category"])
                confidence = float(item["confidence"])
                confidence = max(0.0, min(1.0, confidence))
                sql = str(item["sql"])
                reason = str(item.get("reason", ""))
            except (KeyError, TypeError, ValueError) as exc:
                logger.debug("Skipping malformed category item %r: %s", item, exc)
                continue

            if not _is_safe_select(sql):
                logger.warning(
                    "Skipping non-SELECT query for category %r: %r", category, sql
                )
                continue

            logger.debug("Executing category=%r  confidence=%.2f", category, confidence)
            try:
                cur = conn.cursor()
                cur.execute(sql)
                rows = cur.fetchall()
                col_names = [desc[0] for desc in cur.description] if cur.description else []
            except sqlite3.Error as exc:
                logger.warning("SQL error for category %r: %s", category, exc)
                continue

            try:
                path_idx = col_names.index("path")
            except ValueError:
                logger.warning(
                    "Query for category %r did not return a 'path' column", category
                )
                continue

            for row in rows:
                path = str(row[path_idx])
                existing = by_path.get(path)
                if existing is None or confidence > existing.confidence:
                    by_path[path] = PurgeEstimate(
                        path=path,
                        confidence=confidence,
                        reason=reason,
                    )
    finally:
        conn.close()

    return list(by_path.values())
