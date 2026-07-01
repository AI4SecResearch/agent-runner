"""Shared payload parsing — preliminary signal extraction.

Both Claude (errors inlined in ``.result`` text) and opencode (structured
fields) error payloads are funneled through ``extract_signals`` so that
provider modules reason about a uniform ``Signals`` object regardless of
which agent produced the error.

Mirrors agent-runner's providers/base.py.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass


@dataclass
class Signals:
    """Normalized fields extracted from a raw error payload."""

    code: str | None        # explicit code field, or [NNN]/[NNNN] from message
    status: str | None      # HTTP status
    message: str


# ── recovery action vocabulary ────────────────────────────────────
# Strategies are composable: a comma-joined string of these atoms, applied
# left-to-right in one retry step. ``disable`` (mark current key bad for TTL)
# is a first-class atom, not an implicit side-effect of ``rotate`` — so a
# strategy can rotate to a fresh key WITHOUT disabling (e.g. content-safety
# errors that aren't the key's fault). The model tier is decided by whether
# ``downgrade`` is present (orthogonal to rotate).
ATOMS = ("disable", "rotate", "downgrade")
DEFAULT_ACTION = "disable,rotate,downgrade"


def _parse_payload(text: str) -> dict:
    """Tolerant parse: empty/non-JSON → {'message': <text>}."""
    if not text or not text.strip():
        return {"message": ""}
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    return {"message": text}


# [NNN] = 3 digits (HTTP status, e.g. emitted by opencode)
# [NNNN] = 4 digits (upstream-specific, e.g. zhipu's 1305)
_CODE_RE = re.compile(r"\[(\d{3,4})\]")


def extract_signals(payload_text: str) -> Signals:
    """Parse a raw error payload into Signals.

    Resolution order for ``code``:
      1. explicit ``code`` field (opencode responseBody.error.code)
      2. ``[NNN]``/``[NNNN]`` bracketed code found in ``message``
      3. None
    """
    payload = _parse_payload(payload_text)
    message = str(payload.get("message", ""))
    code = payload.get("code")
    status = payload.get("status")
    if code is None:
        m = _CODE_RE.search(message)
        if m:
            code = m.group(1)
    return Signals(
        code=str(code) if code is not None else None,
        status=str(status) if status is not None else None,
        message=message,
    )
