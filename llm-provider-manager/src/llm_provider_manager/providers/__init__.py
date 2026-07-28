"""Provider backends — pluggable per-provider error classification.

Each provider module knows how to interpret its own error payloads:
  * default_error_handling — built-in code → action map (the provider's
    own defaults; does NOT depend on config files).
  * classify(payload_text, overrides) — parse the payload however the
    provider sees fit, then return an atom strategy string (e.g.
    ``"disable,rotate"``, ``"downgrade"``).

The framework is **blind to payload shape**: ``classify()`` here dispatches
the raw payload text to the backend and lets it parse — no pre-parsing, no
parsing model baked into the dispatch path. ``DefaultProvider`` (the
fallback for unregistered ids) doesn't parse at all; providers that
recognise their errors implement their own ``classify`` with their own
parsing.

The ``errorHandling`` block in providers.jsonc is an *override layer* on
top of each provider's built-in defaults — providers classify correctly
even with an empty/absent config block.

Adding a new provider is a new module + one REGISTRY entry; unregistered
ids fall back to ``DefaultProvider`` (see ``default.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


# ── recovery action vocabulary (output contract) ─────────────────
# Strategies are composable: a comma-joined string of these atoms, applied
# left-to-right in one retry step. ``disable`` (mark current key bad for TTL)
# is a first-class atom, not an implicit side-effect of ``rotate`` — so a
# strategy can rotate to a fresh key WITHOUT disabling (e.g. content-safety
# errors that aren't the key's fault). The model tier is decided by whether
# ``downgrade`` is present (orthogonal to rotate).
ATOMS = ("disable", "rotate", "downgrade")
# Unknown errors (no matching code, falls through to _default) get a cautious
# rotate only: try a different key, but don't disable the current one (the key
# may well be fine — the error is unrecognised) or downgrade the model yet.
DEFAULT_ACTION = "rotate"


@dataclass(frozen=True)
class ErrorClassification:
    """Provider-owned recovery action with conservative semantic evidence."""

    action: str
    matched: bool
    resource_exhausted: bool = False


# ── provider protocol + registry ──────────────────────────────────
# NOTE: ATOMS/DEFAULT_ACTION are defined above and imported by the concrete
# provider modules (default.py, zhipu.py). ``REGISTRY = _build_registry()``
# below imports those modules, so the vocabulary must already be defined
# (same partial-init pattern as agents/__init__.py).

class ProviderBackend(Protocol):
    """The contract every provider backend implements.

    ``classify`` receives the RAW payload text — each provider parses it
    itself (the framework does no pre-parsing).
    """

    id: str
    default_error_handling: dict[str, str]

    def classify(self, payload_text: str, overrides: dict[str, str]) -> str: ...

    def classify_details(
        self,
        payload_text: str,
        overrides: dict[str, str],
    ) -> ErrorClassification: ...


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
    """Full pipeline: dispatch raw payload → backend parses → atom strategy."""
    backend = get_backend(provider_id)
    return backend.classify(payload_text, overrides)


def classify_details(
    provider_id: str,
    payload_text: str,
    overrides: dict[str, str],
) -> ErrorClassification:
    """Classify while preserving whether the provider recognized the error."""
    backend = get_backend(provider_id)
    return backend.classify_details(payload_text, overrides)


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
    merged.setdefault("_default", DEFAULT_ACTION)
    return merged
