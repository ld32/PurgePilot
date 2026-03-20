"""Send a file/folder list to an LLM server and parse purge confidence scores."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests

from .scanner import ScanResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class PurgeEstimate:
    """Purge confidence estimate for a single path."""

    path: str
    confidence: float  # 0.0 (keep) → 1.0 (definitely purge)
    reason: str

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "confidence": self.confidence,
            "reason": self.reason,
        }


@dataclass
class PurgeReport:
    """Full purge report returned by the LLM client."""

    root: str
    estimates: List[PurgeEstimate]

    def to_dict(self) -> dict:
        return {
            "root": self.root,
            "estimates": [e.to_dict() for e in self.estimates],
        }

    def high_confidence(self, threshold: float = 0.7) -> List[PurgeEstimate]:
        """Return entries whose confidence is at or above *threshold*."""
        return [e for e in self.estimates if e.confidence >= threshold]


# ---------------------------------------------------------------------------
# LLM client
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are a disk-space management assistant for an HPC (High Performance Computing) Linux cluster.
You will receive a JSON list of files and directories from a user's home directory.
HPC home directories have tight quotas and commonly accumulate:
  conda/pip caches, compiled build artefacts (.o, .pyc, __pycache__), old job output files
  (*.out, *.err, core dumps), downloaded archives, stale virtual environments, and Jupyter
  checkpoint directories.
For each entry evaluate how safe it is to delete it from the home directory and return a JSON array.

Do not invent, rewrite, summarize, or normalize paths.
Only use exact path strings that appear in the input.

Each element in the response array must have exactly these keys:
  "path"       – the exact path string from the input
  "confidence" – a float between 0.0 (definitely keep) and 1.0 (definitely purge)
  "reason"     – one concise sentence explaining the decision

Assign high confidence (≥ 0.8) to well-known disposable items such as package caches,
build artefacts, core dumps, and clearly temporary files.
Be conservative (≤ 0.3) with source code, configuration files, unique data files,
SSH keys, shell dotfiles, and anything that looks like active project work.

Respond with ONLY the JSON array and nothing else."""

_REPAIR_SYSTEM_PROMPT = """You repair malformed model output.
You will receive a prior assistant response that should have been a JSON array.

Return ONLY a valid JSON array.
Each element in the array must have exactly these keys:
    \"path\"       - the exact path string from the input when available
    \"confidence\" - a float between 0.0 and 1.0
    \"reason\"     - one concise sentence

Do not include markdown, code fences, or explanatory text.
Do not invent or normalize paths. Only use exact input path strings.
If the prior response does not contain enough information to build the array, return []."""


def estimate_purge_confidence(
    scan_result: ScanResult,
    *,
    api_url: str,
    model: str,
    api_key: Optional[str] = None,
    timeout: int = 120,
    system_prompt: str = _SYSTEM_PROMPT,
    batch_size: int = 50,
    num_ctx: Optional[int] = None,
) -> PurgeReport:
    """Call the LLM server and return a :class:`PurgeReport`.

    Parameters
    ----------
    scan_result:
        Result produced by :func:`~purge_pilot.scanner.scan_directory`.
    api_url:
        Base URL of an OpenAI-compatible chat-completions endpoint,
        e.g. ``"http://localhost:11434/v1"`` for a local Ollama server
        or ``"https://api.openai.com/v1"`` for the OpenAI API.
    model:
        Model name to use, e.g. ``"gpt-4o"`` or ``"llama3"``.
    api_key:
        Optional bearer token. Pass ``None`` for servers that don't require
        authentication (e.g. a local Ollama instance).
    timeout:
        HTTP request timeout in seconds.
    system_prompt:
        The system prompt to use for the LLM.
    batch_size:
        Number of entries to send per LLM request. Smaller values keep the
        prompt within the model's context window.
    num_ctx:
        Ollama ``num_ctx`` option (context window size in tokens). Pass
        ``None`` to use the model's default.
    """
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    endpoint = api_url.rstrip("/") + "/chat/completions"
    entries = scan_result.entries
    all_estimates: List[PurgeEstimate] = []

    batches = [entries[i:i + batch_size] for i in range(0, max(len(entries), 1), batch_size)]
    for batch_index, batch in enumerate(batches):
        logger.debug(
            "POST %s  model=%s  batch=%d/%d  entries=%d",
            endpoint, model, batch_index + 1, len(batches), len(batch),
        )
        entries_json = json.dumps([e.to_dict() for e in batch], indent=2)
        allowed_paths = {entry.path for entry in batch}

        payload: Dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": entries_json},
            ],
            "temperature": 0.2,
        }
        if num_ctx is not None:
            payload["options"] = {"num_ctx": num_ctx}

        content = _request_completion(
            endpoint=endpoint,
            headers=headers,
            payload=payload,
            timeout=timeout,
        )

        try:
            estimates = _parse_estimates(content, allowed_paths=allowed_paths)
        except ValueError as exc:
            logger.debug("LLM returned malformed JSON; requesting repair: %s", exc)
            repaired_content = _repair_completion(
                endpoint=endpoint,
                headers=headers,
                model=model,
                timeout=timeout,
                original_content=content,
                allowed_paths=sorted(allowed_paths),
            )
            estimates = _parse_estimates(repaired_content, allowed_paths=allowed_paths)

        all_estimates.extend(estimates)

    return PurgeReport(root=scan_result.root, estimates=all_estimates)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _request_completion(
    *,
    endpoint: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
    timeout: int,
) -> str:
    """Send one chat completion request and return normalized message content."""
    response = requests.post(endpoint, headers=headers, json=payload, timeout=timeout)
    response.raise_for_status()

    raw = response.json()
    content = raw["choices"][0]["message"]["content"]
    return _normalize_content(content)


def _repair_completion(
    *,
    endpoint: str,
    headers: Dict[str, str],
    model: str,
    timeout: int,
    original_content: str,
    allowed_paths: List[str],
) -> str:
    """Ask the model to convert its prior response into a strict JSON array."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _REPAIR_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "allowed_paths": allowed_paths,
                        "previous_response": original_content,
                    },
                    indent=2,
                ),
            },
        ],
        "temperature": 0,
    }
    return _request_completion(
        endpoint=endpoint,
        headers=headers,
        payload=payload,
        timeout=timeout,
    )


def _normalize_content(content: Any) -> str:
    """Normalize string or structured message content into plain text."""
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        if parts:
            return "\n".join(parts)

    raise ValueError(f"Unsupported LLM message content type: {type(content).__name__}")


def _parse_estimates(
    content: str,
    *,
    allowed_paths: set[str] | None = None,
) -> List[PurgeEstimate]:
    """Parse the LLM response content into a list of :class:`PurgeEstimate`."""
    content = content.strip()

    # Strip optional markdown code fences
    if content.startswith("```"):
        lines = content.splitlines()
        # Remove opening fence (e.g. ```json) and closing fence
        lines = [l for l in lines if not l.startswith("```")]
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

    estimates: List[PurgeEstimate] = []
    for item in data:
        try:
            path = str(item["path"])
            if allowed_paths is not None and path not in allowed_paths:
                logger.debug("Skipping unknown LLM response path %r", path)
                continue

            confidence = float(item["confidence"])
            confidence = max(0.0, min(1.0, confidence))  # clamp to [0, 1]
            estimates.append(
                PurgeEstimate(
                    path=path,
                    confidence=confidence,
                    reason=str(item.get("reason", "")),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.debug("Skipping malformed LLM response item %r: %s", item, exc)

    return estimates


def _extract_json_array(content: str) -> str | None:
    """Return the first decodable JSON array embedded in *content*, if any."""
    decoder = json.JSONDecoder()

    for index, char in enumerate(content):
        if char != "[":
            continue

        try:
            parsed, end = decoder.raw_decode(content, idx=index)
        except json.JSONDecodeError:
            continue

        if isinstance(parsed, list):
            return content[index:end]

    return None
