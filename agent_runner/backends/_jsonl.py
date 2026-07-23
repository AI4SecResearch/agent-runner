"""Shared jsonl + path helpers for backends — the ``json``-stdlib counterpart
of the bash backends' ``jq`` filters.

Kept tiny and explicit so the parsing semantics are auditable side-by-side
with ``backends/*.sh``: each helper mirrors one ``jq`` expression.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator


def ensure_parent(path: str | os.PathLike) -> None:
    """Create the parent directory of ``path`` if missing (defensive —
    callers normally create OUTPUT_DIR up front). Uses pathlib for
    cross-platform path handling (no hardcoded separator)."""
    parent = Path(path).expanduser().parent
    parent.mkdir(parents=True, exist_ok=True)


def open_private_text(path: str):
    """Open a backend diagnostic for truncating writes with POSIX mode 0600."""
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_TRUNC
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        return os.fdopen(descriptor, "w", encoding="utf-8")
    except BaseException:
        os.close(descriptor)
        raise


def read_jsonl(path: str) -> list[dict]:
    """Read a jsonl file into a list of parsed objects.

    Tolerant of trailing whitespace / blank lines (like ``jq`` reading a
    partial file). Returns ``[]`` if the file is absent or empty.
    """
    try:
        with open(path, encoding="utf-8") as f:
            out: list[dict] = []
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue  # mirror `2>/dev/null` silence on malformed lines
                if isinstance(obj, dict):
                    out.append(obj)
            return out
    except FileNotFoundError:
        return []


def first_matching(path: str, predicate) -> dict | None:
    """First parsed object matching ``predicate(obj)``; None if none.

    Mirrors ``jq … | head -1`` semantics: stop at the first hit.
    """
    for obj in read_jsonl(path):
        if predicate(obj):
            return obj
    return None


def any_matching(path: str, predicate) -> bool:
    """True iff some parsed object matches ``predicate``. Mirrors ``jq -e``."""
    return any(predicate(obj) for obj in read_jsonl(path))


def iter_lines(path: str) -> Iterator[str]:
    """Yield raw lines from a file (used for the grep-style is_complete fast
    path on the claude-code side)."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                yield line
    except FileNotFoundError:
        return
