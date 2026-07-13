"""Bailian (百炼 / DashScope) provider backend.

Built-in error code handling is intentionally left empty for now — bailian
has no well-known upstream code in the manager's current usage, so it falls
through to the DefaultProvider ``_default`` action. Codes can be added to
``default_error_handling`` as they are characterized.
"""

from __future__ import annotations

from .default import DefaultProvider


class BailianProvider(DefaultProvider):
    """Bailian — built-in map reserved (currently empty)."""

    id = "bailian"
    default_error_handling: dict[str, str] = {}
