"""Tests for status.py: reverse-resolving what each agent actually uses.

``status`` reads three layers (project/user config-file literals > live env >
active.env.sh drift baseline) and reports the *effective* provider/key/model
per agent. These tests cover each layer and the merge between them.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from llm_provider_manager import config as config_mod
from llm_provider_manager import status as status_mod
from llm_provider_manager.agents.claude import (
    ANTHROPIC_AUTH_TOKEN_VAR,
    ANTHROPIC_BASE_URL_VAR,
    ANTHROPIC_OPUS_MODEL_VAR,
    ANTHROPIC_SONNET_MODEL_VAR,
    ClaudeAgent,
)
from llm_provider_manager.agents.opencode import (
    DEFAULT_MODEL_VAR,
    OpencodeAgent,
    key_var,
)


# ── claude env reverse-resolution ─────────────────────────────────

def test_claude_probe_resolves_provider_key_model(sample_config_file: Path):
    cfg = config_mod.load(sample_config_file)
    env = {
        ANTHROPIC_AUTH_TOKEN_VAR: "sk-zhipu-main",
        ANTHROPIC_BASE_URL_VAR: "https://zhipu/anthropic",
        ANTHROPIC_OPUS_MODEL_VAR: "glm-5.2",
        ANTHROPIC_SONNET_MODEL_VAR: "glm-5.2",
    }
    st = ClaudeAgent().probe(env, cfg)
    assert st.configured is True
    assert st.provider_id == "zhipu"
    assert st.key_id == "main"
    assert st.model == "glm-5.2"
    assert st.effective_source == "env"
    assert ANTHROPIC_AUTH_TOKEN_VAR in st.secret_vars


def test_claude_probe_empty_env_is_unconfigured(sample_config_file: Path):
    cfg = config_mod.load(sample_config_file)
    st = ClaudeAgent().probe({}, cfg)
    assert st.configured is False
    assert st.provider_id is None
    assert st.key_id is None
    assert "no ANTHROPIC_* env vars set" in st.note


def test_claude_probe_baseurl_matches_no_provider(sample_config_file: Path):
    cfg = config_mod.load(sample_config_file)
    env = {
        ANTHROPIC_AUTH_TOKEN_VAR: "sk-zhipu-main",
        ANTHROPIC_BASE_URL_VAR: "https://nowhere/anthropic",
        ANTHROPIC_OPUS_MODEL_VAR: "glm-5.2",
    }
    st = ClaudeAgent().probe(env, cfg)
    assert st.provider_id is None  # baseURL doesn't match any provider
    assert st.key_id is None
    assert "matches no configured provider" in st.note


def test_claude_probe_token_matches_no_key(tmp_path: Path):
    # baseURL matches but the token doesn't match any key → provider found, key unknown
    cfg_dict = {"providers": [{
        "id": "z", "type": "symmetric", "displayName": "Z",
        "baseURLs": {"anthropic": "https://z/a"},
        "keys": [{"id": "k1", "key": "real-key"}],
        "models": [{"id": "m", "displayName": "M", "context": 1, "output": 1}],
    }]}
    p = tmp_path / "c.jsonc"
    p.write_text(json.dumps(cfg_dict))
    cfg = config_mod.load(p)
    env = {
        ANTHROPIC_AUTH_TOKEN_VAR: "wrong-key",
        ANTHROPIC_BASE_URL_VAR: "https://z/a",
        ANTHROPIC_OPUS_MODEL_VAR: "m",
    }
    st = ClaudeAgent().probe(env, cfg)
    assert st.provider_id == "z"
    assert st.key_id is None  # token matches no key


# ── opencode env reverse-resolution ───────────────────────────────

def test_opencode_probe_resolves_symmetric(sample_config_file: Path):
    cfg = config_mod.load(sample_config_file)
    env = {
        DEFAULT_MODEL_VAR: "zhipu/glm-5.2",
        key_var("zhipu"): "sk-zhipu-main",
    }
    st = OpencodeAgent().probe(env, cfg)
    assert st.configured is True
    assert st.provider_id == "zhipu"
    assert st.key_id == "main"
    assert st.model == "glm-5.2"


def test_opencode_probe_resolves_multiey_asymmetric(sample_config_file: Path):
    cfg = config_mod.load(sample_config_file)
    # bailian is asymmetric with keys account-a/account-b; entry id is bailian-account-a
    env = {
        DEFAULT_MODEL_VAR: "bailian-account-a/glm-5.2",
        key_var("bailian", "account-a"): "sk-bailian-a",
        key_var("bailian", "account-b"): "sk-bailian-b",
    }
    st = OpencodeAgent().probe(env, cfg)
    assert st.provider_id == "bailian"
    assert st.key_id == "account-a"
    assert st.model == "glm-5.2"


def test_opencode_probe_empty_env_is_unconfigured(sample_config_file: Path):
    cfg = config_mod.load(sample_config_file)
    st = OpencodeAgent().probe({}, cfg)
    assert st.configured is False
    assert "no LLM_DEFAULT_MODEL" in st.note


def test_opencode_probe_blacklisted_key_empty_is_normal(tmp_path: Path):
    # a blacklisted key's LLM_KEY_* is the empty string — not flagged as a problem
    cfg_dict = {"providers": [{
        "id": "z", "type": "symmetric", "displayName": "Z", "defaultKey": "back",
        "baseURLs": {"openai": "https://z/o"},
        "keys": [
            {"id": "main", "key": "v1", "agentBlacklist": ["opencode"]},
            {"id": "back", "key": "v2"},
        ],
        "models": [{"id": "m", "displayName": "M", "context": 1, "output": 1}],
    }]}
    p = tmp_path / "c.jsonc"
    p.write_text(json.dumps(cfg_dict))
    cfg = config_mod.load(p)
    env = {DEFAULT_MODEL_VAR: "z/m", key_var("z"): ""}  # blacklisted → empty
    st = OpencodeAgent().probe(env, cfg)
    assert st.configured is True
    assert st.env_values[key_var("z")] == ""
    assert st.note == ""  # empty blacklisted slot is not an error


def test_opencode_probe_default_model_matches_no_entry(sample_config_file: Path):
    cfg = config_mod.load(sample_config_file)
    env = {DEFAULT_MODEL_VAR: "nope/m"}
    st = OpencodeAgent().probe(env, cfg)
    assert st.provider_id is None
    assert "matches no configured entry" in st.note


# ── config-file override layer ────────────────────────────────────

def test_claude_config_file_overrides_env(sample_config_file: Path, tmp_path: Path):
    cfg = config_mod.load(sample_config_file)
    # env says zhipu, but a project settings.local.json overrides to bailian-style
    # bailian in sample_config has no anthropic baseURL, so use zhipu's backup key
    proj = tmp_path / ".claude"
    proj.mkdir()
    (proj / "settings.local.json").write_text(json.dumps({"env": {
        ANTHROPIC_BASE_URL_VAR: "https://zhipu/anthropic",
        ANTHROPIC_AUTH_TOKEN_VAR: "sk-zhipu-backup",   # different key
        ANTHROPIC_OPUS_MODEL_VAR: "glm-5-turbo",
        ANTHROPIC_SONNET_MODEL_VAR: "glm-5-turbo",
    }}))
    env = {
        ANTHROPIC_AUTH_TOKEN_VAR: "sk-zhipu-main",
        ANTHROPIC_BASE_URL_VAR: "https://zhipu/anthropic",
        ANTHROPIC_OPUS_MODEL_VAR: "glm-5.2",
    }
    statuses = status_mod.probe_all(cfg, env=env, cwd=str(tmp_path))
    st = statuses["claude"]
    # override wins: key re-resolves to backup, model to glm-5-turbo
    assert st.key_id == "backup"
    assert st.model == "glm-5-turbo"
    assert st.effective_source.startswith("config-file:")
    assert st.config_overrides[ANTHROPIC_AUTH_TOKEN_VAR] == "sk-zhipu-backup"
    # env_values still shows the live (env-layer) value
    assert st.env_values[ANTHROPIC_AUTH_TOKEN_VAR] == "sk-zhipu-main"


def test_claude_config_file_precedence_local_over_settings(tmp_path: Path):
    # .claude/settings.local.json (higher prec) beats .claude/settings.json
    cfg_dict = {"providers": [{
        "id": "z", "type": "symmetric", "displayName": "Z",
        "baseURLs": {"anthropic": "https://z/a"},
        "keys": [{"id": "k1", "key": "from-local"}],
        "models": [{"id": "m", "displayName": "M", "context": 1, "output": 1}],
    }]}
    p = tmp_path / "c.jsonc"
    p.write_text(json.dumps(cfg_dict))
    cfg = config_mod.load(p)
    proj = tmp_path / "proj"
    claude_dir = proj / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "settings.json").write_text(json.dumps({"env": {
        ANTHROPIC_AUTH_TOKEN_VAR: "from-settings",
        ANTHROPIC_BASE_URL_VAR: "https://z/a",
    }}))
    (claude_dir / "settings.local.json").write_text(json.dumps({"env": {
        ANTHROPIC_AUTH_TOKEN_VAR: "from-local",
        ANTHROPIC_BASE_URL_VAR: "https://z/a",
    }}))
    statuses = status_mod.probe_all(cfg, env={}, cwd=str(proj))
    st = statuses["claude"]
    assert st.key_id == "k1"  # token from-local matches k1's key
    assert st.config_overrides[ANTHROPIC_AUTH_TOKEN_VAR] == "from-local"
    assert st.override_source.endswith("settings.local.json")


def test_opencode_config_file_literal_apikey_overrides_env(tmp_path: Path):
    cfg_dict = {"providers": [{
        "id": "z", "type": "symmetric", "displayName": "Z",
        "baseURLs": {"openai": "https://z/o"},
        "keys": [{"id": "k1", "key": "env-key"}, {"id": "k2", "key": "baked-key"}],
        "models": [{"id": "m", "displayName": "M", "context": 1, "output": 1}],
    }]}
    p = tmp_path / "c.jsonc"
    p.write_text(json.dumps(cfg_dict))
    cfg = config_mod.load(p)
    proj = tmp_path / "proj"
    proj.mkdir()
    # inline-style opencode.json: literal apiKey + literal model (no {env:})
    (proj / "opencode.json").write_text(json.dumps({
        "provider": {
            "z": {
                "options": {"apiKey": "baked-key", "baseURL": "https://z/o"},
                "models": {},
            }
        },
        "model": "z/m",
    }))
    env = {DEFAULT_MODEL_VAR: "z/m", key_var("z"): "env-key"}
    statuses = status_mod.probe_all(cfg, env=env, cwd=str(proj))
    st = statuses["opencode"]
    assert st.config_overrides[key_var("z")] == "baked-key"
    assert st.effective_source.startswith("config-file:")
    # note: provider/key come from LLM_DEFAULT_MODEL (z/m → z/k1 default); the
    # apiKey override doesn't change entry resolution, only the key *value*.


def test_opencode_config_file_env_placeholder_not_an_override(tmp_path: Path):
    # a {env:...} placeholder (template mode) must NOT count as a literal override
    cfg_dict = {"providers": [{
        "id": "z", "type": "symmetric", "displayName": "Z",
        "baseURLs": {"openai": "https://z/o"},
        "keys": [{"id": "k1", "key": "v1"}],
        "models": [{"id": "m", "displayName": "M", "context": 1, "output": 1}],
    }]}
    p = tmp_path / "c.jsonc"
    p.write_text(json.dumps(cfg_dict))
    cfg = config_mod.load(p)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "opencode.json").write_text(json.dumps({
        "provider": {
            "z": {"options": {"apiKey": "{env:LLM_KEY_Z}", "baseURL": "https://z/o"}},
        },
        "model": "{env:LLM_DEFAULT_MODEL}",
    }))
    statuses = status_mod.probe_all(cfg, env={DEFAULT_MODEL_VAR: "z/m", key_var("z"): "v1"},
                                    cwd=str(proj))
    st = statuses["opencode"]
    assert st.config_overrides == {}  # placeholders are not overrides
    assert st.effective_source == "env"


def test_claude_probe_config_file_missing_returns_empty(tmp_path: Path):
    assert ClaudeAgent().probe_config_file(str(tmp_path / "nope.json")) == {}


def test_claude_probe_config_file_no_env_block_returns_empty(tmp_path: Path):
    f = tmp_path / "s.json"
    f.write_text(json.dumps({"permissions": {"allow": ["Read"]}}))
    assert ClaudeAgent().probe_config_file(str(f)) == {}


# ── active.env.sh parsing + drift ─────────────────────────────────

def test_parse_active_env_exports_roundtrips_quoting():
    from llm_provider_manager.env_contract import sh_export
    exports = {
        "ANTHROPIC_AUTH_TOKEN": "sk-with'quote",
        "ANTHROPIC_BASE_URL": "https://z/anthropic",
        "LLM_DEFAULT_MODEL": "z/glm-5.2",
    }
    text = "\n".join(sh_export(k, v) for k, v in exports.items())
    parsed = status_mod.parse_active_env_exports(text)
    assert parsed == exports


def test_parse_active_env_exports_skips_non_export_lines():
    text = "# a comment\nexport FOO='bar'\necho hi\nexport BAZ='qux'\n"
    parsed = status_mod.parse_active_env_exports(text)
    assert parsed == {"FOO": "bar", "BAZ": "qux"}


def test_drift_flagged_when_env_disagrees_with_active_env(tmp_path: Path,
                                                          monkeypatch):
    # point active.env.sh at a temp file recording a DIFFERENT token
    active = tmp_path / "active.env.sh"
    active.write_text(
        f"export {ANTHROPIC_AUTH_TOKEN_VAR}='stale-token'\n"
        f"export {ANTHROPIC_BASE_URL_VAR}='https://z/anthropic'\n"
    )
    monkeypatch.setenv("LLM_PROVIDER_ACTIVE_ENV", str(active))
    cfg_dict = {"providers": [{
        "id": "z", "type": "symmetric", "displayName": "Z",
        "baseURLs": {"anthropic": "https://z/a"},
        "keys": [{"id": "k1", "key": "live-token"}, {"id": "k2", "key": "stale-token"}],
        "models": [{"id": "m", "displayName": "M", "context": 1, "output": 1}],
    }]}
    p = tmp_path / "c.jsonc"
    p.write_text(json.dumps(cfg_dict))
    cfg = config_mod.load(p)
    env = {
        ANTHROPIC_AUTH_TOKEN_VAR: "live-token",   # differs from active.env.sh
        ANTHROPIC_BASE_URL_VAR: "https://z/anthropic",  # ...and baseURL differs too
    }
    statuses = status_mod.probe_all(cfg, env=env, cwd=str(tmp_path))
    st = statuses["claude"]
    assert ANTHROPIC_AUTH_TOKEN_VAR in st.drift
    assert st.drift[ANTHROPIC_AUTH_TOKEN_VAR] == "stale-token"


def test_no_drift_when_env_matches_active_env(tmp_path: Path, monkeypatch):
    active = tmp_path / "active.env.sh"
    active.write_text(
        f"export {ANTHROPIC_AUTH_TOKEN_VAR}='live-token'\n"
        f"export {ANTHROPIC_BASE_URL_VAR}='https://z/anthropic'\n"
    )
    monkeypatch.setenv("LLM_PROVIDER_ACTIVE_ENV", str(active))
    cfg_dict = {"providers": [{
        "id": "z", "type": "symmetric", "displayName": "Z",
        "baseURLs": {"anthropic": "https://z/anthropic"},
        "keys": [{"id": "k1", "key": "live-token"}],
        "models": [{"id": "m", "displayName": "M", "context": 1, "output": 1}],
    }]}
    p = tmp_path / "c.jsonc"
    p.write_text(json.dumps(cfg_dict))
    cfg = config_mod.load(p)
    env = {
        ANTHROPIC_AUTH_TOKEN_VAR: "live-token",
        ANTHROPIC_BASE_URL_VAR: "https://z/anthropic",
    }
    statuses = status_mod.probe_all(cfg, env=env, cwd=str(tmp_path))
    assert statuses["claude"].drift == {}


# ── config_probe_paths ────────────────────────────────────────────

def test_claude_config_probe_paths_includes_cwd_and_user(tmp_path: Path):
    paths = ClaudeAgent().config_probe_paths(str(tmp_path))
    assert paths[0] == os.path.join(str(tmp_path), ".claude", "settings.local.json")
    assert paths[1] == os.path.join(str(tmp_path), ".claude", "settings.json")
    # user-level paths are expanded
    assert any(os.path.expanduser("~/.claude/settings.local.json") in p for p in paths)


def test_opencode_config_probe_paths_includes_cwd_and_user(tmp_path: Path):
    paths = OpencodeAgent().config_probe_paths(str(tmp_path))
    assert paths[0] == os.path.join(str(tmp_path), "opencode.json")
    assert any(os.path.expanduser("~/.config/opencode/opencode.json") in p for p in paths)


# ── rendering ─────────────────────────────────────────────────────

def test_render_status_shows_effective_and_drift(sample_config_file: Path,
                                                  tmp_path: Path, monkeypatch):
    cfg = config_mod.load(sample_config_file)
    active = tmp_path / "active.env.sh"
    active.write_text(f"export {ANTHROPIC_AUTH_TOKEN_VAR}='stale-token'\n")
    monkeypatch.setenv("LLM_PROVIDER_ACTIVE_ENV", str(active))
    env = {
        ANTHROPIC_AUTH_TOKEN_VAR: "sk-zhipu-main",
        ANTHROPIC_BASE_URL_VAR: "https://zhipu/anthropic",
        ANTHROPIC_OPUS_MODEL_VAR: "glm-5.2",
        ANTHROPIC_SONNET_MODEL_VAR: "glm-5.2",
    }
    statuses = status_mod.probe_all(cfg, env=env, cwd=str(tmp_path))
    out = status_mod.render_status(cfg, statuses)
    assert "agent: claude" in out
    assert "provider:      zhipu" in out
    assert "key:           main" in out
    assert "drift:" in out
    # secrets are redacted in output
    assert "sk-zhipu-main" not in out  # full secret not shown
    assert "stale-token" not in out    # full drift secret not shown either


def test_render_status_unconfigured_agent(sample_config_file: Path, tmp_path: Path,
                                          monkeypatch):
    cfg = config_mod.load(sample_config_file)
    monkeypatch.setenv("LLM_PROVIDER_ACTIVE_ENV", str(tmp_path / "nope.env.sh"))
    statuses = status_mod.probe_all(cfg, env={}, cwd=str(tmp_path))
    out = status_mod.render_status(cfg, statuses)
    assert "unconfigured" in out
    assert "(unknown)" in out
