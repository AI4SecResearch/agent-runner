"""OpenCsiTool provider backend.

Built-in error code handling is intentionally left empty for now — codes
can be added to ``default_error_handling`` as they are characterized. Until
then, classification falls through to the DefaultProvider ``_default``.
"""

from __future__ import annotations

from .default import DefaultProvider


class OpencsitoolProvider(DefaultProvider):
    """OpenCsiTool — built-in map reserved (currently empty)."""

    id = "opencsitool"
    default_error_handling: dict[str, str] = {}
