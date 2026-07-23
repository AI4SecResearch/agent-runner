"""Tests for keypool entry resolution consistency.

init/rotate/on_success return a ``(key, provider_id, key_id)`` entry whose key
and provider agree, and ``_apply_line`` derives base_url/models from that same
entry without re-reading state. This keeps the emitted line self-consistent —
a concurrent rotate can no longer pair one provider's key with another's
base_url (the TOCTOU that caused cross-provider key/URL mismatches).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm_provider_manager.keypool import KeyPool, dispatch


def _two_provider_cfg(tmp_path: Path) -> Path:
    """Two anthropic-protocol providers, each with one key + distinct model id."""
    cfg = {
        "settings": {"rotateEvery": 1, "disableTtlHours": 1},
        "default": {"agent": "claude", "provider": "zhipu", "key": "z"},
        "providers": [
            {
                "id": "zhipu", "type": "symmetric", "defaultKey": "z",
                "baseURLs": {"anthropic": "https://zhipu/a"},
                "keys": [{"id": "z", "key": "sk-z"}],
                "models": [{"id": "glm-5.2", "displayName": "GLM-5.2",
                            "context": 1, "output": 1}],
            },
            {
                "id": "acme", "type": "symmetric", "defaultKey": "a",
                "baseURLs": {"anthropic": "https://acme/a"},
                "keys": [{"id": "a", "key": "sk-a"}],
                "models": [{"id": "GLM-5.2", "displayName": "glm-5.2",
                            "context": 1, "output": 1}],
            },
        ],
    }
    p = tmp_path / "providers.jsonc"
    p.write_text(json.dumps(cfg))
    return p


def _line_for(cmd: str, cfg: Path, state: Path, capsys) -> dict | None:
    """Run a dispatch command, return its printed JSON line (or None if empty)."""
    dispatch([cmd, "--config", str(cfg), "--state", str(state), "--agent", "claude"])
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out else None


def test_init_line_key_matches_base_url(tmp_path: Path, capsys):
    cfg = _two_provider_cfg(tmp_path)
    state = tmp_path / "state.json"
    line = _line_for("init", cfg, state, capsys)
    assert line is not None
    assert line["key"] == "sk-z"
    assert line["base_url"] == "https://zhipu/a"


def test_on_success_rotation_line_is_consistent(tmp_path: Path, capsys):
    cfg = _two_provider_cfg(tmp_path)
    state = tmp_path / "state.json"
    _line_for("init", cfg, state, capsys)               # current = zhipu
    line = _line_for("on-success", cfg, state, capsys)  # rotateEvery=1 → acme
    assert line is not None
    # key, base_url and model must all belong to acme — never a zhipu/acme mix.
    assert line["key"] == "sk-a"
    assert line["base_url"] == "https://acme/a"
    assert line["primary_model"] == "GLM-5.2"


def test_non_rotating_on_success_emits_empty_line(tmp_path: Path, capsys):
    cfg_dict = json.loads(_two_provider_cfg(tmp_path).read_text())
    cfg_dict["settings"]["rotateEvery"] = 3
    cfg = tmp_path / "providers.jsonc"
    cfg.write_text(json.dumps(cfg_dict))
    state = tmp_path / "state.json"
    _line_for("init", cfg, state, capsys)
    # count 0→1 < 3: no rotation → empty line (no-op signal to the wrapper).
    assert _line_for("on-success", cfg, state, capsys) is None


def test_rotate_every_zero_disables_proactive_rotation(tmp_path: Path):
    cfg_dict = json.loads(_two_provider_cfg(tmp_path).read_text())
    cfg_dict["settings"]["rotateEvery"] = 0
    cfg = tmp_path / "providers.jsonc"
    cfg.write_text(json.dumps(cfg_dict))
    state = tmp_path / "state.json"
    pool = KeyPool(cfg, state, agent_id="claude")

    pool.init()
    initial_key = pool.current_key()
    initial_state = json.loads(state.read_text())

    assert [pool.on_success() for _ in range(5)] == [None] * 5
    assert pool.current_key() == initial_key
    final_state = json.loads(state.read_text())
    assert final_state["current_index"] == initial_state["current_index"]
    assert final_state["success_count"] == 0


def test_rotate_returns_entry_with_matching_provider(tmp_path: Path):
    cfg = _two_provider_cfg(tmp_path)
    state = tmp_path / "state.json"
    kp = KeyPool(cfg, state, agent_id="claude")
    kp.init()
    entry = kp.rotate()
    assert entry is not None
    key_value, pid, kid = entry
    assert pid == "acme"
    assert key_value == "sk-a"
    assert kid == "a"
