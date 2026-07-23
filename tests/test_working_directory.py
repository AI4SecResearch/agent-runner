from __future__ import annotations

import json
from pathlib import Path

import agent_runner.engine as engine
from agent_runner.backends import claude_code, opencode
from agent_runner.backends.claude_code import ClaudeCodeBackend
from agent_runner.backends.opencode import OpencodeBackend


class _ExitedProcess:
    returncode = 0

    def poll(self):
        return 0


class _SuccessfulBackend(ClaudeCodeBackend):
    def __init__(self, config):
        super().__init__(config=config)
        self.working_directories: list[Path | None] = []

    def invoke(
        self,
        prompt,
        prefix,
        argv,
        key_ctx=None,
        *,
        working_directory=None,
    ):
        self.working_directories.append(working_directory)
        Path(f"{prefix}.jsonl").write_text(
            json.dumps(
                {
                    "type": "result",
                    "result": "ok",
                    "is_error": False,
                    "session_id": "session-1",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return _ExitedProcess()

    def stream(self, proc, prefix):
        return None


class _KeyPool:
    def init(self):
        return None

    def on_success(self):
        return None


class _RetryKeyPool(_KeyPool):
    def available_size(self):
        return 1

    def react(self, text):
        return "rotate"

    def rotate(self):
        return None


class _RetryBackend(_SuccessfulBackend):
    def invoke(
        self,
        prompt,
        prefix,
        argv,
        key_ctx=None,
        *,
        working_directory=None,
    ):
        self.working_directories.append(working_directory)
        failed = len(self.working_directories) == 1
        Path(f"{prefix}.jsonl").write_text(
            json.dumps(
                {
                    "type": "result",
                    "result": "error" if failed else "ok",
                    "is_error": failed,
                    "session_id": "" if failed else "session-1",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return _ExitedProcess()


def test_session_new_uses_requested_working_directory(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    working_directory = tmp_path / "workspace"
    working_directory.mkdir()
    runner = engine.Runner(
        config_overrides={
            "run_dir": str(run_dir),
            "stall_timeout": 5,
            "total_timeout": 0,
        },
        discover_config_files=False,
    )
    backend = _SuccessfulBackend(runner._config)
    monkeypatch.setattr(runner, "_get_backend", lambda: backend)
    monkeypatch.setattr(runner, "_ensure_keypool", lambda: _KeyPool())

    result = runner.agent_with_retry_session_new(
        "prompt",
        "new",
        working_directory=working_directory,
    )

    assert result.rc == 0
    assert backend.working_directories == [working_directory]


def test_claude_process_starts_in_requested_working_directory(
    tmp_path,
    monkeypatch,
):
    working_directory = tmp_path / "workspace"
    working_directory.mkdir()
    popen_calls = []

    class _Process:
        pass

    def record_popen(command, **kwargs):
        popen_calls.append((command, kwargs))
        return _Process()

    monkeypatch.setattr(claude_code.subprocess, "Popen", record_popen)
    backend = ClaudeCodeBackend()

    process = backend.invoke(
        "prompt",
        str(tmp_path / "logs" / "claude"),
        [],
        working_directory=working_directory,
    )
    process._ar_err.close()

    assert popen_calls[0][1]["cwd"] == working_directory


def test_opencode_process_starts_in_requested_working_directory(
    tmp_path,
    monkeypatch,
):
    working_directory = tmp_path / "workspace"
    working_directory.mkdir()
    popen_calls = []

    class _Process:
        pass

    def record_popen(command, **kwargs):
        popen_calls.append((command, kwargs))
        return _Process()

    monkeypatch.setattr(opencode.subprocess, "Popen", record_popen)
    backend = OpencodeBackend()

    process = backend.invoke(
        "prompt",
        str(tmp_path / "logs" / "opencode"),
        [],
        working_directory=working_directory,
    )
    process._ar_err.close()

    assert popen_calls[0][1]["cwd"] == working_directory


def test_session_resume_uses_requested_working_directory(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    working_directory = tmp_path / "workspace"
    working_directory.mkdir()
    runner = engine.Runner(
        config_overrides={
            "run_dir": str(run_dir),
            "stall_timeout": 5,
            "total_timeout": 0,
        },
        discover_config_files=False,
    )
    backend = _SuccessfulBackend(runner._config)
    monkeypatch.setattr(runner, "_get_backend", lambda: backend)
    monkeypatch.setattr(runner, "_ensure_keypool", lambda: _KeyPool())

    result = runner.agent_with_retry_session_resume(
        "prompt",
        "resume",
        "source-session",
        working_directory=working_directory,
    )

    assert result.rc == 0
    assert backend.working_directories == [working_directory]


def test_session_fork_uses_requested_working_directory(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    working_directory = tmp_path / "workspace"
    working_directory.mkdir()
    runner = engine.Runner(
        config_overrides={
            "run_dir": str(run_dir),
            "stall_timeout": 5,
            "total_timeout": 0,
        },
        discover_config_files=False,
    )
    backend = _SuccessfulBackend(runner._config)
    monkeypatch.setattr(runner, "_get_backend", lambda: backend)
    monkeypatch.setattr(runner, "_ensure_keypool", lambda: _KeyPool())

    result = runner.agent_with_retry_session_fork(
        "prompt",
        "fork",
        "source-session",
        working_directory=working_directory,
    )

    assert result.rc == 0
    assert backend.working_directories == [working_directory]


def test_internal_retry_keeps_requested_working_directory(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    working_directory = tmp_path / "workspace"
    working_directory.mkdir()
    runner = engine.Runner(
        config_overrides={
            "run_dir": str(run_dir),
            "stall_timeout": 5,
            "total_timeout": 0,
        },
        discover_config_files=False,
    )
    backend = _RetryBackend(runner._config)
    monkeypatch.setattr(runner, "_get_backend", lambda: backend)
    monkeypatch.setattr(runner, "_ensure_keypool", lambda: _RetryKeyPool())

    result = runner.agent_with_retry_session_new(
        "prompt",
        "retry",
        working_directory=working_directory,
    )

    assert result.rc == 0
    assert backend.working_directories == [working_directory, working_directory]
