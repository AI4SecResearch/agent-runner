"""Zhipu (智谱 / BigModel) provider backend.

Built-in error codes for the GLM API (the upstream codes Claude Code's
zhipu path emits, and opencode's zhipuai-coding-plan path surfaces via
``responseBody.error.code``). These defaults let zhipu classify correctly
with NO ``errorHandling`` block in providers.jsonc — config is an override
layer, not a dependency.
"""

from __future__ import annotations

from .base import Signals
from .default import DefaultProvider, DEFAULT_ACTION


class ZhipuProvider(DefaultProvider):
    """Zhipu — built-in GLM upstream code → action map."""

    id = "zhipu"
    default_error_handling = {
        "1305": "downgrade",                 # content / sensitive
        "1308": "rotate_key",                # quota-related
        "1310": "rotate_key",
        "_default": "rotate_then_downgrade",
    }

    def classify(self, signals: Signals, overrides: dict[str, str]) -> str:
        merged = dict(self.default_error_handling)
        merged.update(overrides)
        code = signals.code or signals.status
        action = (
            merged.get(code, merged.get("_default", DEFAULT_ACTION))
            if code
            else merged.get("_default", DEFAULT_ACTION)
        )
        disable = "true" if "rotate" in action else "false"
        return f"{action}:{disable}"
