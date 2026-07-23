from __future__ import annotations

import io
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
import inspect
from pathlib import Path

import pytest
from llm_provider_manager import config as lpm_config
import llm_provider_manager.keypool as lpm_keypool

from agent_runner.backends.claude_code import ClaudeCodeBackend
from agent_runner.config import Config
from agent_runner.engine import Runner
from agent_runner.keypool import KeyContext, KeyPool as RunnerKeyPool
from agent_runner.keypool import _VerifiedProviderSnapshot


class _ExitedProcess:
    returncode = 0
    pid = -1
    _ar_err = None

    def __init__(self) -> None:
        self.stdout = io.StringIO(
            json.dumps(
                {
                    "type": "result",
                    "result": "raw diagnostic",
                    "is_error": False,
                }
            )
            + "\n"
        )

    def poll(self) -> int:
        return 0

    def wait(self, timeout=None) -> int:
        del timeout
        return 0


def test_key_pool_debug_diagnostics_never_include_raw_key_material(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    secret = "CANARY11"
    provider_path = tmp_path / "providers.jsonc"
    provider_path.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "id": "provider-a",
                        "type": "symmetric",
                        "baseURLs": {
                            "anthropic": "https://example.test/anthropic",
                        },
                        "keys": [{"id": "key-a", "key": secret}],
                        "models": [
                            {
                                "id": "model-a",
                                "context": 4096,
                                "output": 1024,
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    state_path = tmp_path / "key-pool-state.json"
    key_pool = RunnerKeyPool(
        str(provider_path),
        str(state_path),
        agent_id="claude",
        config=Config(config_overrides={}, toml={}),
    )
    import llm_provider_manager.keypool as lpm_keypool

    monkeypatch.setattr(lpm_keypool, "DEBUG", True)

    key_pool.init()

    assert secret not in capsys.readouterr().err
    assert state_path.stat().st_mode & 0o777 == 0o600


def test_backend_raw_diagnostics_are_created_owner_only(
    tmp_path: Path,
    monkeypatch,
) -> None:
    process = _ExitedProcess()
    monkeypatch.setattr(
        "agent_runner.backends.claude_code.subprocess.Popen",
        lambda *args, **kwargs: process,
    )
    backend = ClaudeCodeBackend()
    prefix = str(tmp_path / "diagnostics" / "run")

    invoked = backend.invoke(
        "prompt",
        prefix,
        [],
        key_ctx=KeyContext(key="selected-key"),
    )
    backend.stream(invoked, prefix)

    assert (tmp_path / "diagnostics" / "run.err").stat().st_mode & 0o777 == 0o600
    assert (
        (tmp_path / "diagnostics" / "run.jsonl").stat().st_mode & 0o777
        == 0o600
    )


def test_backend_raw_diagnostics_remain_owner_only_under_restrictive_umask(
    tmp_path: Path,
    monkeypatch,
) -> None:
    process = _ExitedProcess()
    monkeypatch.setattr(
        "agent_runner.backends.claude_code.subprocess.Popen",
        lambda *args, **kwargs: process,
    )
    backend = ClaudeCodeBackend()
    (tmp_path / "diagnostics").mkdir()
    prefix = str(tmp_path / "diagnostics" / "restrictive")
    previous_umask = os.umask(0o777)
    try:
        invoked = backend.invoke(
            "prompt",
            prefix,
            [],
            key_ctx=KeyContext(key="selected-key"),
        )
        backend.stream(invoked, prefix)
    finally:
        os.umask(previous_umask)

    assert (
        (tmp_path / "diagnostics" / "restrictive.err").stat().st_mode
        & 0o777
        == 0o600
    )
    assert (
        (tmp_path / "diagnostics" / "restrictive.jsonl").stat().st_mode
        & 0o777
        == 0o600
    )


def test_key_pool_rejects_symlink_state_file_before_reading_or_writing(
    tmp_path: Path,
) -> None:
    provider_path = tmp_path / "providers.jsonc"
    provider_path.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "id": "provider-a",
                        "type": "symmetric",
                        "baseURLs": {
                            "anthropic": "https://example.test/anthropic",
                        },
                        "keys": [{"id": "key-a", "key": "selected-key"}],
                        "models": [
                            {
                                "id": "model-a",
                                "context": 4096,
                                "output": 1024,
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    real_state = tmp_path / "real-state.json"
    real_state.write_text(
        '{"current_index": 0, "disabled": {}, "success_count": 0}',
        encoding="utf-8",
    )
    state_path = tmp_path / "state.json"
    state_path.symlink_to(real_state)
    key_pool = RunnerKeyPool(
        str(provider_path),
        str(state_path),
        agent_id="claude",
        config=Config(config_overrides={}, toml={}),
    )

    with pytest.raises(OSError):
        key_pool.init()


def test_runner_uses_held_provider_descriptor_for_real_child_spawn(
    tmp_path: Path,
    monkeypatch,
) -> None:
    provider_path = tmp_path / "providers.jsonc"

    def provider_document(key: str) -> str:
        return json.dumps(
            {
                "providers": [
                    {
                        "id": "provider-a",
                        "type": "symmetric",
                        "baseURLs": {
                            "anthropic": "https://example.test/anthropic",
                        },
                        "keys": [{"id": "key-a", "key": key}],
                        "models": [
                            {
                                "id": "model-a",
                                "context": 4096,
                                "output": 1024,
                            }
                        ],
                    }
                ]
            }
        )

    original_key = "descriptor-selected-key"
    original_text = provider_document(original_key)
    provider_path.write_text(original_text, encoding="utf-8")
    provider_fd = os.open(provider_path, os.O_RDONLY)
    replacement = tmp_path / "replacement.jsonc"
    replacement.write_text(
        provider_document("replacement-key"),
        encoding="utf-8",
    )
    replacement.replace(provider_path)
    capture_path = tmp_path / "captured-key.txt"
    executable_directory = tmp_path / "bin"
    executable_directory.mkdir()
    fake_claude = executable_directory / "claude"
    fake_claude.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os\n"
        "open(os.environ['HOST_CAPTURE_PATH'], 'w').write("
        "os.environ['ANTHROPIC_AUTH_TOKEN'])\n"
        "print(json.dumps({'type': 'result', 'result': 'safe', "
        "'is_error': False, 'session_id': 'session-1'}))\n",
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)
    monkeypatch.setenv(
        "PATH",
        f"{executable_directory}{os.pathsep}{os.environ['PATH']}",
    )
    monkeypatch.setenv("HOST_CAPTURE_PATH", str(capture_path))
    try:
        runner = Runner._with_provider_snapshot(
            {
                "backend": "claude-code",
                "key_pool_config": str(provider_path),
                "keypool_state": str(tmp_path / "state.json"),
                "run_dir": str(tmp_path / "diagnostics"),
            },
            discover_config_files=False,
            provider_snapshot=_VerifiedProviderSnapshot(
                file_descriptor=provider_fd,
                sha256=hashlib.sha256(
                    original_text.encode("utf-8")
                ).hexdigest(),
            ),
        )

        result = runner.agent_with_retry_session_new("safe prompt", "run")
    finally:
        os.close(provider_fd)

    assert result.rc == 0
    assert capture_path.read_text(encoding="utf-8") == original_key


def test_held_provider_descriptor_can_be_loaded_concurrently_without_offset_race(
    tmp_path: Path,
) -> None:
    provider_path = tmp_path / "providers.jsonc"
    text = json.dumps(
        {
            "providers": [
                {
                    "id": "provider-a",
                    "type": "symmetric",
                    "baseURLs": {
                        "anthropic": "https://example.test/anthropic",
                    },
                    "keys": [{"id": "key-a", "key": "selected-key"}],
                    "models": [
                        {
                            "id": "model-a",
                            "context": 4096,
                            "output": 1024,
                        }
                    ],
                }
            ]
        }
    )
    provider_path.write_text(text, encoding="utf-8")
    descriptor = os.open(provider_path, os.O_RDONLY)
    expected_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            catalogs = list(
                executor.map(
                    lambda _: lpm_config.load(
                        provider_path,
                        file_descriptor=descriptor,
                        expected_sha256=expected_sha256,
                    ),
                    range(16),
                )
            )
    finally:
        os.close(descriptor)

    assert {
        catalog.providers[0].keys[0].key
        for catalog in catalogs
    } == {"selected-key"}


def test_held_provider_fallback_serializes_seek_read_and_restores_offset(
    tmp_path: Path,
    monkeypatch,
) -> None:
    provider_path = tmp_path / "providers.jsonc"
    text = json.dumps(
        {
            "providers": [
                {
                    "id": "provider-a",
                    "type": "symmetric",
                    "baseURLs": {
                        "anthropic": "https://example.test/anthropic",
                    },
                    "keys": [{"id": "key-a", "key": "selected-key"}],
                    "models": [
                        {
                            "id": "model-a",
                            "context": 4096,
                            "output": 1024,
                        }
                    ],
                }
            ]
        }
    )
    provider_path.write_text(text, encoding="utf-8")
    descriptor = os.open(provider_path, os.O_RDONLY)
    os.lseek(descriptor, 7, os.SEEK_SET)
    monkeypatch.setattr(lpm_config.os, "pread", None)
    expected_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            catalogs = list(
                executor.map(
                    lambda _: lpm_config.load(
                        provider_path,
                        file_descriptor=descriptor,
                        expected_sha256=expected_sha256,
                    ),
                    range(8),
                )
            )
        final_offset = os.lseek(descriptor, 0, os.SEEK_CUR)
    finally:
        os.close(descriptor)

    assert len(catalogs) == 8
    assert final_offset == 7


def test_platform_lock_helper_locks_byte_zero_and_restores_position() -> None:
    stream = io.StringIO("state")
    stream.seek(3)
    observed = []

    lpm_keypool._with_lock_at_start(
        stream,
        lambda: observed.append(stream.tell()),
    )

    assert observed == [0]
    assert stream.tell() == 3


def test_public_runner_constructor_has_no_provider_descriptor_primitives(
) -> None:
    parameters = inspect.signature(Runner.__init__).parameters

    assert "provider_config_fd" not in parameters
    assert "provider_config_sha256" not in parameters
