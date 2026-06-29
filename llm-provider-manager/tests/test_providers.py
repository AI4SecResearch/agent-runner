"""Tests for the providers package: error classification + built-in defaults.

Each provider backend has built-in error-handling defaults (code → action);
config overrides merge on top. Providers classify correctly with NO config.
"""
from __future__ import annotations

from llm_provider_manager import providers as providers_mod
from llm_provider_manager.providers.base import extract_signals


# ── extract_signals ───────────────────────────────────────────────

def test_extract_signals_explicit_code():
    s = extract_signals('{"code": "1308", "message": "quota exceeded"}')
    assert s.code == "1308"
    assert s.message == "quota exceeded"


def test_extract_signals_code_from_message_brackets():
    s = extract_signals('{"message": "request failed [1305] please retry"}')
    assert s.code == "1305"
    assert "1305" in s.message


def test_extract_signals_non_json_falls_back_to_message():
    s = extract_signals("raw error text with [429] inside")
    assert s.code == "429"
    assert s.message == "raw error text with [429] inside"


def test_extract_signals_empty_payload():
    s = extract_signals("")
    assert s.code is None
    assert s.message == ""


def test_extract_signals_http_status_3digit():
    s = extract_signals('{"status": "429", "message": "rate limited"}')
    assert s.status == "429"


# ── classify dispatch ─────────────────────────────────────────────

def test_classify_zhipu_builtin_defaults_no_config():
    # zhipu classifies correctly with empty overrides (built-in defaults)
    assert providers_mod.classify("zhipu", '{"code": "1305"}', {}) == "downgrade:false"
    assert providers_mod.classify("zhipu", '{"code": "1308"}', {}) == "rotate_key:true"
    assert providers_mod.classify("zhipu", '{"code": "1310"}', {}) == "rotate_key:true"
    # unknown code → _default
    assert providers_mod.classify("zhipu", '{"code": "9999"}', {}) == "rotate_then_downgrade:true"


def test_classify_zhipu_config_overrides_builtin():
    # config override changes 1308 from rotate_key to downgrade
    result = providers_mod.classify(
        "zhipu", '{"code": "1308"}', {"1308": "downgrade"})
    assert result == "downgrade:false"


def test_classify_unknown_provider_falls_to_default():
    # unregistered provider → DefaultProvider → _default action
    result = providers_mod.classify("unknown", '{"code": "429"}', {})
    assert result == "rotate_then_downgrade:true"


def test_classify_bailian_empty_builtin_uses_config():
    # bailian has empty built-in; config _default=rotate_key → rotate
    result = providers_mod.classify(
        "bailian", '{"code": "429"}', {"_default": "rotate_key"})
    assert result == "rotate_key:true"


def test_classify_bailian_no_config_uses_default_fallback():
    # bailian with no config → DefaultProvider._default = rotate_then_downgrade
    result = providers_mod.classify("bailian", '{"code": "429"}', {})
    assert result == "rotate_then_downgrade:true"


def test_classify_disable_flag():
    # rotate_* → disable=true; downgrade → disable=false
    assert providers_mod.classify("zhipu", '{"code": "1308"}', {}).endswith(":true")
    assert providers_mod.classify("zhipu", '{"code": "1305"}', {}).endswith(":false")


# ── effective_error_handling ──────────────────────────────────────

def test_effective_error_handling_zhipu_no_overrides():
    eff = providers_mod.effective_error_handling("zhipu", {})
    assert eff["1305"] == "downgrade"
    assert eff["1308"] == "rotate_key"
    assert eff["_default"] == "rotate_then_downgrade"


def test_effective_error_handling_zhipu_with_overrides():
    eff = providers_mod.effective_error_handling(
        "zhipu", {"1308": "downgrade", "9999": "rotate_key"})
    assert eff["1308"] == "downgrade"  # overridden
    assert eff["1305"] == "downgrade"  # built-in preserved
    assert eff["9999"] == "rotate_key"  # config-only code added


def test_effective_error_handling_bailian_empty_builtin():
    eff = providers_mod.effective_error_handling("bailian", {"_default": "rotate_key"})
    assert eff == {"_default": "rotate_key"}


def test_effective_error_handling_always_has_default():
    # even with empty builtin + empty config, _default is guaranteed
    eff = providers_mod.effective_error_handling("bailian", {})
    assert "_default" in eff
    assert eff["_default"] == "rotate_then_downgrade"


# ── registry ──────────────────────────────────────────────────────

def test_known_provider_ids():
    ids = providers_mod.known_provider_ids()
    assert "zhipu" in ids
    assert "bailian" in ids
    assert "opencsitool" in ids
    assert "_default" not in ids  # internal fallback, not listed


def test_get_backend_registered_and_fallback():
    assert providers_mod.get_backend("zhipu").id == "zhipu"
    assert providers_mod.get_backend("bailian").id == "bailian"
    # unregistered → DefaultProvider
    assert providers_mod.get_backend("nonexistent").id == "_default"
