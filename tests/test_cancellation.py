from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent_runner.engine as engine
from agent_runner import CancellationSource, Result, RunOutcome, Runner
from agent_runner import platform as runner_platform


class _Cancellation:
    def __init__(self, requested: bool = False) -> None:
        self.requested = requested

    @property
    def is_cancellation_requested(self) -> bool:
        return self.requested


class _KeyPool:
    def init(self) -> None:
        return None

    def on_success(self) -> None:
        return None

    def available_size(self) -> int:
        return 0

    def react(self, text: str):
        del text
        return SimpleNamespace(
            action="",
            stop_reason=SimpleNamespace(value="no_actionable_recovery"),
        )


class _ProcessBackend:
    agent_id = "process-test"

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.processes: list[subprocess.Popen[str]] = []

    def perm_args(self) -> list[str]:
        return []

    def model_args(
        self,
        model: str,
        *,
        resolved_model: str = "",
    ) -> list[str]:
        del model, resolved_model
        return []

    def invoke(
        self,
        prompt: str,
        prefix: str,
        argv: list[str],
        key_ctx: object = None,
    ) -> subprocess.Popen[str]:
        del prompt, prefix, argv, key_ctx
        parent_pid_path = self.directory / "parent.pid"
        child_pid_path = self.directory / "child.pid"
        late_success_path = self.directory / "late-success.txt"
        child_code = (
            "import os,time,pathlib;"
            f"pathlib.Path({str(child_pid_path)!r}).write_text(str(os.getpid()));"
            "time.sleep(0.5);"
            f"pathlib.Path({str(late_success_path)!r}).write_text('late success');"
            "time.sleep(30)"
        )
        code = (
            "import os,subprocess,sys,time,pathlib;"
            f"pathlib.Path({str(parent_pid_path)!r}).write_text(str(os.getpid()));"
            f"subprocess.Popen([sys.executable,'-c',{child_code!r}]);"
            "time.sleep(30)"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **runner_platform.PLATFORM.new_session_kwargs(),
        )
        self.processes.append(proc)
        return proc

    def stream(self, proc: subprocess.Popen[str], prefix: str) -> None:
        del prefix
        proc.communicate()

    def is_complete(self, prefix: str) -> bool:
        del prefix
        return False

    def result_ok(self, prefix: str) -> bool:
        del prefix
        return False

    def result_text(self, prefix: str) -> str:
        del prefix
        return "backend failed"

    def session_id(self, prefix: str) -> str:
        del prefix
        return ""

    def resume_args(self, session_id: str) -> list[str]:
        del session_id
        return []


class _KillSpy:
    def __init__(self, delegate: object) -> None:
        self.delegate = delegate
        self.calls = 0

    @property
    def supports_process_tree_kill(self) -> bool:
        return True

    def new_session_kwargs(self) -> dict[str, bool]:
        return self.delegate.new_session_kwargs()

    def kill_tree(self, proc: subprocess.Popen[str]) -> None:
        self.calls += 1
        self.delegate.kill_tree(proc)


def test_cancellation_before_start_does_not_spawn(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = Runner(
        config_overrides={
            "backend": "claude-code",
            "run_dir": str(tmp_path),
        },
        discover_config_files=False,
    )
    monkeypatch.setattr(runner, "_ensure_keypool", lambda: _KeyPool())

    def fail_spawn(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("backend must not spawn after cancellation")

    monkeypatch.setattr(runner, "_agent_once", fail_spawn)
    cancellation: CancellationSource = _Cancellation(requested=True)

    result = runner.agent_with_retry_session_new(
        "prompt",
        "run",
        cancellation=cancellation,
    )

    assert result == Result.canceled()


def test_cancellation_observed_after_key_selection_does_not_spawn(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cancellation = _Cancellation()
    runner = Runner(
        config_overrides={
            "backend": "claude-code",
            "run_dir": str(tmp_path),
        },
        discover_config_files=False,
    )

    class _CancelingKeyPool(_KeyPool):
        def init(self) -> None:
            cancellation.requested = True
            return None

    monkeypatch.setattr(
        runner,
        "_ensure_keypool",
        lambda: _CancelingKeyPool(),
    )

    def fail_spawn(*args: object, **kwargs: object) -> Result:
        del args, kwargs
        raise AssertionError("backend must not spawn after cancellation")

    monkeypatch.setattr(runner, "_agent_once_with_check", fail_spawn)

    result = runner.agent_with_retry_session_new(
        "prompt",
        "run",
        cancellation=cancellation,
    )

    assert result.outcome is RunOutcome.CANCELED


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group proof")
def test_running_cancellation_kills_group_and_reaps_before_return(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cancellation = _Cancellation()
    backend = _ProcessBackend(tmp_path)
    runner = Runner(
        config_overrides={
            "backend": "claude-code",
            "run_dir": str(tmp_path),
            "stall_timeout": 0,
            "total_timeout": 0,
        },
        discover_config_files=False,
    )
    key_pool = _KeyPool()
    monkeypatch.setattr(runner, "_ensure_keypool", lambda: key_pool)
    monkeypatch.setattr(runner, "_get_backend", lambda: backend)
    monkeypatch.setattr(engine, "_WATCHDOG_POLL_SECONDS", 0.01)
    kill_spy = _KillSpy(runner_platform.PLATFORM)
    monkeypatch.setattr(runner_platform, "PLATFORM", kill_spy)

    result_box: list[Result] = []
    worker = threading.Thread(
        target=lambda: result_box.append(
            runner.agent_with_retry_session_new(
                "prompt",
                "run",
                cancellation=cancellation,
            )
        )
    )
    worker.start()
    deadline = time.monotonic() + 5
    while not (tmp_path / "child.pid").exists():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    parent_pid = int((tmp_path / "parent.pid").read_text())
    parent_pgid = os.getpgid(parent_pid)

    cancellation.requested = True
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert result_box[0].outcome is RunOutcome.CANCELED
    assert kill_spy.calls == 1
    assert backend.processes[0].poll() is not None
    # The runner reaps its direct child; init reaps killed grandchildren.
    # A transient zombie keeps the group visible even though it cannot run.
    reap_deadline = time.monotonic() + 5
    while True:
        try:
            os.killpg(parent_pgid, 0)
        except (ProcessLookupError, OSError):
            break
        assert time.monotonic() < reap_deadline, "killed process group was not reaped"
        time.sleep(0.01)
    with pytest.raises(ChildProcessError):
        os.waitpid(parent_pid, os.WNOHANG)
    time.sleep(0.6)
    assert not (tmp_path / "late-success.txt").exists()


def test_cancellation_after_failed_attempt_prevents_retry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cancellation = _Cancellation()
    runner = Runner(
        config_overrides={
            "backend": "claude-code",
            "run_dir": str(tmp_path),
        },
        discover_config_files=False,
    )
    monkeypatch.setattr(runner, "_ensure_keypool", lambda: _KeyPool())
    calls = 0

    def fail_once(*args: object, **kwargs: object) -> Result:
        nonlocal calls
        del args, kwargs
        calls += 1
        cancellation.requested = True
        return Result(1)

    monkeypatch.setattr(runner, "_agent_once_with_check", fail_once)

    result = runner.agent_with_retry_session_new(
        "prompt",
        "run",
        cancellation=cancellation,
    )

    assert result.outcome is RunOutcome.CANCELED
    assert calls == 1


def test_cancellation_observed_during_retry_decision_prevents_spawn(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cancellation = _Cancellation()
    runner = Runner(
        config_overrides={
            "backend": "claude-code",
            "run_dir": str(tmp_path),
        },
        discover_config_files=False,
    )

    class _CancelingKeyPool(_KeyPool):
        def react(self, text: str):
            del text
            cancellation.requested = True
            return SimpleNamespace(action="rotate", stop_reason=None)

        def rotate(self) -> None:
            return None

    monkeypatch.setattr(
        runner,
        "_ensure_keypool",
        lambda: _CancelingKeyPool(),
    )
    calls = 0

    def fail_attempt(*args: object, **kwargs: object) -> Result:
        nonlocal calls
        del args, kwargs
        calls += 1
        return Result(1)

    monkeypatch.setattr(runner, "_agent_once_with_check", fail_attempt)
    monkeypatch.setattr(
        runner,
        "_get_backend",
        lambda: type(
            "Backend",
            (),
            {
                "result_text": lambda self, prefix: "failed",
                "session_id": lambda self, prefix: "",
                "resume_args": lambda self, sid: [],
            },
        )(),
    )

    result = runner.agent_with_retry_session_new(
        "prompt",
        "run",
        cancellation=cancellation,
    )

    assert result.outcome is RunOutcome.CANCELED
    assert calls == 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX watchdog proof")
@pytest.mark.parametrize(
    ("stall_timeout", "attempt_timeout", "expected"),
    [
        (1, 0, RunOutcome.STALL_TIMEOUT),
        (0, 1, RunOutcome.ATTEMPT_TIMEOUT),
    ],
)
def test_watchdog_timeout_outcomes_remain_distinct(
    tmp_path: Path,
    monkeypatch,
    stall_timeout: int,
    attempt_timeout: int,
    expected: RunOutcome,
) -> None:
    backend = _ProcessBackend(tmp_path)
    runner = Runner(
        config_overrides={
            "backend": "claude-code",
            "run_dir": str(tmp_path),
            "stall_timeout": stall_timeout,
            "total_timeout": attempt_timeout,
        },
        discover_config_files=False,
    )
    monkeypatch.setattr(runner, "_ensure_keypool", lambda: _KeyPool())
    monkeypatch.setattr(runner, "_get_backend", lambda: backend)
    clock = iter((0.0, 2.0))
    monkeypatch.setattr(engine.time, "time", lambda: next(clock))

    result = runner.agent_with_retry_session_new("prompt", "run")

    assert result.outcome is expected
    assert backend.processes[0].poll() is not None
