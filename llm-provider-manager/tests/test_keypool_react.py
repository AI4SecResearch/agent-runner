"""Tests for keypool.react: one-step reactive recovery decisions.

react is the reactive counterpart to the deleted retry_plan: the runner calls
it after each failure, applies the returned atom strategy, then re-classifies
the next error. A structured stop reason is returned when no recovery is
possible.
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
    decision = react('{"code":"1301"}', str(cfg), str(state), agent_id="claude")
    assert decision.action == "rotate,downgrade"
    assert decision.stop_reason is None
    assert "disable" not in decision.action.split(",")


def test_react_1305_downgrade_only(tmp_path: Path):
    cfg = _zhipu_cfg(tmp_path)
    state = _init_state(tmp_path, cfg)
    decision = react('{"code":"1305"}', str(cfg), str(state), agent_id="claude")
    assert decision.action == "downgrade"
    assert decision.stop_reason is None


def test_react_1308_disables_and_rotates(tmp_path: Path):
    cfg = _zhipu_cfg(tmp_path)
    state = _init_state(tmp_path, cfg)
    decision = react('{"code":"1308"}', str(cfg), str(state), agent_id="claude")
    assert decision.action == "disable,rotate"
    assert decision.stop_reason is None


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
    decision = react('{"code":"1308"}', str(cfg), str(state), agent_id="claude")
    assert decision.action == ""
    assert decision.stop_reason.value == "resource_exhausted"
    assert decision.classification.resource_exhausted is True
    assert "sk-main" not in repr(decision)
    assert "sk-back" not in repr(decision)


def test_react_no_pool_default_only_rotates_then_stops(tmp_path: Path):
    # no config → no pool. Unrecognised codes fall to DefaultProvider._default
    # (= rotate); without a pool rotate is meaningless → stripped → nothing
    # actionable → stop.
    bogus = str(tmp_path / "nope.jsonc")
    for payload in ('{"code":"1308"}', '{"code":"9999"}'):
        decision = react(
            payload,
            bogus,
            str(tmp_path / "s.json"),
            agent_id="claude",
        )
        assert decision.action == ""
        assert decision.stop_reason.value == "no_key_pool"
        assert decision.classification.matched is False
        assert decision.classification.resource_exhausted is False


def test_react_dispatch_keeps_string_output_contract(
    tmp_path: Path,
    capsys,
):
    from llm_provider_manager.keypool import dispatch

    rc = dispatch([
        "react",
        "--config", str(tmp_path / "nope.jsonc"),
        "--state", str(tmp_path / "state.json"),
        "--agent", "claude",
        "--text", '{"code":"9999"}',
    ])

    assert rc == 0
    assert capsys.readouterr().out == "stop\n"


def test_react_no_pool_pure_disable_error_stops(tmp_path: Path):
    # A strategy with NO rotate/downgrade atom is not actionable. Construct a
    # provider whose _default is disable,rotate, exhaust its pool so rotate is
    # stripped, leaving bare disable → stop.
    cfg = _write_cfg(tmp_path, [{
        "id": "p", "type": "symmetric", "displayName": "P", "defaultKey": "k",
        "baseURLs": {"anthropic": "https://p/a"},
        "keys": [{"id": "k", "key": "sk-k"}],
        "models": [{"id": "m", "displayName": "M", "context": 1, "output": 1}],
        "errorHandling": {"_default": "disable,rotate"},
    }])
    state = _init_state(tmp_path, cfg)
    from llm_provider_manager.keypool import dispatch
    dispatch(["disable", "--config", str(cfg), "--state", str(state),
              "--agent", "claude", "--key", "sk-k"])
    # pool exhausted; _default=disable,rotate → rotate stripped → disable only
    # → not actionable → stop
    decision = react(
        '{"code":"9999"}',
        str(cfg),
        str(state),
        agent_id="claude",
    )
    assert decision.action == ""
    assert decision.stop_reason.value == "unclassified_backend_failure"


def test_react_explicit_non_retryable_failure_has_no_actionable_recovery(
    tmp_path: Path,
):
    cfg = _zhipu_cfg(tmp_path)
    document = json.loads(cfg.read_text())
    document["providers"][0]["errorHandling"] = {"9999": "disable"}
    cfg.write_text(json.dumps(document))
    state = _init_state(tmp_path, cfg)

    decision = react(
        '{"code":"9999"}',
        str(cfg),
        str(state),
        agent_id="claude",
    )

    assert decision.action == ""
    assert decision.stop_reason.value == "no_actionable_recovery"
    assert decision.classification.matched is True
    assert decision.classification.resource_exhausted is False


def test_react_does_not_report_resource_exhaustion_while_key_is_available(
    tmp_path: Path,
):
    cfg = _zhipu_cfg(tmp_path)
    document = json.loads(cfg.read_text())
    document["providers"][0]["errorHandling"] = {"1308": "disable"}
    cfg.write_text(json.dumps(document))
    state = _init_state(tmp_path, cfg)

    decision = react(
        '{"code":"1308"}',
        str(cfg),
        str(state),
        agent_id="claude",
    )

    assert decision.action == ""
    assert decision.stop_reason.value == "no_actionable_recovery"
    assert decision.classification.resource_exhausted is True


def test_react_keeps_downgrade_when_key_pool_is_exhausted(tmp_path: Path):
    cfg = _zhipu_cfg(tmp_path)
    state = _init_state(tmp_path, cfg)
    from llm_provider_manager.keypool import dispatch

    dispatch([
        "disable",
        "--config", str(cfg),
        "--state", str(state),
        "--agent", "claude",
        "--key", "sk-main",
    ])
    dispatch([
        "rotate",
        "--config", str(cfg),
        "--state", str(state),
        "--agent", "claude",
    ])
    dispatch([
        "disable",
        "--config", str(cfg),
        "--state", str(state),
        "--agent", "claude",
        "--key", "sk-back",
    ])

    decision = react(
        '{"code":"1301"}',
        str(cfg),
        str(state),
        agent_id="claude",
    )

    assert decision.action == "downgrade"
    assert decision.stop_reason is None


def test_react_unknown_code_falls_to_default(tmp_path: Path):
    cfg = _zhipu_cfg(tmp_path)
    state = _init_state(tmp_path, cfg)
    # unknown code → _default = rotate (cautious: try another key, no disable)
    decision = react('{"code":"9999"}', str(cfg), str(state), agent_id="claude")
    assert decision.action == "rotate"
    assert decision.stop_reason is None
