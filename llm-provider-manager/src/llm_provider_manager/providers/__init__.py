"""Provider backends — pluggable per-provider error classification.

Each provider module knows how to interpret its own error payloads:
  * default_error_handling — built-in code → action map (the provider's
    own defaults; does NOT depend on config files).
  * classify(signals, overrides) — merge config overrides over built-in
    defaults, look up the extracted code, return ``"action:disable"``.

The ``errorHandling`` block in providers.jsonc is an *override layer* on
top of these built-in defaults — providers classify correctly even with
an empty/absent config block.

Mirrors agent-runner's providers/<name>.py pattern: adding a new provider
is a new module + one REGISTRY entry. Unregistered provider ids fall back
to DefaultProvider.
"""

from __future__ import annotations

from typing import Protocol

from .base import Signals, extract_signals


class ProviderBackend(Protocol):
    """The contract every provider backend implements."""

    id: str
    default_error_handling: dict[str, str]

    def classify(self, signals: Signals, overrides: dict[str, str]) -> str: ...


def _build_registry() -> dict[str, ProviderBackend]:
    from .bailian import BailianProvider
    from .default import DefaultProvider
    from .opencsitool import OpencsitoolProvider
    from .zhipu import ZhipuProvider

    return {
        "zhipu": ZhipuProvider(),
        "bailian": BailianProvider(),
        "opencsitool": OpencsitoolProvider(),
        # unregistered ids fall through to DefaultProvider via get_backend
        "_default": DefaultProvider(),
    }


REGISTRY: dict[str, ProviderBackend] = _build_registry()


def get_backend(provider_id: str) -> ProviderBackend:
    """Look up a provider backend by id; fall back to DefaultProvider."""
    return REGISTRY.get(provider_id, REGISTRY["_default"])


def known_provider_ids() -> list[str]:
    return [k for k in REGISTRY if k != "_default"]


def classify(
    provider_id: str, payload_text: str, overrides: dict[str, str]
) -> str:
    """Full pipeline: parse payload → dispatch to backend → 'action:disable'."""
    signals = extract_signals(payload_text)
    backend = get_backend(provider_id)
    return backend.classify(signals, overrides)


def effective_error_handling(
    provider_id: str, overrides: dict[str, str]
) -> dict[str, str]:
    """The merged code → action table: built-in defaults + config overrides.

    Used by ``list`` to show the effective policy. The ``_default`` entry is
    always present (DefaultProvider guarantees a fallback action).
    """
    backend = get_backend(provider_id)
    merged = dict(backend.default_error_handling)
    merged.update(overrides)
    merged.setdefault("_default", "rotate_then_downgrade")
    return merged
