"""Tests for config loading + schema validation + JSONC stripping."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from llm_provider_manager import config as config_mod
from llm_provider_manager.agents.opencode import opencode_entry_id_for
from llm_provider_manager.config import (
    StrictConfigError,
    check_permissions,
    looks_unfilled,
    parse_text,
)
from llm_provider_manager.schema import Config, Key, Model, Provider


def test_parse_symmetric_and_asymmetric(sample_config_file: Path):
    cfg = config_mod.load(sample_config_file)
    assert cfg.settings.rotate_every == 3
    assert cfg.settings.disable_ttl_hours == 1
    assert len(cfg.providers) == 2
    zhipu = cfg.provider_by_id("zhipu")
    assert zhipu.type == "symmetric"
    assert zhipu.default_key_id() == "main"
    assert {m.id for m in zhipu.all_models()} == {"glm-5.2", "glm-5-turbo"}
    # symmetric: models shared regardless of key
    assert {m.id for m in zhipu.models_for_key("backup")} == {"glm-5.2", "glm-5-turbo"}
    bailian = cfg.provider_by_id("bailian")
    assert bailian.type == "asymmetric"
    assert {m.id for m in bailian.models_for_key("account-b")} == {"glm-4.6"}


def test_opencode_entry_id_for_uses_provider_shape() -> None:
    model = Model("model", "Model", 4096, 1024)
    symmetric = Provider(
        id="symmetric",
        type="symmetric",
        display_name="Symmetric",
        base_urls={"openai": "https://example.test/v1"},
        keys=[Key(id="main", key="secret")],
        models=[model],
    )
    asymmetric = Provider(
        id="asymmetric",
        type="asymmetric",
        display_name="Asymmetric",
        base_urls={"openai": "https://example.test/v1"},
        keys=[Key(id="tenant", key="secret", models=[model])],
    )

    assert opencode_entry_id_for(symmetric, "main") == "symmetric"
    assert (
        opencode_entry_id_for(asymmetric, "tenant")
        == "asymmetric-tenant"
    )


def test_jsonc_comments_stripped(tmp_path: Path):
    text = """{
      // a line comment
      "settings": {"rotateEvery": 2 /* block */, "disableTtlHours": 5},
      "providers": [
        { "id": "p1", "type": "symmetric", "displayName": "P1",
          "baseURLs": {"openai": "https://x"},
          "keys": [{"id": "k1", "key": "secret-with-//-inside"}],
          "models": [{"id": "m", "displayName": "M", "context": 1, "output": 1}] }
      ]
    }"""
    cfg = parse_text(text)
    assert cfg.settings.rotate_every == 2
    # the // inside the string literal must be preserved, not treated as a comment
    assert cfg.providers[0].keys[0].key == "secret-with-//-inside"


def test_strict_mode_rejects_unknown_top_level_fields_only_when_enabled():
    text = """{
      "unexpected": true,
      "providers": [{
        "id": "p", "type": "symmetric",
        "baseURLs": {"openai": "https://x"},
        "keys": [{"id": "k", "key": "secret"}],
        "models": [{"id": "m", "context": 1, "output": 1}]
      }]
    }"""

    assert parse_text(text).providers[0].id == "p"
    with pytest.raises(StrictConfigError) as captured:
        parse_text(text, strict=True)

    assert captured.value.code == "unknown_fields"
    assert captured.value.path == "$"


@pytest.mark.parametrize(
    ("case", "code", "path"),
    [
        (
            "settings_type",
            "expected_non_negative_integer",
            "settings.rotateEvery",
        ),
        ("provider_field", "unknown_fields", "providers[0]"),
        ("duplicate_key", "duplicate_id", "providers[0] Key id"),
        (
            "model_output",
            "expected_positive_integer",
            "providers[0].models[0].output",
        ),
        ("duplicate_model", "duplicate_id", "providers[0].models Model id"),
        (
            "primary_reference",
            "invalid_reference",
            "providers[0].primaryModel",
        ),
        (
            "error_handling",
            "invalid_error_handling",
            "providers[0].errorHandling",
        ),
        (
            "model_location",
            "invalid_model_location",
            "providers[0].keys[0].models",
        ),
        ("default_field", "unknown_fields", "default"),
    ],
)
def test_strict_mode_rejects_noncanonical_schema(
    case: str,
    code: str,
    path: str,
):
    document = {
        "settings": {"rotateEvery": 5, "disableTtlHours": 5},
        "default": {"provider": "p", "key": "k", "agent": "opencode"},
        "providers": [{
            "id": "p",
            "type": "symmetric",
            "baseURLs": {"openai": "https://x"},
            "defaultKey": "k",
            "keys": [{"id": "k", "key": "secret"}],
            "models": [
                {"id": "m", "context": 4096, "output": 1024},
                {"id": "m2", "context": 4096, "output": 1024},
            ],
            "primaryModel": "m",
            "downgradeModel": "m2",
            "errorHandling": {"_default": "disable,rotate,downgrade"},
        }],
    }
    provider = document["providers"][0]
    if case == "settings_type":
        document["settings"]["rotateEvery"] = "5"
    elif case == "provider_field":
        provider["unexpected"] = True
    elif case == "duplicate_key":
        provider["keys"].append({"id": "k", "key": "other-secret"})
    elif case == "model_output":
        provider["models"][0]["output"] = 0
    elif case == "duplicate_model":
        provider["models"][1]["id"] = "m"
    elif case == "primary_reference":
        provider["primaryModel"] = "missing"
    elif case == "error_handling":
        provider["errorHandling"]["_default"] = "rotate,disable"
    elif case == "model_location":
        provider["keys"][0]["models"] = [
            {"id": "wrong-level", "context": 1, "output": 1}
        ]
    else:
        document["default"]["unexpected"] = True

    with pytest.raises(StrictConfigError) as captured:
        parse_text(json.dumps(document), strict=True)

    assert captured.value.code == code
    assert captured.value.path == path


def test_strict_mode_rejects_unclosed_jsonc_comment_only_when_enabled():
    text = """{
      "providers": [{
        "id": "p", "type": "symmetric",
        "baseURLs": {"openai": "https://x"},
        "keys": [{"id": "k", "key": "secret"}],
        "models": [{"id": "m", "context": 1, "output": 1}]
      }]
    } /* unclosed"""

    assert parse_text(text).providers[0].id == "p"
    with pytest.raises(StrictConfigError) as captured:
        parse_text(text, strict=True)

    assert captured.value.code == "invalid_jsonc"
    assert captured.value.path == "$"


def test_load_forwards_strict_mode_to_schema_validation(tmp_path: Path):
    path = tmp_path / "providers.jsonc"
    path.write_text(
        """{
          "unexpected": true,
          "providers": [{
            "id": "p", "type": "symmetric",
            "baseURLs": {"openai": "https://x"},
            "keys": [{"id": "k", "key": "secret"}],
            "models": [{"id": "m", "context": 1, "output": 1}]
          }]
        }""",
        encoding="utf-8",
    )

    assert config_mod.load(path).providers[0].id == "p"
    with pytest.raises(StrictConfigError) as captured:
        config_mod.load(path, strict=True)

    assert captured.value.code == "unknown_fields"
    assert captured.value.path == "$"


def test_strict_error_path_never_contains_provider_values():
    secret = "protocol-name-is-a-secret"
    text = json.dumps(
        {
            "providers": [
                {
                    "id": "p",
                    "type": "symmetric",
                    "baseURLs": {secret: ""},
                    "keys": [{"id": "k", "key": secret}],
                    "models": [
                        {"id": "m", "context": 1, "output": 1}
                    ],
                }
            ]
        }
    )

    with pytest.raises(StrictConfigError) as captured:
        parse_text(text, strict=True)

    assert secret not in captured.value.path
    assert secret not in str(captured.value)


def test_symmetric_key_must_not_have_models():
    with pytest.raises(ValueError, match="must not declare 'models'"):
        parse_text("""{
          "providers": [{
            "id": "p", "type": "symmetric", "displayName": "P",
            "baseURLs": {"openai": "https://x"},
            "keys": [{"id": "k", "key": "v", "models": []}],
            "models": [{"id": "m", "displayName": "M", "context": 1, "output": 1}]
          }]
        }""")


def test_asymmetric_key_must_have_models():
    with pytest.raises(ValueError, match="must declare its own 'models'"):
        parse_text("""{
          "providers": [{
            "id": "p", "type": "asymmetric", "displayName": "P",
            "baseURLs": {"openai": "https://x"},
            "keys": [{"id": "k", "key": "v"}]
          }]
        }""")


@pytest.mark.parametrize("field", ["primaryModel", "downgradeModel"])
def test_asymmetric_provider_model_overrides_rejected(
    sample_config_dict: dict, field: str
):
    provider = sample_config_dict["providers"][1]
    provider[field] = provider["keys"][0]["models"][0]["id"]

    with pytest.raises(
        ValueError,
        match=rf"asymmetric provider 'bailian' must not declare provider-level '{field}'",
    ):
        Config.from_dict(sample_config_dict)


def test_asymmetric_key_model_overrides_allowed(sample_config_dict: dict):
    key = sample_config_dict["providers"][1]["keys"][0]
    key["primaryModel"] = "glm-5.2"
    key["downgradeModel"] = "glm-5.2"

    parsed_key = Config.from_dict(sample_config_dict).provider_by_id("bailian").keys[0]

    assert parsed_key.primary_model == "glm-5.2"
    assert parsed_key.downgrade_model == "glm-5.2"


def test_symmetric_provider_model_overrides_allowed(sample_config_dict: dict):
    provider = sample_config_dict["providers"][0]
    provider["primaryModel"] = "glm-5-turbo"
    provider["downgradeModel"] = "glm-5.2"

    parsed_provider = Config.from_dict(sample_config_dict).provider_by_id("zhipu")

    assert parsed_provider.primary_model == "glm-5-turbo"
    assert parsed_provider.downgrade_model == "glm-5.2"


def test_duplicate_provider_ids_rejected():
    with pytest.raises(ValueError, match="duplicate provider ids"):
        parse_text("""{
          "providers": [
            {"id":"p","type":"symmetric","displayName":"P","baseURLs":{"openai":"https://x"},
             "keys":[{"id":"k","key":"v"}],"models":[{"id":"m","displayName":"M","context":1,"output":1}]},
            {"id":"p","type":"symmetric","displayName":"P2","baseURLs":{"openai":"https://y"},
             "keys":[{"id":"k","key":"v"}],"models":[{"id":"m","displayName":"M","context":1,"output":1}]}
          ]
        }""")


def test_default_key_validated():
    with pytest.raises(ValueError, match="defaultKey 'nope' not among keys"):
        parse_text("""{
          "providers": [{
            "id":"p","type":"symmetric","displayName":"P","defaultKey":"nope",
            "baseURLs":{"openai":"https://x"},
            "keys":[{"id":"k","key":"v"}],
            "models":[{"id":"m","displayName":"M","context":1,"output":1}]
          }]
        }""")


def test_default_agent_validated():
    with pytest.raises(ValueError, match="default.agent 'nope' not a registered agent"):
        parse_text("""{
          "default": {"agent": "nope", "provider": "p"},
          "providers": [{
            "id":"p","type":"symmetric","displayName":"P",
            "baseURLs":{"openai":"https://x"},
            "keys":[{"id":"k","key":"v"}],
            "models":[{"id":"m","displayName":"M","context":1,"output":1}]
          }]
        }""")


def test_default_agent_optional():
    cfg = parse_text("""{
      "default": {"provider": "p"},
      "providers": [{
        "id":"p","type":"symmetric","displayName":"P",
        "baseURLs":{"openai":"https://x"},
        "keys":[{"id":"k","key":"v"}],
        "models":[{"id":"m","displayName":"M","context":1,"output":1}]
      }]
    }""")
    assert cfg.default.agent is None  # omitted → None (use falls back to registry default)


def test_agent_blacklist_unknown_agent_rejected():
    with pytest.raises(ValueError, match="agentBlacklist has unknown agents"):
        parse_text("""{
          "providers": [{
            "id":"p","type":"symmetric","displayName":"P",
            "baseURLs":{"openai":"https://x"},
            "keys":[{"id":"k","key":"v","agentBlacklist":["nonexistent"]}],
            "models":[{"id":"m","displayName":"M","context":1,"output":1}]
          }]
        }""")


def test_looks_unfilled_detects_replace_me(sample_config_dict: dict):
    cfg = Config.from_dict(sample_config_dict)
    assert not looks_unfilled(cfg)
    cfg.providers[0].keys[0].key = "sk-REPLACE-ME"
    assert looks_unfilled(cfg)


def test_check_permissions_warns_on_world_readable(tmp_path: Path):
    p = tmp_path / "providers.jsonc"
    p.write_text("{}", encoding="utf-8")
    os.chmod(p, 0o644)
    warnings = check_permissions(p)
    assert len(warnings) == 1
    assert "chmod 600" in warnings[0]


def test_usable_for_protocol(sample_config_file: Path):
    cfg = config_mod.load(sample_config_file)
    zhipu = cfg.provider_by_id("zhipu")
    assert zhipu.usable_for("anthropic") and zhipu.usable_for("openai")
    bailian = cfg.provider_by_id("bailian")
    assert not bailian.usable_for("anthropic") and bailian.usable_for("openai")
