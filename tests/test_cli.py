"""CLI / process-mode tests — verify ``python -m agent_runner`` dispatches to
the right engine function with the right arg ordering, and returns the engine's
exit code.

These call ``main()`` directly with monkeypatched engine functions, so no real
agent binary is involved. The arg-ordering and exit-code-mapping contracts are
the point — the engine functions themselves are covered by test_engine.py.

Note: ``main()`` receives the INTERNAL form ``<entry> <model_tier> <prompt>
<log_name> [session_id] [-- <passthrough>]`` — the user-facing ``--tier`` flag
is parsed by ``agent-runner.sh`` (covered separately), which injects model_tier
as argv[1].
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import agent_runner.engine as eng  # noqa: E402
from agent_runner.__main__ import main  # noqa: E402


def test_help_returns_zero(capsys):
    rc = main(["--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "new" in out and "resume" in out and "fork" in out


def test_unknown_entry_returns_2(capsys):
    rc = main(["bogus", "primary", "p", "l"])
    assert rc == 2


def test_missing_model_tier_returns_2(capsys, monkeypatch):
    """entry 之后必须有 model_tier(argv[1],由 agent-runner.sh 注入)。"""
    monkeypatch.setattr(eng, "agent_with_retry_session_new", lambda *a, **k: 0)
    rc = main(["new"])  # 只有 entry,缺 model_tier
    assert rc == 2
    assert "model_tier" in capsys.readouterr().err


def test_new_dispatches_to_session_new(monkeypatch):
    calls = []
    monkeypatch.setattr(eng, "agent_with_retry_session_new",
                        lambda prompt, log, model_tier, passthrough:
                        (calls.append((prompt, log, model_tier, passthrough)), 0)[1])
    rc = main(["new", "primary", "the prompt", "log1"])
    assert rc == 0
    assert calls == [("the prompt", "log1", "primary", ())]


def test_new_alias_agent_with_retry(monkeypatch):
    calls = []
    monkeypatch.setattr(eng, "agent_with_retry_session_new",
                        lambda prompt, log, model_tier, passthrough:
                        (calls.append((model_tier, passthrough)), 0)[1])
    rc = main(["agent_with_retry", "primary", "p", "l"])
    assert rc == 0
    assert calls == [("primary", ())]


def test_new_missing_log_returns_2(capsys, monkeypatch):
    monkeypatch.setattr(eng, "agent_with_retry_session_new", lambda *a, **k: 0)
    rc = main(["new", "primary", "p"])  # missing log_name
    assert rc == 2
    assert "needs" in capsys.readouterr().err


def test_resume_requires_session_id(capsys, monkeypatch):
    monkeypatch.setattr(eng, "agent_with_retry_session_resume", lambda *a, **k: 0)
    rc = main(["resume", "primary", "p", "l"])  # missing sid
    assert rc == 2
    assert "session_id" in capsys.readouterr().err


def test_resume_dispatches_with_sid_and_passthrough(monkeypatch):
    calls = []
    monkeypatch.setattr(eng, "agent_with_retry_session_resume",
                        lambda prompt, log, sid, model_tier, passthrough:
                        (calls.append((prompt, log, sid, model_tier, passthrough)), 0)[1])
    rc = main(["resume", "primary", "p", "l", "sid-9", "--", "--model", "x"])
    assert rc == 0
    assert calls == [("p", "l", "sid-9", "primary", ("--model", "x"))]


def test_fork_dispatches(monkeypatch):
    calls = []
    monkeypatch.setattr(eng, "agent_with_retry_session_fork",
                        lambda prompt, log, sid, model_tier, passthrough:
                        (calls.append((sid, model_tier, passthrough)), 0)[1])
    rc = main(["fork", "primary", "p", "l", "sid-1"])
    assert rc == 0
    assert calls == [("sid-1", "primary", ())]


def test_passthrough_after_dashdash(monkeypatch):
    """'--' 之后的参数原样作为 passthrough 透传给 agent。"""
    calls = []
    monkeypatch.setattr(eng, "agent_with_retry_session_new",
                        lambda prompt, log, model_tier, passthrough:
                        (calls.append(passthrough), 0)[1])
    rc = main(["new", "primary", "p", "l", "--", "--model", "x", "--verbose"])
    assert rc == 0
    assert calls == [("--model", "x", "--verbose")]


def test_subprocess_invocation_uses_main_module(tmp_path, monkeypatch):
    """`python -m agent_runner --help` exits 0 and prints usage (proves the
    __main__ entry is wired so the .sh wrapper's `exec python3 -m agent_runner`
    works)."""
    import subprocess, os
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    r = subprocess.run(
        [sys.executable, "-m", "agent_runner", "--help"],
        capture_output=True, text=True, env=env,
    )
    assert r.returncode == 0, r.stderr
    assert "new" in r.stdout


def test_stdout_carries_session_id(monkeypatch, capsys):
    """进程形态的通道分工:$? = 成败,stdout = session_id 一行。

    bash 上层靠 `sid=$(agent-runner.sh new ...)` 取 session_id,靠 $? 判成败。
    这里 mock engine 返回带 session_id 的 Result,断言 main() 把 session_id
    写到 stdout、退出码取自 rc。"""
    from agent_runner.engine import Result
    monkeypatch.setattr(
        eng, "agent_with_retry_session_new",
        lambda prompt, log, model_tier, passthrough:
        Result(0, session_id="sid-abc-123", text="agent answer"),
    )
    rc = main(["new", "primary", "p", "l"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out == "sid-abc-123\n"  # 一行 session_id,无别的


def test_stdout_empty_when_no_session_id(monkeypatch, capsys):
    """失败或后端未记录 session 时,stdout 为空行,退出码反映失败。"""
    from agent_runner.engine import Result
    monkeypatch.setattr(
        eng, "agent_with_retry_session_new",
        lambda prompt, log, model_tier, passthrough: Result(1),
    )
    rc = main(["new", "primary", "p", "l"])
    assert rc == 1
    assert capsys.readouterr().out == "\n"


def test_int_result_still_works(monkeypatch, capsys):
    """engine 函数若返回裸 int(向后兼容),main() 也能处理:session_id 为空。"""
    monkeypatch.setattr(eng, "agent_with_retry_session_new",
                        lambda prompt, log, model_tier, passthrough: 0)
    rc = main(["new", "primary", "p", "l"])
    assert rc == 0
    assert capsys.readouterr().out == "\n"


# ── model_tier(argv[1],由 agent-runner.sh 从 --tier 注入) ────────────────


def test_model_tier_downgrade_forwarded(monkeypatch):
    """argv[1]=downgrade → engine 收到 model_tier="downgrade"。"""
    calls = []
    monkeypatch.setattr(eng, "agent_with_retry_session_new",
                        lambda prompt, log, model_tier, passthrough:
                        (calls.append(model_tier), 0)[1])
    rc = main(["new", "downgrade", "p", "l"])
    assert rc == 0
    assert calls == ["downgrade"]


def test_model_tier_passthrough_independent(monkeypatch):
    """model_tier 与 passthrough 各自独立:downgrade + 透传并存。"""
    calls = []
    monkeypatch.setattr(eng, "agent_with_retry_session_new",
                        lambda prompt, log, model_tier, passthrough:
                        (calls.append((model_tier, passthrough)), 0)[1])
    rc = main(["new", "downgrade", "p", "l", "--", "--model", "x"])
    assert rc == 0
    assert calls == [("downgrade", ("--model", "x"))]
