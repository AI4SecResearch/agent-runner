from __future__ import annotations

import os


_MANAGED_ENV_NAMES = frozenset(
    {
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "LLM_DEFAULT_MODEL",
        "Z_AI_API_KEY",
    }
)
_MANAGED_ENV_PREFIXES = ("ANTHROPIC_", "LLM_KEY_")


def subprocess_base_environment(
    *,
    additional_managed_names: tuple[str, ...] = (),
) -> dict[str, str]:
    """Copy the host environment without stale Runner/LPM credentials."""
    managed_names = _MANAGED_ENV_NAMES.union(additional_managed_names)
    return {
        name: value
        for name, value in os.environ.items()
        if name not in managed_names
        and not name.startswith(_MANAGED_ENV_PREFIXES)
    }
