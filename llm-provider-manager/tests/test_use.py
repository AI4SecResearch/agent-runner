"""Tests for use.py: agent-scoped manual provider/key selection.

``use`` is agent-scoped: only the selected agent's env vars are exported.
The agent's own ``exports_for`` decides what to emit.
"""
from __future__ import annotations
import re
from pathlib import Path

import pytest

from llm_provider_manager import config as config_mod
from llm_provider_manager import use as use_mod
from llm_provider_manager.agents.claude import (
    ANTHROPIC_AUTH_TOKEN_VAR,
    ANTHROPIC_BASE_URL_VAR,
    ANTHROPIC_OPUS_MODEL_VAR,
    ANTHROPIC_SONNET_MODEL_VAR,
)
from llm_provider_manager.agents.opencode import (
    DEFAULT_MODEL_VAR,
    key_var,
)


# ── default agent (claude) ────────────────────────────────────────

def test_use_default_agent_is_claude(sample_config_file: Path):
    cfg = config_mod.load(sample_config_file)
    plan = use_mod.build_use_plan(cfg)  # no agent → config default.agent (claude)
    assert plan.agent_id == "claude"
    assert plan.provider_id == "zhipu"
    assert plan.key_id == "main"
    # Claude vars only (zhipu has anthropic baseURL; main not blacklisted)
    assert plan.exports[ANTHROPIC_AUTH_TOKEN_VAR] == "sk-zhipu-main"
    assert plan.exports[ANTHROPIC_BASE_URL_VAR] == "https://zhipu/anthropic"
    assert plan.exports[ANTHROPIC_OPUS_MODEL_VAR] == "glm-5.2"
    assert plan.exports[ANTHROPIC_SONNET_MODEL_VAR] == "glm-5.2"
    assert plan.blocked is False
    # NO opencode vars when agent=claude
    assert key_var("zhipu") not in plan.exports
    assert DEFAULT_MODEL_VAR not in plan.exports


def test_use_no_default_falls_back_to_first_provider(tmp_path: Path):
    cfg_dict = {"providers": [{
        "id": "z", "type": "symmetric", "displayName": "Z",
        "baseURLs": {"anthropic": "https://z/a"},
        "keys": [{"id": "k1", "key": "v1"}],
        "models": [{"id": "m", "displayName": "M", "context": 1, "output": 1}],
    }]}
    p = tmp_path / "c.jsonc"
    p.write_text(__import__("json").dumps(cfg_dict))
    cfg = config_mod.load(p)
    plan = use_mod.build_use_plan(cfg)
    assert plan.provider_id == "z"
    assert plan.key_id == "k1"


# ── explicit agent ────────────────────────────────────────────────

def test_use_opencode_agent_exports_only_opencode_vars(sample_config_file: Path):
    cfg = config_mod.load(sample_config_file)
    plan = use_mod.build_use_plan(cfg, agent_id="opencode")
    assert plan.agent_id == "opencode"
    assert plan.provider_id == "zhipu"
    assert plan.key_id == "main"
    # opencode vars present
    assert plan.exports[key_var("zhipu")] == "sk-zhipu-main"
    assert plan.exports[DEFAULT_MODEL_VAR] == "zhipu/glm-5.2"
    # NO claude vars when agent=opencode
    assert ANTHROPIC_AUTH_TOKEN_VAR not in plan.exports
    assert ANTHROPIC_BASE_URL_VAR not in plan.exports


def test_use_claude_agent_with_bailian_blocked(sample_config_file: Path):
    cfg = config_mod.load(sample_config_file)
    plan = use_mod.build_use_plan(
        cfg, agent_id="claude", provider_id="bailian", key_id="account-b")
    assert plan.agent_id == "claude"
    assert plan.provider_id == "bailian"
    assert plan.key_id == "account-b"
    # bailian has no anthropic baseURL → claude blocked
    assert plan.blocked is True
    assert "no anthropic baseURL" in plan.block_reason
    assert ANTHROPIC_AUTH_TOKEN_VAR not in plan.exports


def test_use_model_arg_fills_agent_slots(sample_config_file: Path):
    cfg = config_mod.load(sample_config_file)
    # claude: model fills opus+sonnet
    plan = use_mod.build_use_plan(
        cfg, agent_id="claude", provider_id="zhipu", key_id="main",
        model="glm-5-turbo")
    assert plan.exports[ANTHROPIC_OPUS_MODEL_VAR] == "glm-5-turbo"
    assert plan.exports[ANTHROPIC_SONNET_MODEL_VAR] == "glm-5-turbo"
    # opencode: model fills default_model
    plan2 = use_mod.build_use_plan(
        cfg, agent_id="opencode", provider_id="zhipu", key_id="main",
        model="glm-5-turbo")
    assert plan2.exports[DEFAULT_MODEL_VAR] == "zhipu/glm-5-turbo"


def test_use_invalid_model_raises(sample_config_file: Path):
    cfg = config_mod.load(sample_config_file)
    with pytest.raises(ValueError, match="not available under provider"):
        use_mod.build_use_plan(
            cfg, agent_id="claude", provider_id="zhipu", key_id="main",
            model="nope")


def test_use_unknown_agent_raises(sample_config_file: Path):
    cfg = config_mod.load(sample_config_file)
    with pytest.raises(ValueError, match="unknown agent 'nope'"):
        use_mod.build_use_plan(cfg, agent_id="nope")


# ── blacklist behavior (per-agent) ────────────────────────────────

def test_use_claude_blacklist_blocks_anthropic(tmp_path: Path):
    cfg_dict = {"providers": [{
        "id": "z", "type": "symmetric", "displayName": "Z",
        "baseURLs": {"anthropic": "https://z/a", "openai": "https://z/o"},
        "keys": [
            {"id": "main", "key": "v1", "agentBlacklist": ["claude"]},
            {"id": "back", "key": "v2"},
        ],
        "models": [{"id": "m", "displayName": "M", "context": 1, "output": 1}],
    }]}
    p = tmp_path / "c.jsonc"
    p.write_text(__import__("json").dumps(cfg_dict))
    cfg = config_mod.load(p)
    # claude + blacklisted key → blocked
    plan = use_mod.build_use_plan(
        cfg, agent_id="claude", provider_id="z", key_id="main")
    assert plan.blocked is True
    assert "blacklisted for claude" in plan.block_reason
    assert ANTHROPIC_AUTH_TOKEN_VAR not in plan.exports
    # opencode + same key → still works (not opencode-blacklisted)
    plan2 = use_mod.build_use_plan(
        cfg, agent_id="opencode", provider_id="z", key_id="main")
    assert plan2.exports[key_var("z")] == "v1"


def test_use_opencode_blacklist_empties_key_var(tmp_path: Path):
    cfg_dict = {"providers": [{
        "id": "z", "type": "symmetric", "displayName": "Z",
        "defaultKey": "back",
        "baseURLs": {"anthropic": "https://z/a", "openai": "https://z/o"},
        "keys": [
            {"id": "main", "key": "v1", "agentBlacklist": ["opencode"]},
            {"id": "back", "key": "v2"},
        ],
        "models": [{"id": "m", "displayName": "M", "context": 1, "output": 1}],
    }]}
    p = tmp_path / "c.jsonc"
    p.write_text(__import__("json").dumps(cfg_dict))
    cfg = config_mod.load(p)
    # opencode + blacklisted key → empty var, default model falls back
    plan = use_mod.build_use_plan(
        cfg, agent_id="opencode", provider_id="z", key_id="main")
    assert plan.exports[key_var("z")] == ""
    # default model falls back to first opencode-allowed provider/key → z/m
    assert plan.default_model == "z/m"
    assert "z" in plan.skipped_providers
    # claude + same key → still works (not claude-blacklisted)
    plan2 = use_mod.build_use_plan(
        cfg, agent_id="claude", provider_id="z", key_id="main")
    assert plan2.exports[ANTHROPIC_AUTH_TOKEN_VAR] == "v1"


# ── render_exports + shell hook ───────────────────────────────────

def test_render_exports_is_shell_sourceable(sample_config_file: Path):
    cfg = config_mod.load(sample_config_file)
    plan = use_mod.build_use_plan(cfg, agent_id="claude")
    text = use_mod.render_exports(plan)
    for line in text.strip().splitlines():
        assert line.startswith("export ")
    assert re.search(r"export ANTHROPIC_AUTH_TOKEN='sk-zhipu-main'", text)


def test_render_exports_opencode(sample_config_file: Path):
    cfg = config_mod.load(sample_config_file)
    plan = use_mod.build_use_plan(cfg, agent_id="opencode")
    text = use_mod.render_exports(plan)
    assert re.search(r"export LLM_KEY_ZHIPU='sk-zhipu-main'", text)
    assert re.search(r"export LLM_DEFAULT_MODEL='zhipu/glm-5\.2'", text)
    # no ANTHROPIC vars in opencode output
    assert "ANTHROPIC" not in text


def test_init_shell_hook_idempotent(tmp_path: Path):
    rc = tmp_path / ".zshrc"
    rc.write_text("export FOO=bar\n")
    assert use_mod.init_shell_hook(str(rc)) is True
    assert use_mod.init_shell_hook(str(rc)) is False  # second time no-op
    content = rc.read_text()
    assert "export FOO=bar" in content
    assert use_mod.HOOK_MARKER_BEGIN in content
    assert "lpm()" in content
    assert 'command lpm use' in content
    assert "source \"$LLM_PROVIDER_ACTIVE_ENV\"" in content
    assert "lpm use" in content


def test_write_active_env_creates_file_with_0600(sample_config_file: Path, tmp_path: Path):
    import os
    cfg = config_mod.load(sample_config_file)
    plan = use_mod.build_use_plan(cfg, agent_id="claude")
    env_path = tmp_path / "active.env.sh"
    written = use_mod.write_active_env(plan, str(env_path))
    assert written == str(env_path)
    assert env_path.exists()
    content = env_path.read_text()
    assert "export ANTHROPIC_AUTH_TOKEN='sk-zhipu-main'" in content
    mode = os.stat(env_path).st_mode & 0o777
    assert mode == 0o600


def test_write_active_env_default_path_uses_env_var(sample_config_file: Path, tmp_path: Path, monkeypatch):
    cfg = config_mod.load(sample_config_file)
    plan = use_mod.build_use_plan(cfg, agent_id="claude")
    custom = tmp_path / "custom.env.sh"
    monkeypatch.setenv("LLM_PROVIDER_ACTIVE_ENV", str(custom))
    written = use_mod.write_active_env(plan)
    assert written == str(custom)
    assert custom.exists()


# ── default.agent resolution ──────────────────────────────────────

def test_use_default_agent_from_config(tmp_path: Path):
    cfg_dict = {
        "default": {"agent": "opencode", "provider": "z", "key": "k1"},
        "providers": [{
            "id": "z", "type": "symmetric", "displayName": "Z",
            "baseURLs": {"openai": "https://z/o"},
            "keys": [{"id": "k1", "key": "v1"}],
            "models": [{"id": "m", "displayName": "M", "context": 1, "output": 1}],
        }],
    }
    p = tmp_path / "c.jsonc"
    p.write_text(__import__("json").dumps(cfg_dict))
    cfg = config_mod.load(p)
    plan = use_mod.build_use_plan(cfg)  # no --agent → config default.agent (opencode)
    assert plan.agent_id == "opencode"
