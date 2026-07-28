"""OpenCsiTool provider backend.

opencsitool surfaces budget-exhaustion as a free-text message (no
structured code), e.g.:

    API Error: Request rejected (429) · Budget has been exceeded!

This provider parses its own way: ``classify`` pattern-matches the budget
text / HTTP 429 and maps them to ``disable,rotate`` (a budget-exhausted key
should be disabled and the pool rotated, not merely rotated). Anything
else falls through to the DefaultProvider lookup. The parsing here is
opencsitool's, not the framework's.
"""

from __future__ import annotations

import json

from . import ErrorClassification
from .default import DefaultProvider


class OpencsitoolProvider(DefaultProvider):
    """OpenCsiTool — recognises budget exhaustion; else falls to default."""

    id = "opencsitool"
    default_error_handling: dict[str, str] = {}

    def classify(self, payload_text: str, overrides: dict[str, str]) -> str:
        return self.classify_details(payload_text, overrides).action

    def classify_details(
        self,
        payload_text: str,
        overrides: dict[str, str],
    ) -> ErrorClassification:
        # Parse the payload just enough to get message + HTTP status — a JSON
        # object with those fields, or else the raw text as the message.
        message = payload_text or ""
        status: str | None = None
        try:
            data = json.loads(payload_text)
            if isinstance(data, dict):
                message = str(data.get("message", message))
                status = data.get("status")
        except (json.JSONDecodeError, TypeError):
            pass

        msg = message.lower()
        is_budget = "budget" in msg and "exceed" in msg
        is_429 = status == "429" or "429" in msg
        if is_budget or is_429:
            return ErrorClassification(
                action="disable,rotate",
                matched=True,
                resource_exhausted=True,
            )
        return super().classify_details(payload_text, overrides)
