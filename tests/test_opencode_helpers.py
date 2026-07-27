"""opencode backend helper tests.

Pure-Python unit tests for the invoke-construction helpers in
``agent_runner.backends.opencode`` that have no bash/jq counterpart (so they
don't fit the jq-equivalence suite). Covers ``model_args`` (the
provider-qualification of ``--model``).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from agent_runner.backends.opencode import OpencodeBackend  # noqa: E402
from agent_runner.config import Config  # noqa: E402


# ── model_args: provider-qualification of --model ────────────────────────
# opencode's --model requires "provider/model"; the keypool resolves bare ids,
# so model_args prefixes a bare resolved_model with provider_id. claude's
# --model takes a bare id and is unaffected (covered by its own suite).

def _backend(primary_model: str = "") -> OpencodeBackend:
    return OpencodeBackend(Config(config_overrides={"primary_model": primary_model}))


def test_model_args_bare_resolved_gets_prefixed():
    b = _backend()
    assert b.model_args("primary", resolved_model="glm-5.2",
                        provider_id="bailian") == ["--model", "bailian/glm-5.2"]


def test_model_args_already_qualified_unchanged():
    b = _backend()
    assert b.model_args("primary", resolved_model="bailian/glm-5.2",
                        provider_id="bailian") == ["--model", "bailian/glm-5.2"]


def test_model_args_cross_provider_qualified_unchanged():
    # An already-qualified id keeps its explicit provider even if it differs
    # from the resolved one — the caller chose it on purpose.
    b = _backend()
    assert b.model_args("primary", resolved_model="acme/glm-5.2",
                        provider_id="bailian") == ["--model", "acme/glm-5.2"]


def test_model_args_empty_provider_id_leaves_bare():
    # No provider_id (no key pool) → can't qualify, leave the bare id. The
    # caller is then expected to supply a qualified AR_PRIMARY_MODEL itself.
    b = _backend()
    assert b.model_args("primary", resolved_model="glm-5.2",
                        provider_id="") == ["--model", "glm-5.2"]


def test_model_args_config_fallback_then_prefixed():
    # Empty resolved_model → fall back to config primary_model, then qualify.
    b = _backend(primary_model="glm-5.2")
    assert b.model_args("primary", provider_id="bailian") == [
        "--model", "bailian/glm-5.2",
    ]


def test_model_args_downgrade_config_fallback_then_prefixed():
    b = _backend()
    cfg = Config(config_overrides={"downgrade_model": "glm-5.2-air"})
    assert OpencodeBackend(cfg).model_args(
        "downgrade", provider_id="bailian",
    ) == ["--model", "bailian/glm-5.2-air"]


def test_model_args_empty_everything_returns_nothing():
    # No resolved_model, no config model, no provider_id → no --model emitted.
    b = _backend()
    assert b.model_args("primary", provider_id="bailian") == []


def test_model_args_bare_tier_id_gets_prefixed():
    # tier itself can be a bare model id (the "else" branch); it qualifies too.
    b = _backend()
    assert b.model_args("glm-5.2", provider_id="bailian") == [
        "--model", "bailian/glm-5.2",
    ]
