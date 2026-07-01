"""Tests for keypool.react: one-step reactive recovery decisions.

react is the reactive counterpart to the deleted retry_plan: the runner calls
it after each failure, applies the returned atom strategy, then re-classifies
the next error. Returns "stop" when no recovery is possible.
"""
from __future__ import annotations

import json
from pathlib import Path

from llm_provider_manager.keypool import react


def _write_cfg(tmp_path: Path, providers: list) -> Path:
    cfg = {"providers": providers}
    p = tmp_path / "providers.jsonc"
    p.write_text(json.dumps(cfg))
    return p


def _zhipu_cfg(tmp_path: Path) -> Path:
    """A 2-key zhipu pool; both keys usable for claude (anthropic baseURL)."""
    return _write_cfg(tmp_path, [{
        "id": "zhipu", "type": "symmetric", "displayName": "智谱", "defaultKey": "main",
        "baseURLs": {"anthropic": "https://zhipu/anthropic"},
        "keys": [
            {"id": "main", "key": "sk-main"},
            {"id": "back", "key": "sk-back"},
        ],
        "models": [
            {"id": "glm-5.2", "displayName": "GLM-5.2", "context": 1, "output": 1},
            {"id": "glm-4.7", "displayName": "GLM-4.7", "context": 1, "output": 1},
        ],
    }])


def _init_state(tmp_path: Path, cfg_path: Path) -> Path:
    """Initialize keypool state so react can resolve the current provider."""
    from llm_provider_manager.keypool import dispatch
    state = tmp_path / "state.json"
    dispatch(["init", "--config", str(cfg_path), "--state", str(state), "--agent", "claude"])
    return state


# ── error codes → strategy ────────────────────────────────────────

def test_react_1301_rotates_and_downgrades_without_disable(tmp_path: Path):
    cfg = _zhipu_cfg(tmp_path)
    state = _init_state(tmp_path, cfg)
    step = react('{"code":"1301"}', str(cfg), str(state), agent_id="claude")
    assert step == "rotate,downgrade"
    assert "disable" not in step.split(",")


def test_react_1305_downgrade_only(tmp_path: Path):
    cfg = _zhipu_cfg(tmp_path)
    state = _init_state(tmp_path, cfg)
    step = react('{"code":"1305"}', str(cfg), str(state), agent_id="claude")
    assert step == "downgrade"


def test_react_1308_disables_and_rotates(tmp_path: Path):
    cfg = _zhipu_cfg(tmp_path)
    state = _init_state(tmp_path, cfg)
    step = react('{"code":"1308"}', str(cfg), str(state), agent_id="claude")
    assert step == "disable,rotate"


# ── stop conditions ───────────────────────────────────────────────

def test_react_stops_when_pool_exhausted_and_rotate_needed(tmp_path: Path):
    # 1308 → disable,rotate. Disable both keys first → available=0 → rotate
    # impossible → stripped → no actionable atom → stop.
    cfg = _zhipu_cfg(tmp_path)
    state = _init_state(tmp_path, cfg)
    from llm_provider_manager.keypool import dispatch
    # disable current key, rotate to the other, disable that too
    dispatch(["disable", "--config", str(cfg), "--state", str(state),
              "--agent", "claude", "--key", "sk-main"])
    dispatch(["rotate", "--config", str(cfg), "--state", str(state), "--agent", "claude"])
    dispatch(["disable", "--config", str(cfg), "--state", str(state),
              "--agent", "claude", "--key", "sk-back"])
    step = react('{"code":"1308"}', str(cfg), str(state), agent_id="claude")
    assert step == "stop"


def test_react_no_pool_keeps_only_downgrade(tmp_path: Path):
    # no config → no pool. Without a pool, disable/rotate are meaningless (no
    # key to disable, none to rotate to); only downgrade can help. Both 1308
    # and 1305 (unrecognised without a provider → _default) collapse to
    # "downgrade".
    bogus = str(tmp_path / "nope.jsonc")
    assert react('{"code":"1308"}', bogus, str(tmp_path / "s.json"), agent_id="claude") == "downgrade"
    assert react('{"code":"1305"}', bogus, str(tmp_path / "s.json"), agent_id="claude") == "downgrade"


def test_react_no_pool_pure_disable_error_stops(tmp_path: Path):
    # if a strategy had NO downgrade atom (hypothetical), no pool → nothing
    # actionable → stop. Construct via a provider whose _default is disable,rotate.
    cfg = _write_cfg(tmp_path, [{
        "id": "p", "type": "symmetric", "displayName": "P", "defaultKey": "k",
        "baseURLs": {"anthropic": "https://p/a"},
        "keys": [{"id": "k", "key": "sk-k"}],
        "models": [{"id": "m", "displayName": "M", "context": 1, "output": 1}],
        "errorHandling": {"_default": "disable,rotate"},
    }])
    # but with a pool it has 1 key → disable,rotate is actionable. To force the
    # "no actionable" path we point at a missing config so _default applies AND
    # no pool: _default (disable,rotate,downgrade) → strip to downgrade only,
    # which IS actionable. So instead, exhaust the pool then hit a disable,rotate.
    state = _init_state(tmp_path, cfg)
    from llm_provider_manager.keypool import dispatch
    dispatch(["disable", "--config", str(cfg), "--state", str(state),
              "--agent", "claude", "--key", "sk-k"])
    # pool now exhausted; _default=disable,rotate → rotate stripped → disable only
    # → not actionable → stop
    assert react('{"code":"9999"}', str(cfg), str(state), agent_id="claude") == "stop"


def test_react_unknown_code_falls_to_default(tmp_path: Path):
    cfg = _zhipu_cfg(tmp_path)
    state = _init_state(tmp_path, cfg)
    # unknown code → _default = disable,rotate,downgrade
    step = react('{"code":"9999"}', str(cfg), str(state), agent_id="claude")
    assert step == "disable,rotate,downgrade"
