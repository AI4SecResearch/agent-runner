from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent_runner.engine as engine
from agent_runner.backends import claude_code, opencode
from agent_runner.backends.claude_code import ClaudeCodeBackend
from agent_runner.backends.opencode import OpencodeBackend
from agent_runner.config import Config
from agent_runner.keypool import KeyContext


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
        return SimpleNamespace(action="rotate", stop_reason=None)

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


def _runner_with_backend(tmp_path, monkeypatch, backend_type, key_pool):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    runner = engine.Runner(
        config_overrides={
            "run_dir": str(run_dir),
            "stall_timeout": 5,
            "total_timeout": 0,
        },
        discover_config_files=False,
    )
    backend = backend_type(runner._config)
    monkeypatch.setattr(runner, "_get_backend", lambda: backend)
    monkeypatch.setattr(runner, "_ensure_keypool", lambda: key_pool)
    return runner, backend


@pytest.mark.parametrize(
    ("method_name", "arguments"),
    [
        ("agent_with_retry_session_new", ("prompt", "new")),
        (
            "agent_with_retry_session_resume",
            ("prompt", "resume", "source-session"),
        ),
        (
            "agent_with_retry_session_fork",
            ("prompt", "fork", "source-session"),
        ),
    ],
    ids=("new", "resume", "fork"),
)
def test_session_entry_uses_requested_working_directory(
    tmp_path,
    monkeypatch,
    method_name,
    arguments,
):
    working_directory = tmp_path / "workspace"
    working_directory.mkdir()
    runner, backend = _runner_with_backend(
        tmp_path,
        monkeypatch,
        _SuccessfulBackend,
        _KeyPool(),
    )

    result = getattr(runner, method_name)(
        *arguments,
        working_directory=working_directory,
    )

    assert result.rc == 0
    assert backend.working_directories == [working_directory]


@pytest.mark.parametrize(
    ("backend_module", "backend_type"),
    [
        (claude_code, ClaudeCodeBackend),
        (opencode, OpencodeBackend),
    ],
    ids=("claude-code", "opencode"),
)
def test_backend_process_starts_in_requested_working_directory(
    tmp_path,
    monkeypatch,
    backend_module,
    backend_type,
):
    working_directory = tmp_path / "workspace"
    working_directory.mkdir()
    popen_calls = []

    class _Process:
        pass

    def record_popen(command, **kwargs):
        popen_calls.append((command, kwargs))
        return _Process()

    monkeypatch.setattr(backend_module.subprocess, "Popen", record_popen)
    backend = backend_type()

    process = backend.invoke(
        "prompt",
        str(tmp_path / "logs" / backend_type.agent_id),
        [],
        working_directory=working_directory,
    )
    process._ar_err.close()

    assert popen_calls[0][1]["cwd"] == working_directory


def test_internal_retry_keeps_requested_working_directory(tmp_path, monkeypatch):
    working_directory = tmp_path / "workspace"
    working_directory.mkdir()
    runner, backend = _runner_with_backend(
        tmp_path,
        monkeypatch,
        _RetryBackend,
        _RetryKeyPool(),
    )

    result = runner.agent_with_retry_session_new(
        "prompt",
        "retry",
        working_directory=working_directory,
    )

    assert result.rc == 0
    assert backend.working_directories == [working_directory, working_directory]


def test_opencode_process_gets_private_config_without_changing_working_directory(
    tmp_path,
    monkeypatch,
):
    working_directory = tmp_path / "workspace"
    working_directory.mkdir()
    private_config = tmp_path / "private" / "opencode.json"
    private_config.parent.mkdir()
    private_config.write_text("{}\n", encoding="utf-8")
    host_home = tmp_path / "host-home"
    (host_home / ".opencode").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(host_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "host-config"))
    monkeypatch.setenv(
        "OPENCODE_CONFIG_CONTENT",
        '{"permission":{"*":"allow"}}',
    )
    monkeypatch.setenv("OPENCODE_PERMISSION", '{"*":"allow"}')
    monkeypatch.setenv("OPENCODE_FAKE_VCS", "git")
    popen_calls = []

    class _Process:
        pass

    def record_popen(command, **kwargs):
        popen_calls.append((command, kwargs))
        return _Process()

    monkeypatch.setattr(opencode.subprocess, "Popen", record_popen)
    backend = OpencodeBackend(
        config=Config(
            config_overrides={
                "opencode_config": str(private_config),
                "opencode_auth_env_var": "Z_AI_API_KEY",
            },
            toml={},
        )
    )

    process = backend.invoke(
        "prompt",
        str(tmp_path / "logs" / "opencode"),
        ["--dir", str(working_directory)],
        key_ctx=KeyContext(key="fixture-key"),
        working_directory=working_directory,
    )
    process._ar_err.close()

    assert popen_calls[0][1]["cwd"] == working_directory
    process_env = popen_calls[0][1]["env"]
    private_root = private_config.parent.parent
    assert process_env["OPENCODE_CONFIG"] == str(private_config)
    assert process_env["OPENCODE_CONFIG_DIR"] == str(private_config.parent)
    assert process_env["HOME"] == str(private_root)
    assert process_env["XDG_CONFIG_HOME"] == str(private_root)
    assert process_env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"
    assert process_env["OPENCODE_DISABLE_EXTERNAL_SKILLS"] == "1"
    assert process_env["OPENCODE_DISABLE_CLAUDE_CODE"] == "1"
    assert process_env["OPENCODE_DISABLE_CLAUDE_CODE_SKILLS"] == "1"
    assert process_env["OPENCODE_DISABLE_DEFAULT_PLUGINS"] == "1"
    assert process_env["Z_AI_API_KEY"] == "fixture-key"
    assert "OPENCODE_CONFIG_CONTENT" not in process_env
    assert "OPENCODE_PERMISSION" not in process_env
    assert "OPENCODE_FAKE_VCS" not in process_env


def test_opencode_without_private_config_keeps_legacy_environment(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENCODE_PERMISSION", '{"*":"allow"}')
    backend = OpencodeBackend(
        config=Config(
            config_overrides={
                "opencode_config": "",
                "opencode_auth_env_var": "Z_AI_API_KEY",
            },
            toml={},
        )
    )

    assert backend._build_env(None) is None
    process_env = backend._build_env(KeyContext(key="fixture-key"))
    assert process_env is not None
    assert process_env["OPENCODE_PERMISSION"] == '{"*":"allow"}'
    assert process_env["Z_AI_API_KEY"] == "fixture-key"


def test_private_config_roots_share_host_session_storage(
    tmp_path,
    monkeypatch,
) -> None:
    host_home = tmp_path / "host-home"
    monkeypatch.setenv("HOME", str(host_home))
    for name in (
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
    ):
        monkeypatch.delenv(name, raising=False)

    environments = []
    for execution_id in ("execution-1", "execution-2"):
        private_config = (
            tmp_path
            / execution_id
            / "backend"
            / "opencode"
            / ".opencode"
            / "opencode.json"
        )
        backend = OpencodeBackend(
            config=Config(
                config_overrides={
                    "opencode_config": str(private_config),
                },
                toml={},
            )
        )
        environments.append(backend._build_env(None))

    first, second = environments
    assert first is not None
    assert second is not None
    assert first["HOME"] != second["HOME"]
    for name, expected in (
        ("XDG_DATA_HOME", host_home / ".local" / "share"),
        ("XDG_STATE_HOME", host_home / ".local" / "state"),
        ("XDG_CACHE_HOME", host_home / ".cache"),
    ):
        assert first[name] == str(expected)
        assert second[name] == str(expected)
