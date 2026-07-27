"""config.py 测试:Config 类(解析视图)、SPECS 表驱动、TOML 加载、
AR_ env 覆盖优先级、config_overrides 微调(多实例)、类型转换。

新设计:配置经 ``Config(config_overrides=..., toml=...)`` 实例化,构造时一次性解析、
无后续缓存污染(无 ``clear_cache``)。优先级:**config_overrides(实例) > AR_ env > TOML > 默认**。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from agent_runner import config  # noqa: E402
from agent_runner.config import SPECS, Config, Spec  # noqa: E402


@pytest.fixture(autouse=True)
def reset_default():
    config._reset_default()
    yield
    config._reset_default()


# ── SPECS 表自检 ───────────────────────────────────────────────────────────

def test_specs_cover_all_expected_keys():
    """SPECS 声明了所有预期的配置项。"""
    keys = {s.key for s in SPECS}
    expected = {
        "backend", "primary_model", "downgrade_model", "key_pool_config",
        "keypool_state", "run_dir", "skip_permissions", "stall_timeout", "total_timeout",
        "opencode_auth_env_var", "lpm_src",
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
    monkeypatch.delenv("AR_BACKEND", raising=False)
    monkeypatch.delenv("AR_SKIP_PERMISSIONS", raising=False)
    monkeypatch.delenv("AR_STALL_TIMEOUT", raising=False)
    monkeypatch.delenv("AR_TOTAL_TIMEOUT", raising=False)
    monkeypatch.delenv("AR_OPENCODE_AUTH_ENV_VAR", raising=False)
    c = Config(toml={})
    assert c.get("backend") == "claude-code"
    assert c.get("skip_permissions") is False
    assert c.get("stall_timeout") == 300
    assert c.get("total_timeout") == 0
    assert c.get("opencode_auth_env_var") == "Z_AI_API_KEY"


def test_no_default_items_return_none(monkeypatch):
    monkeypatch.delenv("AR_PRIMARY_MODEL", raising=False)
    monkeypatch.delenv("AR_RUN_DIR", raising=False)
    c = Config(toml={})
    assert c.get("primary_model") is None
    assert c.get("run_dir") is None
    assert c.get("primary_model", "fb") == "fb"


# ── TOML 加载 ──────────────────────────────────────────────────────────────

def test_toml_loaded():
    c = Config(toml={"backend": "opencode", "primary_model": "glm-5.1", "run_dir": "/tmp/x"})
    assert c.get("backend") == "opencode"
    assert c.get("primary_model") == "glm-5.1"
    assert c.get("run_dir") == "/tmp/x"


def test_toml_nested_timeouts():
    """[timeouts] 子表经 toml_path 解析。"""
    c = Config(toml={"timeouts": {"stall": 100, "total": 200}})
    assert c.get("stall_timeout") == 100
    assert c.get("total_timeout") == 200


# ── env 覆盖 ───────────────────────────────────────────────────────────────

def test_env_overrides_toml(monkeypatch):
    monkeypatch.setenv("AR_BACKEND", "claude-code")
    monkeypatch.setenv("AR_STALL_TIMEOUT", "999")
    c = Config(toml={"backend": "opencode", "stall_timeout": 100})
    assert c.get("backend") == "claude-code"
    assert c.get("stall_timeout") == 999


def test_env_overrides_default_when_no_toml(monkeypatch):
    monkeypatch.setenv("AR_PRIMARY_MODEL", "glm-5.2")
    c = Config(toml={})
    assert c.get("primary_model") == "glm-5.2"


# ── config_overrides(实例级微调,多线程/多 Agent) ─────────────────────────

def test_config_overrides_beat_env_and_toml(monkeypatch):
    """config_overrides 优先级最高:压过 env 与 TOML。"""
    monkeypatch.setenv("AR_PRIMARY_MODEL", "env-model")
    c = Config(config_overrides={"primary_model": "override-model"},
               toml={"primary_model": "toml-model"})
    assert c.get("primary_model") == "override-model"


def test_config_overrides_isolate_instances(monkeypatch):
    """两个 Config 实例各自 config_overrides 互不干扰(多 Agent 微调)。"""
    monkeypatch.setenv("AR_BACKEND", "claude-code")
    c1 = Config(config_overrides={"backend": "opencode", "stall_timeout": 600})
    c2 = Config(config_overrides={"backend": "claude-code", "stall_timeout": 50})
    assert c1.get("backend") == "opencode"
    assert c1.get("stall_timeout") == 600
    assert c2.get("backend") == "claude-code"
    assert c2.get("stall_timeout") == 50
    # 互不污染
    c1_b = c1.get("backend")
    assert c2.get("backend") != c1_b or c1_b == "opencode"


def test_config_overrides_none_falls_through(monkeypatch):
    """config_overrides 里某键为 None → 不覆盖(回落 env/TOML)。"""
    monkeypatch.setenv("AR_PRIMARY_MODEL", "env-model")
    c = Config(config_overrides={"primary_model": None}, toml={})
    assert c.get("primary_model") == "env-model"


# ── 类型转换 ───────────────────────────────────────────────────────────────

def test_skip_permissions_env_coerced_to_bool(monkeypatch):
    for truthy in ("1", "true", "yes", "on"):
        monkeypatch.setenv("AR_SKIP_PERMISSIONS", truthy)
        assert Config(toml={}).get("skip_permissions") is True, f"{truthy} should be True"
    for falsy in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("AR_SKIP_PERMISSIONS", falsy)
        assert Config(toml={}).get("skip_permissions") is False, f"{falsy} should be False"


def test_timeout_env_coerced_to_int(monkeypatch):
    monkeypatch.setenv("AR_STALL_TIMEOUT", "42")
    c = Config(toml={})
    assert c.get("stall_timeout") == 42
    assert isinstance(c.get("stall_timeout"), int)


# ── keypool_state 派生 ─────────────────────────────────────────────────────

def test_keypool_state_derived_from_key_pool_config():
    c = Config(toml={"key_pool_config": "/some/dir/providers.jsonc"})
    assert c.get("keypool_state") == "/some/dir/key-pool-state.json"


def test_keypool_state_override(monkeypatch):
    monkeypatch.setenv("AR_KEYPOOL_STATE", "/custom/state.json")
    c = Config(toml={"key_pool_config": "/a/providers.jsonc"})
    assert c.get("keypool_state") == "/custom/state.json"


# ── 模块级默认视图(锁守懒加载,向后兼容) ─────────────────────────────────

def test_module_get_uses_default(monkeypatch):
    monkeypatch.setenv("AR_BACKEND", "opencode")
    config._reset_default()
    assert config.get("backend") == "opencode"


def test_reset_default_picks_up_env_change(monkeypatch):
    """重置默认 Config 后,env 改动被下次解析吃到。"""
    monkeypatch.setenv("AR_BACKEND", "opencode")
    config._reset_default()
    assert config.get("backend") == "opencode"
    monkeypatch.setenv("AR_BACKEND", "claude-code")
    config._reset_default()
    assert config.get("backend") == "claude-code"


def test_no_clear_cache_interface():
    """clear_cache 已被移除(改用 Config 实例 / _reset_default)。"""
    assert not hasattr(config, "clear_cache")
