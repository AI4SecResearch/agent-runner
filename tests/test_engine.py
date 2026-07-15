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
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import agent_runner.engine as eng  # noqa: E402
from agent_runner.backends.claude_code import ClaudeCodeBackend  # noqa: E402


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
        self._idx = 0

    def invoke(self, prompt, prefix, argv):
        self.calls.append((prompt, prefix, list(argv)))
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
        self._idx = 0


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("AR_RUN_DIR", str(tmp_path))
    monkeypatch.setenv("AR_BACKEND", "claude-code")
    monkeypatch.setenv("AR_STALL_TIMEOUT", "5")
    monkeypatch.setenv("AR_TOTAL_TIMEOUT", "0")
    # config 层有缓存;setenv 后清缓存让它重解析。
    from agent_runner import config as _cfg
    _cfg.clear_cache()
    # Install the mock backend into the engine's module-level state.
    eng._backend = None
    eng._backend_name = None
    eng._kp = None
    eng._kp_current_env_var = None
    # Monkeypatch get_backend to return our mock.
    monkeypatch.setattr(eng, "_get_backend", lambda: _MOCK)
    yield
    _MOCK.calls.clear()
    _cfg.clear_cache()


# module-singleton mock backend; reset() before each test to clear the call
# counter and load the new script.
_MOCK = MockBackend()


def make_result(text="ok", is_error=False, session_id=""):
    e = {"type": "result", "result": text, "is_error": is_error}
    if session_id:
        e["session_id"] = session_id
    return e


class _FakeProc:
    """Already-exited process: poll() returns 0 (the watchdog sees the process
    has exited and falls through to wait/return). returncode is 0."""
    returncode = 0
    pid = -1

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


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
        def react(self, t): return "stop"
        def classify(self, t): return "rotate"
    monkeypatch.setattr(eng, "_ensure_keypool", lambda: FakeKP())
    monkeypatch.setenv("AR_PRIMARY_MODEL", "test-model")
    from agent_runner import config as _cfg
    _cfg.clear_cache()
    rc = eng.agent_with_retry_session_new("prompt", "log1")
    assert rc.rc == 0
    assert len(_MOCK.calls) == 1
    assert _MOCK.calls[0][0] == "prompt"  # the prompt
    # one --model (primary,来自 AR_PRIMARY_MODEL),无 resume args
    argv = _MOCK.calls[0][2]
    assert argv.count("--model") == 1


# ── all retries fail → exit 1 ─────────────────────────────────────────────

def test_session_new_all_fail(monkeypatch):
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
        def react(self, t): return "rotate"  # always rotate, never stop
        def classify(self, t): return "rotate"
    kp = FakeKP()
    monkeypatch.setattr(eng, "_ensure_keypool", lambda: kp)
    rc = eng.agent_with_retry_session_new("prompt", "log2")
    assert rc.rc == 1


# ── react returns stop → exit 1 ───────────────────────────────────────────

def test_session_new_react_stop(monkeypatch):
    _MOCK.reset([{"events": [make_result("err", is_error=True)], "ok": False}])
    class FakeKP:
        def init(self): pass
        def on_success(self): pass
        def rotate(self): pass
        def disable(self): pass
        def available_size(self): return 1
        def react(self, t): return "stop"
        def classify(self, t): return "stop"
    monkeypatch.setattr(eng, "_ensure_keypool", lambda: FakeKP())
    rc = eng.agent_with_retry_session_new("prompt", "log3")
    assert rc.rc == 1
    assert len(_MOCK.calls) == 1  # only the primary; retry loop stopped before retrying


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
        def react(self, t): return "rotate"
        def classify(self, t): return "rotate"
    monkeypatch.setattr(eng, "_ensure_keypool", lambda: FakeKP())
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
        def react(self, t): return "rotate"
        def classify(self, t): return "rotate"
    monkeypatch.setattr(eng, "_ensure_keypool", lambda: FakeKP())
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
        def react(self, t): return "rotate"
        def classify(self, t): return "rotate"
    monkeypatch.setattr(eng, "_ensure_keypool", lambda: FakeKP())
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
        def react(self, t): return "disable,rotate"
        def classify(self, t): return "disable,rotate"
    monkeypatch.setattr(eng, "_ensure_keypool", lambda: FakeKP())
    # Make _kp_config point to an existing file so the disable branch fires
    # (otherwise it returns 2 = no key pool).
    (Path(os.environ["AR_RUN_DIR"]) / "cfg.jsonc").touch()
    monkeypatch.setattr(eng, "_kp_config", lambda: str(Path(os.environ["AR_RUN_DIR"]) / "cfg.jsonc"))
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
        def react(self, t): return "disable,rotate"
        def classify(self, t): return "disable,rotate"
    monkeypatch.setattr(eng, "_ensure_keypool", lambda: FakeKP())
    # _kp_config → nonexistent file → disable branch returns 2
    monkeypatch.setattr(eng, "_kp_config", lambda: "/nonexistent/cfg.jsonc")
    rc = eng.agent_once_session_resume("prompt", "log8", "s1")
    assert rc.rc == 2  # quota exhausted, no key pool


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
        def react(self, t): return "stop"
        def classify(self, t): return "rotate"
    monkeypatch.setattr(eng, "_ensure_keypool", lambda: FakeKP())
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
        def react(self, t): return "stop"
        def classify(self, t): return "rotate"
    monkeypatch.setattr(eng, "_ensure_keypool", lambda: FakeKP())
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
        def react(self, t): return "stop"
        def classify(self, t): return "rotate"
    monkeypatch.setattr(eng, "_ensure_keypool", lambda: FakeKP())
    res = eng.agent_with_retry_session_new("prompt", "logFail")
    assert res.rc == 1
    assert res.session_id == ""
    assert res.text == ""
    assert int(res) == 1
    assert bool(res) is False
