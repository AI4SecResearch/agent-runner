"""Zhipu (zhipu.ai / BigModel) provider.

Currently uses the generic code->action mapping: the upstream 4-digit code
(e.g. 1305) is already surfaced by the agents and resolved by extract_signals,
so no provider-specific extraction is needed yet. This module is the place to
specialize zhipu behavior (token refresh, distinct retry semantics, ...) as it
diverges from the default.
"""
from .default import classify  # noqa: F401  (specialize here when needed)
