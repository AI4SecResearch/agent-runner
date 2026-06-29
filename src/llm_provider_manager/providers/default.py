"""Default provider backend — config-driven fallback.

Used for any provider id without a dedicated module. Has no built-in
code→action map; ``classify`` consults the config overrides (with a
hardcoded ``_default`` action so the provider still classifies sanely
under zero config).
"""

from __future__ import annotations

from .base import Signals

DEFAULT_ACTION = "rotate_then_downgrade"


class DefaultProvider:
    """Generic fallback: pure config-driven, with a hardcoded _default."""

    id = "_default"
    default_error_handling: dict[str, str] = {}

    def classify(self, signals: Signals, overrides: dict[str, str]) -> str:
        merged = dict(self.default_error_handling)
        merged.update(overrides)
        merged.setdefault("_default", DEFAULT_ACTION)
        code = signals.code or signals.status
        action = (
            merged.get(code, merged.get("_default", DEFAULT_ACTION))
            if code
            else merged.get("_default", DEFAULT_ACTION)
        )
        disable = "true" if "rotate" in action else "false"
        return f"{action}:{disable}"
