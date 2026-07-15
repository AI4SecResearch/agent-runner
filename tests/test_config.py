"""config.py 测试:SPECS 表驱动、TOML 加载、AR_ env 覆盖优先级、类型转换。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from agent_runner import config  # noqa: E402
from agent_runner.config import SPECS, Spec  # noqa: E402


@pytest.fixture(autouse=True)
def clear():
    config.clear_cache()
    yield
    config.clear_cache()


# ── SPECS 表自检 ───────────────────────────────────────────────────────────

def test_specs_cover_all_expected_keys():
    """SPECS 声明了所有预期的配置项。"""
    keys = {s.key for s in SPECS}
    expected = {
        "backend", "primary_model", "downgrade_model", "key_pool_config",
        "keypool_state", "run_dir", "sandbox", "stall_timeout", "total_timeout",
        "landlock_config", "landlock_runner", "opencode_auth_env_var", "lpm_src",
    }
    assert keys == expected


def test_no_duplicate_keys():
    keys = [s.key for s in SPECS]
    assert len(keys) == len(set(keys))


def test_each_spec_has_consistent_type_and_key():
    """每项 key 是 str、type 是 str/int/bool。"""
    for s in SPECS:
        assert isinstance(s, Spec)
        assert isinstance(s.key, str)
        assert s.type in (str, int, bool)


# ── 默认值 ─────────────────────────────────────────────────────────────────

def test_defaults_without_toml_or_env(monkeypatch):
    monkeypatch.setattr(config, "_load_toml", lambda: {})
    config.clear_cache()
    assert config.get("backend") == "claude-code"
    assert config.get("sandbox") is False
    assert config.get("stall_timeout") == 300
    assert config.get("total_timeout") == 0
    assert config.get("opencode_auth_env_var") == "Z_AI_API_KEY"


def test_no_default_items_return_none(monkeypatch):
    monkeypatch.setattr(config, "_load_toml", lambda: {})
    config.clear_cache()
    assert config.get("primary_model") is None
    assert config.get("run_dir") is None
    assert config.get("primary_model", "fb") == "fb"


# ── TOML 加载 ──────────────────────────────────────────────────────────────

def test_toml_loaded(monkeypatch):
    monkeypatch.setattr(config, "_load_toml", lambda: {
        "backend": "opencode", "primary_model": "glm-5.1", "run_dir": "/tmp/x",
    })
    config.clear_cache()
    assert config.get("backend") == "opencode"
    assert config.get("primary_model") == "glm-5.1"
    assert config.get("run_dir") == "/tmp/x"


def test_toml_nested_timeouts(monkeypatch):
    """[timeouts] 子表经 toml_path 解析(不再有 _flatten)。"""
    monkeypatch.setattr(config, "_load_toml", lambda: {"timeouts": {"stall": 100, "total": 200}})
    config.clear_cache()
    assert config.get("stall_timeout") == 100
    assert config.get("total_timeout") == 200


# ── env 覆盖 ───────────────────────────────────────────────────────────────

def test_env_overrides_toml(monkeypatch):
    monkeypatch.setattr(config, "_load_toml", lambda: {"backend": "opencode", "stall_timeout": 100})
    monkeypatch.setenv("AR_BACKEND", "claude-code")
    monkeypatch.setenv("AR_STALL_TIMEOUT", "999")
    config.clear_cache()
    assert config.get("backend") == "claude-code"
    assert config.get("stall_timeout") == 999


def test_env_overrides_default_when_no_toml(monkeypatch):
    monkeypatch.setattr(config, "_load_toml", lambda: {})
    monkeypatch.setenv("AR_PRIMARY_MODEL", "glm-5.2")
    config.clear_cache()
    assert config.get("primary_model") == "glm-5.2"


# ── 类型转换 ───────────────────────────────────────────────────────────────

def test_sandbox_env_coerced_to_bool(monkeypatch):
    monkeypatch.setattr(config, "_load_toml", lambda: {})
    for truthy in ("1", "true", "yes", "on"):
        monkeypatch.setenv("AR_SANDBOX", truthy)
        config.clear_cache()
        assert config.get("sandbox") is True, f"{truthy} should be True"
    for falsy in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("AR_SANDBOX", falsy)
        config.clear_cache()
        assert config.get("sandbox") is False, f"{falsy} should be False"


def test_timeout_env_coerced_to_int(monkeypatch):
    monkeypatch.setattr(config, "_load_toml", lambda: {})
    monkeypatch.setenv("AR_STALL_TIMEOUT", "42")
    config.clear_cache()
    assert config.get("stall_timeout") == 42
    assert isinstance(config.get("stall_timeout"), int)


# ── keypool_state 派生 ─────────────────────────────────────────────────────

def test_keypool_state_derived_from_key_pool_config(monkeypatch):
    monkeypatch.setattr(config, "_load_toml", lambda: {"key_pool_config": "/some/dir/providers.jsonc"})
    config.clear_cache()
    assert config.get("keypool_state") == "/some/dir/key-pool-state.json"


def test_keypool_state_override(monkeypatch):
    monkeypatch.setattr(config, "_load_toml", lambda: {"key_pool_config": "/a/providers.jsonc"})
    monkeypatch.setenv("AR_KEYPOOL_STATE", "/custom/state.json")
    config.clear_cache()
    assert config.get("keypool_state") == "/custom/state.json"


# ── 缓存 ───────────────────────────────────────────────────────────────────

def test_clear_cache_picks_up_env_change(monkeypatch):
    monkeypatch.setattr(config, "_load_toml", lambda: {})
    monkeypatch.setenv("AR_BACKEND", "opencode")
    config.clear_cache()
    assert config.get("backend") == "opencode"
    monkeypatch.setenv("AR_BACKEND", "claude-code")
    config.clear_cache()
    assert config.get("backend") == "claude-code"
