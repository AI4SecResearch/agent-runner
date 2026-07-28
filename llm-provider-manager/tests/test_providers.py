"""Tests for the providers package: error classification + built-in defaults.

Each provider backend has built-in error-handling defaults (code → action);
config overrides merge on top. Providers classify correctly with NO config.
"""
from __future__ import annotations

from llm_provider_manager import providers as providers_mod
from llm_provider_manager.providers.zhipu import extract_signals


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
    assert providers_mod.classify("zhipu", '{"code": "1301"}', {}) == "rotate,downgrade"
    assert providers_mod.classify("zhipu", '{"code": "1305"}', {}) == "downgrade"
    assert providers_mod.classify("zhipu", '{"code": "1308"}', {}) == "disable,rotate"
    assert providers_mod.classify("zhipu", '{"code": "1310"}', {}) == "disable,rotate"
    # unknown code → _default (rotate only, no disable)
    assert providers_mod.classify("zhipu", '{"code": "9999"}', {}) == "rotate"


def test_classify_details_marks_known_quota_as_resource_exhausted():
    classification = providers_mod.classify_details(
        "zhipu",
        '{"code": "1308"}',
        {},
    )

    assert classification.action == "disable,rotate"
    assert classification.matched is True
    assert classification.resource_exhausted is True


def test_classify_details_keeps_unknown_failure_unclassified():
    classification = providers_mod.classify_details(
        "zhipu",
        '{"code": "9999"}',
        {},
    )

    assert classification.action == "rotate"
    assert classification.matched is False
    assert classification.resource_exhausted is False


def test_classify_zhipu_1301_does_not_disable():
    # content-safety — rotate+downgrade but the key isn't bad, so no disable atom
    result = providers_mod.classify("zhipu", '{"code": "1301"}', {})
    assert "rotate" in result and "downgrade" in result
    assert "disable" not in result


def test_classify_zhipu_config_overrides_builtin():
    # config override changes 1308 from disable,rotate to downgrade
    result = providers_mod.classify(
        "zhipu", '{"code": "1308"}', {"1308": "downgrade"})
    assert result == "downgrade"


def test_classify_unknown_provider_falls_to_default():
    # unregistered provider → DefaultProvider → _default action (rotate only)
    result = providers_mod.classify("unknown", '{"code": "429"}', {})
    assert result == "rotate"


def test_classify_bailian_empty_builtin_uses_config():
    # bailian has empty built-in; config _default=disable,rotate → disable+rotate
    result = providers_mod.classify(
        "bailian", '{"code": "429"}', {"_default": "disable,rotate"})
    assert result == "disable,rotate"


def test_classify_bailian_no_config_uses_default_fallback():
    # bailian with no config → DefaultProvider._default (rotate only)
    result = providers_mod.classify("bailian", '{"code": "429"}', {})
    assert result == "rotate"


def test_classify_disable_atom_present_or_absent():
    # disable is encoded by the `disable` atom being in the strategy, not a flag
    assert "disable" in providers_mod.classify("zhipu", '{"code": "1308"}', {})
    assert "disable" not in providers_mod.classify("zhipu", '{"code": "1305"}', {})
    assert "disable" not in providers_mod.classify("zhipu", '{"code": "1301"}', {})


# ── effective_error_handling ──────────────────────────────────────

def test_effective_error_handling_zhipu_no_overrides():
    eff = providers_mod.effective_error_handling("zhipu", {})
    assert eff["1301"] == "rotate,downgrade"
    assert eff["1305"] == "downgrade"
    assert eff["1308"] == "disable,rotate"
    assert eff["_default"] == "rotate"


def test_effective_error_handling_zhipu_with_overrides():
    eff = providers_mod.effective_error_handling(
        "zhipu", {"1308": "downgrade", "9999": "disable,rotate"})
    assert eff["1308"] == "downgrade"           # overridden
    assert eff["1305"] == "downgrade"            # built-in preserved
    assert eff["9999"] == "disable,rotate"       # config-only code added


def test_effective_error_handling_bailian_empty_builtin():
    eff = providers_mod.effective_error_handling("bailian", {"_default": "disable,rotate"})
    assert eff == {"_default": "disable,rotate"}


def test_effective_error_handling_always_has_default():
    # even with empty builtin + empty config, _default is guaranteed
    eff = providers_mod.effective_error_handling("bailian", {})
    assert "_default" in eff
    assert eff["_default"] == "rotate"


# ── opencsitool: budget / 429 pattern matching ────────────────────

def test_classify_opencsitool_budget_exceeded_disables_and_rotates():
    # real-world opencsitool payload: free text, no [code]/JSON status field
    msg = ("API Error: Request rejected (429) · Budget has been exceeded! "
           "Current cost: 20071198.0, Max budget: 20000000.0")
    assert providers_mod.classify(
        "opencsitool", f'{{"message":"{msg}"}}', {}) == "disable,rotate"


def test_classify_opencsitool_plain_429_disables_and_rotates():
    # HTTP 429 in the text (without the budget keyword) still counts as quota-class
    assert providers_mod.classify(
        "opencsitool", '{"message":"Request rejected (429)"}', {}) == "disable,rotate"
    # or as a structured status field
    assert providers_mod.classify(
        "opencsitool", '{"status":"429","message":"rate limited"}', {}) == "disable,rotate"


def test_classify_details_marks_opencsitool_rate_limit_as_resource_exhausted():
    classification = providers_mod.classify_details(
        "opencsitool",
        '{"status":"429","message":"rate limited"}',
        {},
    )

    assert classification.action == "disable,rotate"
    assert classification.matched is True
    assert classification.resource_exhausted is True


def test_classify_opencsitool_other_error_falls_to_default():
    # an unrecognised error → DefaultProvider._default (rotate only)
    assert providers_mod.classify(
        "opencsitool", '{"message":"something else went wrong"}', {}) == "rotate"


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
