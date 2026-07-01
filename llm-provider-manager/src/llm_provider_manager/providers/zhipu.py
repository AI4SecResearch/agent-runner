"""Zhipu (智谱 / BigModel) provider backend.

Built-in error codes for the GLM API (the upstream codes Claude Code's
zhipu path emits, and opencode's zhipuai-coding-plan path surfaces via
``responseBody.error.code``). These defaults let zhipu classify correctly
with NO ``errorHandling`` block in providers.jsonc — config is an override
layer, not a dependency.

Strategies are composable atom strings (see providers/base.py ATOMS):
``disable`` / ``rotate`` / ``downgrade``. ``disable`` is explicit, so
content-safety codes (1301/1305) that aren't the key's fault rotate
and/or downgrade WITHOUT disabling the key.
"""

from __future__ import annotations

from .base import DEFAULT_ACTION, Signals
from .default import DefaultProvider


class ZhipuProvider(DefaultProvider):
    """Zhipu — built-in GLM upstream code → action map."""

    id = "zhipu"
    default_error_handling = {
        "1301": "rotate,downgrade",          # content safety — not the key's fault
        "1305": "downgrade",                 # traffic overload — same key, smaller model
        "1308": "disable,rotate",            # quota — disable bad key, move on
        "1310": "disable,rotate",
        "_default": "rotate",                # unknown — cautiously try another key
    }

    def classify(self, signals: Signals, overrides: dict[str, str]) -> str:
        merged = dict(self.default_error_handling)
        merged.update(overrides)
        code = signals.code or signals.status
        return (
            merged.get(code, merged.get("_default", DEFAULT_ACTION))
            if code
            else merged.get("_default", DEFAULT_ACTION)
        )
