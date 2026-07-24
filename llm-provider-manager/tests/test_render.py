"""Tests for agent config rendering (ClaudeAgent + OpencodeAgent).

Two modes: --template (default, {env:} placeholders) and --inline
(selection with real keys baked in).
"""
from __future__ import annotations
import json
from pathlib import Path

import pytest

from llm_provider_manager import agents as agents_mod
from llm_provider_manager import config as config_mod
from llm_provider_manager import use as use_mod


# ── Claude settings.json ──────────────────────────────────────────

def test_claude_render_config_fresh_uses_template(sample_config_file: Path, tmp_path: Path):
    """Fresh render → DEFAULT_SETTINGS_TEMPLATE content."""
    p = tmp_path / "settings.json"
    cfg = config_mod.load(sample_config_file)
    agent = agents_mod.get_agent("claude")
    rendered, skipped = agent.render_config(cfg, str(p))
    del rendered  # unused
    assert skipped == []
    data = json.loads(p.read_text())
    # template content present
    assert data["env"]["API_TIMEOUT_MS"] == "3000000"
    assert data["env"]["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert data["env"]["CLAUDE_CODE_ATTRIBUTION_HEADER"] == "0"
    assert "Bash(git status *)" in data["permissions"]["allow"]
    assert "Read" in data["permissions"]["allow"]
    # no ANTHROPIC_* keys (they come from `use` at runtime)
    for k in data.get("env", {}):
        assert not k.startswith("ANTHROPIC")


# ── opencode opencode.json ────────────────────────────────────────

def test_opencode_render_bakes_providers_with_env_placeholders(sample_config_file: Path, tmp_path: Path):
    cfg = config_mod.load(sample_config_file)
    agent = agents_mod.get_agent("opencode")
    p = tmp_path / "opencode.json"
    out, skipped = agent.render_config(cfg, str(p))
    assert skipped == []
    assert out["$schema"] == "https://opencode.ai/config.json"
    assert out["permission"]["*"] == "ask"  # from template
    assert out["model"] == "{env:LLM_DEFAULT_MODEL}"

    zhipu = out["provider"]["zhipu"]
    assert zhipu["npm"] == "@ai-sdk/openai-compatible"   # openai preferred over anthropic
    assert zhipu["options"]["baseURL"] == "https://zhipu/openai"
    assert zhipu["options"]["apiKey"] == "{env:LLM_KEY_ZHIPU}"
    assert set(zhipu["models"].keys()) == {"glm-5.2", "glm-5-turbo"}
    assert zhipu["models"]["glm-5.2"]["limit"] == {"context": 1000000, "output": 131072}

    bailian_a = out["provider"]["bailian-account-a"]
    # asymmetric: one entry per key, only that key's models, per-key env var
    assert set(bailian_a["models"].keys()) == {"glm-5.2"}
    assert bailian_a["options"]["apiKey"] == "{env:LLM_KEY_BAILIAN_ACCOUNT_A}"
    bailian_b = out["provider"]["bailian-account-b"]
    assert set(bailian_b["models"].keys()) == {"glm-4.6"}
    assert bailian_b["options"]["apiKey"] == "{env:LLM_KEY_BAILIAN_ACCOUNT_B}"


def test_opencode_provider_entries_support_one_runtime_key_reference(
    sample_config_file: Path,
) -> None:
    cfg = config_mod.load(sample_config_file)
    agent = agents_mod.get_agent("opencode")

    providers, skipped = agent.provider_entries_for(
        cfg,
        api_key_reference="{env:Z_AI_API_KEY}",
    )

    assert skipped == []
    assert providers["zhipu"]["options"]["apiKey"] == "{env:Z_AI_API_KEY}"
    assert (
        providers["bailian-account-a"]["options"]["apiKey"]
        == "{env:Z_AI_API_KEY}"
    )
    assert set(providers["zhipu"]["models"]) == {"glm-5.2", "glm-5-turbo"}
    assert set(providers["bailian-account-a"]["models"]) == {"glm-5.2"}


def test_opencode_provider_entries_exclude_blacklisted_keys(
    tmp_path: Path,
) -> None:
    sample = {
        "providers": [
            {
                "id": "asymmetric",
                "type": "asymmetric",
                "baseURLs": {"openai": "https://example.test"},
                "keys": [
                    {
                        "id": "blocked",
                        "key": "blocked-secret",
                        "agentBlacklist": ["opencode"],
                        "models": [
                            {"id": "blocked-model", "context": 1, "output": 1}
                        ],
                    },
                    {
                        "id": "usable",
                        "key": "usable-secret",
                        "models": [
                            {"id": "usable-model", "context": 1, "output": 1}
                        ],
                    },
                ],
            }
        ]
    }
    sample_path = tmp_path / "providers.jsonc"
    sample_path.write_text(json.dumps(sample), encoding="utf-8")
    config = config_mod.load(sample_path)

    providers, skipped = agents_mod.get_agent(
        "opencode"
    ).provider_entries_for(
        config,
        api_key_reference="{env:Z_AI_API_KEY}",
    )

    assert set(providers) == {"asymmetric-usable"}
    assert skipped == []


def test_opencode_provider_entries_can_require_one_protocol(
    sample_config_file: Path,
) -> None:
    config = config_mod.load(sample_config_file)

    providers, skipped = agents_mod.get_agent(
        "opencode"
    ).provider_entries_for(
        config,
        api_key_reference="{env:Z_AI_API_KEY}",
        required_protocol="openai",
    )

    assert "zhipu" in providers
    assert all(
        block["options"]["baseURL"].startswith("https://")
        for block in providers.values()
    )
    assert all(
        provider.usable_for("openai")
        for provider in config.providers
        if provider.id not in skipped
    )


def test_opencode_provider_entries_reject_canonical_id_collision(
    tmp_path: Path,
) -> None:
    sample = {
        "providers": [
            {
                "id": "a-b",
                "type": "symmetric",
                "baseURLs": {"openai": "https://symmetric.test"},
                "keys": [{"id": "key", "key": "secret"}],
                "models": [
                    {
                        "id": "symmetric-model",
                        "context": 1,
                        "output": 1,
                    }
                ],
            },
            {
                "id": "a",
                "type": "asymmetric",
                "baseURLs": {"openai": "https://asymmetric.test"},
                "keys": [
                    {
                        "id": "b",
                        "key": "secret",
                        "models": [
                            {
                                "id": "asymmetric-model",
                                "context": 1,
                                "output": 1,
                            }
                        ],
                    }
                ],
            },
        ]
    }
    sample_path = tmp_path / "providers.jsonc"
    sample_path.write_text(json.dumps(sample), encoding="utf-8")
    cfg = config_mod.load(sample_path)

    with pytest.raises(ValueError, match="entry id collision"):
        agents_mod.get_agent("opencode").provider_entries_for(
            cfg,
            api_key_reference="{env:Z_AI_API_KEY}",
        )


def test_opencode_render_default_model(sample_config_file: Path, tmp_path: Path):
    cfg = config_mod.load(sample_config_file)
    agent = agents_mod.get_agent("opencode")
    out, _ = agent.render_config(cfg, str(tmp_path / "opencode.json"))
    assert out["model"] == "{env:LLM_DEFAULT_MODEL}"


def test_opencode_render_skips_provider_without_relevant_baseurl(tmp_path: Path):
    sample = {
        "providers": [
            {"id": "openai-only", "type": "symmetric", "displayName": "O",
             "baseURLs": {"openai": "https://x"},
             "keys": [{"id": "k", "key": "v"}],
             "models": [{"id": "m", "displayName": "M", "context": 1, "output": 1}]},
            {"id": "weird-only", "type": "symmetric", "displayName": "W",
             "baseURLs": {"gemini": "https://g"},
             "keys": [{"id": "k", "key": "v"}],
             "models": [{"id": "m", "displayName": "M", "context": 1, "output": 1}]},
        ]
    }
    sample_path = tmp_path / "p.jsonc"
    sample_path.write_text(json.dumps(sample))
    cfg = config_mod.load(sample_path)
    agent = agents_mod.get_agent("opencode")
    out, skipped = agent.render_config(cfg, str(tmp_path / "opencode.json"))
    assert "openai-only" in out["provider"]
    assert "weird-only" not in out["provider"]
    assert skipped == ["weird-only"]


def test_opencode_single_key_asymmetric_uses_suffix(tmp_path: Path):
    """A single-key asymmetric provider uses <provider>-<keyid> entry id,
    consistent with multi-key asymmetric providers."""
    sample = {
        "providers": [
            {"id": "bailian", "type": "asymmetric", "displayName": "百炼",
             "baseURLs": {"openai": "https://bailian/o"},
             "keys": [{"id": "grq", "key": "sk-x",
                       "models": [{"id": "glm-5.2", "displayName": "GLM-5.2",
                                   "context": 1000000, "output": 131072}]}]},
        ]
    }
    sample_path = tmp_path / "p.jsonc"
    sample_path.write_text(json.dumps(sample))
    cfg = config_mod.load(sample_path)
    agent = agents_mod.get_agent("opencode")
    out, skipped = agent.render_config(cfg, str(tmp_path / "opencode.json"))
    assert skipped == []
    assert list(out["provider"]) == ["bailian-grq"]
    assert out["provider"]["bailian-grq"]["name"] == "百炼 (key: grq)"
    assert out["provider"]["bailian-grq"]["options"]["apiKey"] == "{env:LLM_KEY_BAILIAN_GRQ}"


def test_opencode_multi_key_asymmetric_keeps_suffix(sample_config_file: Path, tmp_path: Path):
    """Multi-key asymmetric providers still use the <provider>-<keyid> suffix
    to distinguish entries with different model sets."""
    cfg = config_mod.load(sample_config_file)
    agent = agents_mod.get_agent("opencode")
    out, _ = agent.render_config(cfg, str(tmp_path / "opencode.json"))
    # bailian in the sample has 2 keys (account-a, account-b) → suffixed
    assert "bailian-account-a" in out["provider"]
    assert "bailian-account-b" in out["provider"]


def test_opencode_render_fresh_uses_template(sample_config_file: Path, tmp_path: Path):
    """Fresh render → DEFAULT_CONFIG_TEMPLATE's permissions present."""
    cfg = config_mod.load(sample_config_file)
    agent = agents_mod.get_agent("opencode")
    out, _ = agent.render_config(cfg, str(tmp_path / "opencode.json"))
    # template permissions present
    assert out["$schema"] == "https://opencode.ai/config.json"
    assert out["permission"]["*"] == "ask"
    assert out["permission"]["read"] == "allow"
    assert "git diff *" in out["permission"]["bash"]
    # providers + model also baked on top
    assert "zhipu" in out["provider"]
    assert out["model"] == "{env:LLM_DEFAULT_MODEL}"


# ── inline mode ───────────────────────────────────────────────────

def test_claude_inline_bakes_anthropic_vars(sample_config_file: Path, tmp_path: Path):
    """Inline claude → ANTHROPIC_* baked into env block."""
    cfg = config_mod.load(sample_config_file)
    plan = use_mod.build_use_plan(cfg, agent_id="claude")
    agent = agents_mod.get_agent("claude")
    out, _ = agent.render_config(cfg, str(tmp_path / "settings.json"), selection=plan)
    env = out["env"]
    assert env["ANTHROPIC_BASE_URL"] == "https://zhipu/anthropic"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-zhipu-main"
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "glm-5.2"
    assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "glm-5.2"
    # template defaults preserved
    assert env["API_TIMEOUT_MS"] == "3000000"


def test_opencode_inline_bakes_real_keys(sample_config_file: Path, tmp_path: Path):
    """Inline opencode → real apiKey values, no {env:} placeholders."""
    cfg = config_mod.load(sample_config_file)
    plan = use_mod.build_use_plan(cfg, agent_id="opencode")
    agent = agents_mod.get_agent("opencode")
    out, _ = agent.render_config(cfg, str(tmp_path / "opencode.json"), selection=plan)
    # zhipu (symmetric) — real key, not placeholder
    assert out["provider"]["zhipu"]["options"]["apiKey"] == "sk-zhipu-main"
    # bailian (asymmetric multi-key) — real keys per entry
    assert out["provider"]["bailian-account-a"]["options"]["apiKey"] == "sk-bailian-a"
    assert out["provider"]["bailian-account-b"]["options"]["apiKey"] == "sk-bailian-b"
    # model is real, not placeholder
    assert out["model"] == "zhipu/glm-5.2"
    assert "{env:" not in out["model"]


def test_opencode_inline_with_provider_selection(sample_config_file: Path, tmp_path: Path):
    """Inline opencode with --provider bailian → default model points to bailian."""
    cfg = config_mod.load(sample_config_file)
    plan = use_mod.build_use_plan(
        cfg, agent_id="opencode", provider_id="bailian", key_id="account-b")
    agent = agents_mod.get_agent("opencode")
    out, _ = agent.render_config(cfg, str(tmp_path / "opencode.json"), selection=plan)
    assert out["model"] == "bailian-account-b/glm-4.6"
    # all providers still have real keys
    assert out["provider"]["zhipu"]["options"]["apiKey"] == "sk-zhipu-main"
