"""OpenCsiTool provider backend.

opencsitool surfaces budget-exhaustion as a free-text message (no
``[NNNN]`` bracketed code, no JSON ``code``/``status`` field), e.g.:

    API Error: Request rejected (429) · Budget has been exceeded!

So ``extract_signals`` yields no code for it, and it would fall through to
the generic ``_default``. That's wrong here: a budget-exhausted key should
be disabled and the pool rotated, not merely rotated. ``classify`` below
pattern-matches the budget text / HTTP 429 and maps them to
``disable,rotate`` before falling back to the DefaultProvider logic for
everything else.
"""

from __future__ import annotations

from .base import Signals
from .default import DefaultProvider


class OpencsitoolProvider(DefaultProvider):
    """OpenCsiTool — recognises budget exhaustion; else falls to default."""

    id = "opencsitool"
    default_error_handling: dict[str, str] = {}

    def classify(self, signals: Signals, overrides: dict[str, str]) -> str:
        msg = (signals.message or "").lower()
        is_budget = "budget" in msg and "exceed" in msg
        is_429 = signals.status == "429" or "429" in msg
        if is_budget or is_429:
            return "disable,rotate"
        return super().classify(signals, overrides)
