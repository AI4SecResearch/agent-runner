from __future__ import annotations

import io
import json
from pathlib import Path

from agent_runner.backends.claude_code import ClaudeCodeBackend
from agent_runner.config import Config
from agent_runner.keypool import KeyContext, KeyPool as RunnerKeyPool


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
