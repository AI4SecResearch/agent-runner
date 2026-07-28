"""Zhipu (智谱 / BigModel) provider backend.

Built-in error codes for the GLM API (the upstream codes Claude Code's
zhipu path emits, and opencode's zhipuai-coding-plan path surfaces via
``responseBody.error.code``). These defaults let zhipu classify correctly
with NO ``errorHandling`` block in providers.jsonc — config is an override
layer, not a dependency.

Strategies are composable atom strings (see providers.ATOMS):
``disable`` / ``rotate`` / ``downgrade``. ``disable`` is explicit, so
content-safety codes (1301/1305) that aren't the key's fault rotate
and/or downgrade WITHOUT disabling the key.

This module owns its payload parsing: zhipu errors carry a ``[NNNN]``
bracketed code (or a JSON ``code``/``status`` field), so ``extract_signals``
parses those into a ``Signals`` for the code lookup below. The parsing is
zhipu's, not the framework's — other providers parse their own way.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from . import DEFAULT_ACTION, ErrorClassification
from .default import DefaultProvider


# ── payload parsing (zhipu's convention: bracketed / JSON code) ───

@dataclass
class Signals:
    """Normalized fields extracted from a zhipu error payload."""

    code: str | None        # explicit code field, or [NNNN] from message
    status: str | None      # HTTP status
    message: str


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


# zhipu upstream codes are 4-digit, e.g. 1301/1305/1308/1310.
_CODE_RE = re.compile(r"\[(\d{3,4})\]")


def extract_signals(payload_text: str) -> Signals:
    """Parse a zhipu error payload into Signals.

    Resolution order for ``code``:
      1. explicit ``code`` field
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


# ── the backend ───────────────────────────────────────────────────

class ZhipuProvider(DefaultProvider):
    """Zhipu — built-in GLM upstream code → action map."""

    id = "zhipu"
    resource_exhaustion_codes = frozenset({"1308", "1310"})
    default_error_handling = {
        "1301": "rotate,downgrade",          # content safety — not the key's fault
        "1305": "downgrade",                 # traffic overload — same key, smaller model
        "1308": "disable,rotate",            # quota — disable bad key, move on
        "1310": "disable,rotate",
        "_default": "rotate",                # unknown — cautiously try another key
    }

    def classify(self, payload_text: str, overrides: dict[str, str]) -> str:
        return self.classify_details(payload_text, overrides).action

    def classify_details(
        self,
        payload_text: str,
        overrides: dict[str, str],
    ) -> ErrorClassification:
        signals = extract_signals(payload_text)
        merged = dict(self.default_error_handling)
        merged.update(overrides)
        code = signals.code or signals.status
        action = (
            merged.get(code, merged.get("_default", DEFAULT_ACTION))
            if code
            else merged.get("_default", DEFAULT_ACTION)
        )
        return ErrorClassification(
            action=action,
            matched=code is not None and code in merged,
            resource_exhausted=code in self.resource_exhaustion_codes,
        )
