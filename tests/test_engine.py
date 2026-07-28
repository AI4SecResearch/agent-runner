"""Engine layering tests — verify the orchestration logic (watchdog early-exit,
reactive retry, continue-vs-redo branches, exit codes) via a mock backend,
without running any agent binary.

The mock backend's ``invoke`` writes a scripted jsonl (so ``result_ok`` etc.
return canned values) and returns immediately. We then assert the public entry
points return the expected exit codes and that the retry loop applies the
right atoms. This isolates the orchestration from the subprocess mechanics.
"""

from __future__ import annotations

import json
import inspect
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import agent_runner.engine as eng  # noqa: E402
from agent_runner.backends import claude_code, opencode  # noqa: E402
from agent_runner.backends.claude_code import ClaudeCodeBackend  # noqa: E402
from agent_runner.backends.opencode import OpencodeBackend  # noqa: E402
from agent_runner.config import Config  # noqa: E402
from agent_runner.keypool import KeyContext  # noqa: E402

_REAL_GET_BACKEND = eng.Runner._get_backend


def write_jsonl(prefix: str, events: list[dict]):
    with open(f"{prefix}.jsonl", "w") as f:
        for e in events:
            f.write(json.dumps(e, separators=(",", ":")) + "\n")


class MockBackend(ClaudeCodeBackend):
    """Subclasses claude-code to override invoke/stream with a scripted writer.

    Inherits the jsonl-parsing ops (result_ok/result_text/session_id/is_complete)
    unchanged — so the engine exercises the REAL parsing on mock-written logs.

    State is INSTANCE-level (not class-level) so each test's reset() via the
    module singleton reliably clears the call counter — class-attribute counters
    get shadowed by instance attributes on first ``self.x += 1`` and won't be
    reset by ``cls.x = 0``.
    """

    def __init__(self):
        super().__init__()
        self.script: list[dict] = []
        self.calls: list[tuple] = []
        self.wrapped_commands: list[list[str]] = []
        self._idx = 0

    def invoke(
        self,
        prompt,
        prefix,
        argv,
        key_ctx=None,
        *,
        working_directory=None,
        command_wrapper=None,
    ):
        del working_directory
        self.calls.append((prompt, prefix, list(argv)))
        if command_wrapper is not None:
            self.wrapped_commands.append(
                command_wrapper(["agent-binary", *argv])
            )
        i = self._idx
        self._idx += 1
        if i < len(self.script):
            step = self.script[i]
        else:
            step = self.script[-1] if self.script else {"events": [], "ok": False}
        events = step.get("events", [])
        # session_id if provided
        if step.get("session_id"):
            events = [dict(e, session_id=step["session_id"]) if e.get("type") == "result"
                      else e for e in events]
            if events and "session_id" not in events[0]:
                events[0] = dict(events[0], session_id=step["session_id"])
        write_jsonl(prefix, events)
        return _FakeProc()  # already-exited proc: poll() returns 0 immediately

    def stream(self, proc, prefix):
        # jsonl 已由 invoke 写好;无需流式
        pass

    def reset(self, script):
        self.script = script
        self.calls = []
        self.wrapped_commands = []
        self._idx = 0


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("AR_RUN_DIR", str(tmp_path))
    monkeypatch.setenv("AR_BACKEND", "claude-code")
    monkeypatch.setenv("AR_STALL_TIMEOUT", "5")
    monkeypatch.setenv("AR_TOTAL_TIMEOUT", "0")
    # 新设计:config 是 Config 实例(无模块级缓存);Runner 是 thread-local 默认实例。
    # 重置默认 Config 与默认 Runner,让 setenv 在下次解析时生效。
    from agent_runner import config as _cfg
    _cfg._reset_default()
    eng._reset_default_runner()
    # Mock backend 注入到 Runner 类(所有实例可见);刷新其 config 吃到 setenv。
    _MOCK._config = _cfg.Config(toml={})
    monkeypatch.setattr(eng.Runner, "_get_backend", lambda self: _MOCK)
    yield
    _MOCK.calls.clear()
    _cfg._reset_default()
    eng._reset_default_runner()


# module-singleton mock backend; reset() before each test to clear the call
# counter and load the new script.
_MOCK = MockBackend()


def make_result(text="ok", is_error=False, session_id=""):
    e = {"type": "result", "result": text, "is_error": is_error}
    if session_id:
        e["session_id"] = session_id
    return e


class FakeRecoveryDecision:
    def __init__(self, action="", stop_reason=None):
        self.action = action
        self.stop_reason = (
            None
            if stop_reason is None
            else type("FakeStopReason", (), {"value": stop_reason})()
        )


def recover(action):
    return FakeRecoveryDecision(action=action)


def stop_recovery(reason="no_actionable_recovery"):
    return FakeRecoveryDecision(stop_reason=reason)


class FakeErrorClassification:
    def __init__(
        self,
        action,
        *,
        matched,
        resource_exhausted=False,
    ):
        self.action = action
        self.matched = matched
        self.resource_exhausted = resource_exhausted


class _FakeProc:
    """Already-exited process: poll() returns 0 (the watchdog sees the process
    has exited and falls through to wait/return). returncode is 0."""
    returncode = 0
    pid = -1

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


def test_private_process_integration_wraps_real_backend_spawn(
    monkeypatch,
) -> None:
    popen_calls = []

    def record_popen(command, **kwargs):
        popen_calls.append((command, kwargs))
        return _FakeProc()

    monkeypatch.setattr(eng.Runner, "_get_backend", _REAL_GET_BACKEND)
    monkeypatch.setattr(claude_code.subprocess, "Popen", record_popen)
    integration = eng._ProcessIntegration(
        command_wrapper=lambda command: ["isolation-wrapper", *command]
    )
    runner = eng.Runner._with_process_integration(
        {
            "backend": "claude-code",
            "run_dir": os.environ["AR_RUN_DIR"],
            "sandbox": False,
        },
        discover_config_files=False,
        provider_snapshot=None,
        integration=integration,
    )

    runner._agent_once("prompt", "spawn", [])

    assert popen_calls[0][0] == [
        "isolation-wrapper",
        "claude",
        "-p",
        "prompt",
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "acceptEdits",
    ]


@pytest.mark.parametrize("backend_kind", ["claude-code", "opencode"])
def test_wrapper_failure_happens_before_diagnostic_fd_or_spawn(
    tmp_path: Path,
    monkeypatch,
    backend_kind: str,
) -> None:
    popen_calls = []

    def unexpected_popen(*args, **kwargs):
        popen_calls.append((args, kwargs))
        raise AssertionError("Popen must not run after wrapper failure")

    class WrapperFailure(RuntimeError):
        pass

    def fail_wrapper(command):
        del command
        raise WrapperFailure("injected wrapper failure")

    if backend_kind == "claude-code":
        backend = ClaudeCodeBackend(
            config=Config({"sandbox": False}, toml={})
        )
        monkeypatch.setattr(
            claude_code.subprocess,
            "Popen",
            unexpected_popen,
        )
    else:
        backend = OpencodeBackend(
            config=Config({"sandbox": False}, toml={})
        )
        monkeypatch.setattr(
            opencode.subprocess,
            "Popen",
            unexpected_popen,
        )
    prefix = tmp_path / "diagnostics" / backend_kind

    with pytest.raises(WrapperFailure):
        backend.invoke(
            "prompt",
            str(prefix),
            [],
            command_wrapper=fail_wrapper,
        )

    assert popen_calls == []
    assert not Path(f"{prefix}.err").exists()


def test_public_runner_constructor_signature_stays_exact() -> None:
    parameters = inspect.signature(eng.Runner.__init__).parameters

    assert tuple(parameters) == (
        "self",
        "config_overrides",
        "discover_config_files",
    )
    assert parameters["config_overrides"].default is None
    assert parameters["discover_config_files"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["discover_config_files"].default is True


def test_internal_retry_wraps_every_backend_spawn_independently(
    monkeypatch,
) -> None:
    _MOCK.reset(
        [
            {
                "events": [make_result("retry", is_error=True)],
                "ok": False,
            },
            {"events": [make_result()], "ok": True},
        ]
    )

    class FakeKP:
        def init(self): pass
        def on_success(self): pass
        def rotate(self): pass
        def disable(self): pass
        def available_size(self): return 1
        def react(self, text): return recover("rotate")
        def classify(self, text): return "rotate"

    monkeypatch.setattr(
        eng.Runner,
        "_ensure_keypool",
        lambda self: FakeKP(),
    )
    wrapped_attempts = []

    def wrap(command):
        wrapped_attempts.append(list(command))
        return [f"wrapper-{len(wrapped_attempts)}", *command]

    runner = eng.Runner._with_process_integration(
        {
            "backend": "claude-code",
            "run_dir": os.environ["AR_RUN_DIR"],
        },
        discover_config_files=False,
        provider_snapshot=None,
        integration=eng._ProcessIntegration(command_wrapper=wrap),
    )

    result = runner.agent_with_retry_session_new("prompt", "retry-spawn")

    assert result.rc == 0
    assert len(wrapped_attempts) == 2
    assert _MOCK.wrapped_commands[0][0] == "wrapper-1"
    assert _MOCK.wrapped_commands[1][0] == "wrapper-2"


# ── success on first try ──────────────────────────────────────────────────

def test_session_new_succeeds_first_try(monkeypatch):
    _MOCK.reset([{"events": [make_result()], "ok": True}])
    # Stub the keypool to no-ops (no config).
    class FakeKP:
        def init(self): pass
        def on_success(self): pass
        def rotate(self): pass
        def disable(self): pass
        def available_size(self): return 0
        def react(self, t): return stop_recovery()
        def classify(self, t): return "rotate"
    monkeypatch.setattr(eng.Runner, "_ensure_keypool", lambda self: FakeKP())
    monkeypatch.setenv("AR_PRIMARY_MODEL", "test-model")
    from agent_runner import config as _cfg
    # 刷新 mock backend 的 config,吃到刚设的 AR_PRIMARY_MODEL(无模块级缓存可清)。
    _MOCK._config = _cfg.Config(toml={})
    rc = eng.agent_with_retry_session_new("prompt", "log1")
    assert rc.rc == 0
    assert len(_MOCK.calls) == 1
    assert _MOCK.calls[0][0] == "prompt"  # the prompt
    # one --model (primary,来自 AR_PRIMARY_MODEL),无 resume args
    argv = _MOCK.calls[0][2]
    assert argv.count("--model") == 1


# ── all retries fail → exit 1 ─────────────────────────────────────────────

def test_session_new_all_fail(monkeypatch, capsys):
    # primary fails, react always says "rotate" (never stop), but pool reports
    # size 0 → max_attempts=2 → exhausts.
    _MOCK.reset([{"events": [make_result("err", is_error=True)], "ok": False}] * 10)
    class FakeKP:
        def __init__(self): self.disabled = 0
        def init(self): pass
        def on_success(self): pass
        def rotate(self): pass
        def disable(self): self.disabled += 1
        def available_size(self): return 0
        def react(self, t): return recover("rotate")
        def classify(self, t): return "rotate"
    kp = FakeKP()
    monkeypatch.setattr(eng.Runner, "_ensure_keypool", lambda self: kp)
    result = eng.agent_with_retry_session_new("prompt", "log2")
    assert result.rc == 1
    assert result.outcome is eng.RunOutcome.FAILED
    diagnostic = capsys.readouterr().err
    assert "已完成允许的重试但后端仍失败" in diagnostic
    assert "资源耗尽" not in diagnostic


def test_retry_preserves_disable_and_rotate_behavior(monkeypatch):
    _MOCK.reset([
        {
            "events": [
                make_result("Error [1308] quota exceeded", is_error=True),
            ],
            "ok": False,
        },
        {"events": [make_result()], "ok": True},
    ])

    class FakeKP:
        def __init__(self):
            self.disabled = 0
            self.rotated = 0

        def init(self): return KeyContext(primary_model="primary")
        def on_success(self): pass
        def rotate(self):
            self.rotated += 1
            return KeyContext(primary_model="primary")
        def disable(self): self.disabled += 1
        def available_size(self): return 1
        def react(self, t): return recover("disable,rotate")
        def classify(self, t): return "disable,rotate"

    key_pool = FakeKP()
    monkeypatch.setattr(
        eng.Runner,
        "_ensure_keypool",
        lambda self: key_pool,
    )

    result = eng.agent_with_retry_session_new(
        "prompt",
        "disable-rotate",
    )

    assert result.rc == 0
    assert key_pool.disabled == 1
    assert key_pool.rotated == 1
    assert len(_MOCK.calls) == 2


def test_retry_executes_downgrade_without_rotating(monkeypatch):
    _MOCK.reset([
        {
            "events": [
                make_result("backend overloaded", is_error=True),
            ],
            "ok": False,
        },
        {"events": [make_result()], "ok": True},
    ])

    class FakeKP:
        def __init__(self):
            self.rotated = 0

        def init(self):
            return KeyContext(
                primary_model="primary-model",
                downgrade_model="downgrade-model",
            )
        def on_success(self): pass
        def rotate(self):
            self.rotated += 1
            return KeyContext()
        def disable(self): pass
        def available_size(self): return 0
        def react(self, t): return recover("downgrade")
        def classify(self, t): return "downgrade"

    key_pool = FakeKP()
    monkeypatch.setattr(
        eng.Runner,
        "_ensure_keypool",
        lambda self: key_pool,
    )

    result = eng.agent_with_retry_session_new(
        "prompt",
        "downgrade",
    )

    assert result.rc == 0
    assert key_pool.rotated == 0
    retry_argv = _MOCK.calls[1][2]
    model_index = retry_argv.index("--model")
    assert retry_argv[model_index + 1] == "downgrade-model"


# ── react returns a stop reason → exit 1 ─────────────────────────────────

def test_session_new_react_stop(monkeypatch, capsys):
    _MOCK.reset([{"events": [make_result("err", is_error=True)], "ok": False}])
    class FakeKP:
        def init(self): pass
        def on_success(self): pass
        def rotate(self): pass
        def disable(self): pass
        def available_size(self): return 1
        def react(self, t): return stop_recovery()
        def classify(self, t): return "stop"
    monkeypatch.setattr(eng.Runner, "_ensure_keypool", lambda self: FakeKP())
    rc = eng.agent_with_retry_session_new("prompt", "log3")
    assert rc.rc == 1
    assert len(_MOCK.calls) == 1  # only the primary; retry loop stopped before retrying
    diagnostic = capsys.readouterr().err
    assert "策略没有可执行的恢复动作" in diagnostic
    assert "资源耗尽" not in diagnostic


def test_no_key_pool_unclassified_failure_is_not_resource_exhaustion(
    capsys,
):
    secret = "backend-token-CANARY"
    _MOCK.reset([
        {
            "events": [
                make_result(
                    f"ordinary backend failure token={secret}",
                    is_error=True,
                ),
            ],
            "ok": False,
        },
    ])

    result = eng.agent_with_retry_session_new(
        "prompt",
        "unclassified-no-pool",
    )

    assert result.rc == 1
    assert result.outcome is eng.RunOutcome.FAILED
    diagnostic = capsys.readouterr().err
    assert "资源耗尽" not in diagnostic
    assert "后端执行失败" in diagnostic
    assert secret not in diagnostic
    assert secret not in repr(result)


def test_confirmed_quota_with_exhausted_key_pool_reports_resource_exhaustion(
    tmp_path: Path,
    capsys,
):
    secret = "super-secret-quota-key"
    config_path = tmp_path / "providers.jsonc"
    state_path = tmp_path / "key-pool-state.json"
    config_path.write_text(json.dumps({
        "providers": [{
            "id": "zhipu",
            "type": "symmetric",
            "displayName": "Zhipu",
            "defaultKey": "main",
            "baseURLs": {"anthropic": "https://example.invalid/anthropic"},
            "keys": [{"id": "main", "key": secret}],
            "models": [{
                "id": "model",
                "displayName": "Model",
                "context": 1,
                "output": 1,
            }],
        }],
    }))
    from agent_runner.keypool import KeyPool

    key_pool = KeyPool(
        str(config_path),
        str(state_path),
        agent_id="claude",
        config=Config(toml={}),
    )
    key_pool.init()
    key_pool.disable()
    _MOCK.reset([{
        "events": [
            make_result("Error [1308] quota exceeded", is_error=True),
        ],
        "ok": False,
    }])
    runner = eng.Runner(
        {
            "backend": "claude-code",
            "run_dir": os.environ["AR_RUN_DIR"],
            "key_pool_config": str(config_path),
            "keypool_state": str(state_path),
        },
        discover_config_files=False,
    )

    result = runner.agent_with_retry_session_new(
        "prompt",
        "confirmed-resource-exhaustion",
    )

    assert result.rc == 2
    assert result.outcome is eng.RunOutcome.QUOTA_EXHAUSTED
    diagnostic = capsys.readouterr().err
    assert "资源耗尽" in diagnostic
    assert secret not in diagnostic
    assert secret not in repr(result)


# ── resume entry: primary records a session_id → continue branch ─────────

def test_session_resume_uses_continue_branch(monkeypatch):
    # primary fails but records session_id s1; retry should resume on s1 with
    # prompt "继续" (not the original prompt).
    _MOCK.reset([
        {"events": [make_result("err", is_error=True, session_id="s1")], "ok": False},
        {"events": [make_result()], "ok": True},  # retry succeeds
    ])
    class FakeKP:
        def init(self): pass
        def on_success(self): pass
        def rotate(self): pass
        def disable(self): pass
        def available_size(self): return 1
        def react(self, t): return recover("rotate")
        def classify(self, t): return "rotate"
    monkeypatch.setattr(eng.Runner, "_ensure_keypool", lambda self: FakeKP())
    rc = eng.agent_with_retry_session_resume("prompt", "log4", "s1")
    assert rc.rc == 0
    assert len(_MOCK.calls) == 2
    # primary used the resume flag on s1 with original prompt
    assert "--resume" in _MOCK.calls[0][2]
    assert _MOCK.calls[0][0] == "prompt"
    # retry used "继续" + --resume s1 (continue branch)
    assert _MOCK.calls[1][0] == "继续"
    assert "--resume" in _MOCK.calls[1][2]
    assert "s1" in _MOCK.calls[1][2]


# ── fork entry: primary records session_id → continue (not re-fork) ──────

def test_session_fork_continue_on_recorded_session(monkeypatch):
    # fork primary records s-fork; retry continues on it (maintains fork
    # independence — does NOT re-fork from source, which would pollute).
    _MOCK.reset([
        {"events": [make_result("err", is_error=True, session_id="sfork")], "ok": False},
        {"events": [make_result()], "ok": True},
    ])
    class FakeKP:
        def init(self): pass
        def on_success(self): pass
        def rotate(self): pass
        def disable(self): pass
        def available_size(self): return 1
        def react(self, t): return recover("rotate")
        def classify(self, t): return "rotate"
    monkeypatch.setattr(eng.Runner, "_ensure_keypool", lambda self: FakeKP())
    rc = eng.agent_with_retry_session_fork("vote", "log5", "src")
    assert rc.rc == 0
    # primary forked from src; retry CONTINUED on the fork (继续 + --resume sfork)
    assert _MOCK.calls[1][0] == "继续"
    assert "--resume" in _MOCK.calls[1][2]
    assert "sfork" in _MOCK.calls[1][2]
    # the fork flag must NOT reappear in the retry (would re-fork from source)
    assert "--fork-session" not in _MOCK.calls[1][2]


# ── fork entry: no session recorded → replay primary verbatim (re-fork) ───

def test_session_fork_no_session_replays_redo(monkeypatch):
    # primary fails WITHOUT recording a session_id → retry re-forks from source.
    _MOCK.reset([
        {"events": [make_result("err", is_error=True)], "ok": False},  # no session_id
        {"events": [make_result()], "ok": True},
    ])
    class FakeKP:
        def init(self): pass
        def on_success(self): pass
        def rotate(self): pass
        def disable(self): pass
        def available_size(self): return 1
        def react(self, t): return recover("rotate")
        def classify(self, t): return "rotate"
    monkeypatch.setattr(eng.Runner, "_ensure_keypool", lambda self: FakeKP())
    rc = eng.agent_with_retry_session_fork("vote", "log6", "src")
    assert rc.rc == 0
    # retry re-sent the original prompt + the fork flag (re-fork from source)
    assert _MOCK.calls[1][0] == "vote"
    assert "--resume" in _MOCK.calls[1][2]
    assert "src" in _MOCK.calls[1][2]
    assert "--fork-session" in _MOCK.calls[1][2]


# ── agent_once_session_resume: single shot, self-disable ─────────────────

def test_once_session_resume_disable_on_failure(monkeypatch):
    _MOCK.reset([{"events": [make_result("err", is_error=True)], "ok": False}])
    disabled = {"n": 0}
    class FakeKP:
        def init(self): pass
        def on_success(self): pass
        def rotate(self): pass
        def disable(self): disabled["n"] += 1
        def available_size(self): return 1
        def react(self, t): return recover("disable,rotate")
        def classify(self, t): return "disable,rotate"
        def classify_details(self, t):
            return FakeErrorClassification(
                "disable,rotate",
                matched=True,
                resource_exhausted=True,
            )
    monkeypatch.setattr(eng.Runner, "_ensure_keypool", lambda self: FakeKP())
    # Make _kp_config point to an existing file so the disable branch fires
    # (otherwise it returns 2 = no key pool).
    (Path(os.environ["AR_RUN_DIR"]) / "cfg.jsonc").touch()
    monkeypatch.setattr(eng.Runner, "_kp_config", lambda self: str(Path(os.environ["AR_RUN_DIR"]) / "cfg.jsonc"))
    rc = eng.agent_once_session_resume("prompt", "log7", "s1")
    assert rc.rc == 1  # failed
    assert disabled["n"] == 1  # self-disabled (no outer loop)


def test_once_session_resume_quota_exhausted_no_keypool(monkeypatch):
    _MOCK.reset([{"events": [make_result("err", is_error=True)], "ok": False}])
    class FakeKP:
        def init(self): pass
        def on_success(self): pass
        def rotate(self): pass
        def disable(self): pass
        def available_size(self): return 0
        def react(self, t): return recover("disable,rotate")
        def classify(self, t): return "disable,rotate"
        def classify_details(self, t):
            return FakeErrorClassification(
                "disable,rotate",
                matched=True,
                resource_exhausted=True,
            )
    monkeypatch.setattr(eng.Runner, "_ensure_keypool", lambda self: FakeKP())
    # _kp_config → nonexistent file → disable branch returns 2
    monkeypatch.setattr(eng.Runner, "_kp_config", lambda self: "/nonexistent/cfg.jsonc")
    rc = eng.agent_once_session_resume("prompt", "log8", "s1")
    assert rc.rc == 2  # quota exhausted, no key pool


def test_once_session_resume_unclassified_no_keypool_is_not_quota_exhausted(
    capsys,
):
    secret = "once-backend-token-CANARY"
    _MOCK.reset([{
        "events": [
            make_result(
                f"ordinary backend failure token={secret}",
                is_error=True,
            ),
        ],
        "ok": False,
    }])

    result = eng.agent_once_session_resume(
        "prompt",
        "unclassified-once",
        "s1",
    )

    assert result.rc == 1
    assert result.outcome is eng.RunOutcome.FAILED
    diagnostic = capsys.readouterr().err
    assert "资源耗尽" not in diagnostic
    assert "后端执行失败" in diagnostic
    assert secret not in diagnostic
    assert secret not in repr(result)


# ── agent_with_retry alias ─────────────────────────────────────────────────

def test_agent_with_retry_alias(monkeypatch):
    """agent_with_retry == agent_with_retry_session_new (backward-compat)."""
    _MOCK.reset([{"events": [make_result()], "ok": True}])
    class FakeKP:
        def init(self): pass
        def on_success(self): pass
        def rotate(self): pass
        def disable(self): pass
        def available_size(self): return 0
        def react(self, t): return stop_recovery()
        def classify(self, t): return "rotate"
    monkeypatch.setattr(eng.Runner, "_ensure_keypool", lambda self: FakeKP())
    rc = eng.agent_with_retry("prompt", "log9")
    assert rc.rc == 0


def test_result_carries_session_id_and_text(monkeypatch):
    """库形态:成功调用的 Result 带 session_id 与结果文本。

    mock backend 的 invoke 写入带 session_id 的 result 事件;stream 把结果
    文本追加到 text_sink;engine 据此填 Result.session_id / Result.text。"""
    from agent_runner.engine import Result
    # 主试成功,带 session_id=sxyz,结果文本 "ok"
    _MOCK.reset([{"events": [make_result("ok", is_error=False, session_id="sxyz")], "ok": True}])
    class FakeKP:
        def init(self): pass
        def on_success(self): pass
        def rotate(self): pass
        def disable(self): pass
        def available_size(self): return 0
        def react(self, t): return stop_recovery()
        def classify(self, t): return "rotate"
    monkeypatch.setattr(eng.Runner, "_ensure_keypool", lambda self: FakeKP())
    res = eng.agent_with_retry_session_new("prompt", "logSid")
    assert isinstance(res, Result)
    assert res.rc == 0
    assert res.session_id == "sxyz"
    assert res.text == "ok"
    # int(Result) 与 bool(Result) 保持退出码习惯
    assert int(res) == 0
    assert bool(res) is True


def test_result_failure_has_no_session_id(monkeypatch):
    """库形态:失败时 Result.rc=1,session_id 与 text 为空。"""
    _MOCK.reset([{"events": [make_result("err", is_error=True)], "ok": False}])
    class FakeKP:
        def init(self): pass
        def on_success(self): pass
        def rotate(self): pass
        def disable(self): pass
        def available_size(self): return 0
        def react(self, t): return stop_recovery()
        def classify(self, t): return "rotate"
    monkeypatch.setattr(eng.Runner, "_ensure_keypool", lambda self: FakeKP())
    res = eng.agent_with_retry_session_new("prompt", "logFail")
    assert res.rc == 1
    assert res.session_id == ""
    assert res.text == ""
    assert int(res) == 1
    assert bool(res) is False
