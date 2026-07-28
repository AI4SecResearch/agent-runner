"""Default provider backend — the config-driven fallback.

Used for any provider id without a dedicated module. It has no built-in
code→action map and **does not parse the payload**: ``classify`` just
returns the configured ``_default`` action (or the framework default).
This is the honest "I don't know what this error is" backend.

Providers that recognise their own error shapes (zhipu's bracketed codes,
opencsitool's budget text) implement their own ``classify`` with their own
parsing — they don't inherit parsing from here.
"""

from __future__ import annotations

from . import DEFAULT_ACTION, ErrorClassification


class DefaultProvider:
    """Generic fallback: no parsing, just the _default action."""

    id = "_default"
    default_error_handling: dict[str, str] = {}

    def _default_action(self, overrides: dict[str, str]) -> str:
        merged = dict(self.default_error_handling)
        merged.update(overrides)
        merged.setdefault("_default", DEFAULT_ACTION)
        return merged["_default"]

    def classify(self, payload_text: str, overrides: dict[str, str]) -> str:
        return self._default_action(overrides)

    def classify_details(
        self,
        payload_text: str,
        overrides: dict[str, str],
    ) -> ErrorClassification:
        return ErrorClassification(
            action=self._default_action(overrides),
            matched=False,
        )
