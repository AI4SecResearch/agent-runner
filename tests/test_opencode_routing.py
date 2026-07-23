from __future__ import annotations

import json
from pathlib import Path

from agent_runner.backends.opencode import OpencodeBackend
from agent_runner.engine import Runner
from agent_runner.keypool import KeyContext, KeyPool
from agent_runner.config import Config
from llm_provider_manager.schema import Key, Model, Provider


class _Config:
    def __init__(self, provider: Provider) -> None:
        self._provider = provider

    def provider_by_id(self, provider_id: str) -> Provider | None:
        return self._provider if provider_id == self._provider.id else None


class _Agent:
    id = "opencode"

    @staticmethod
    def base_url_for(provider: Provider) -> str:
        return provider.base_urls["openai"]


class _LpmKeyPool:
    agent_id = "opencode"

    def __init__(self, provider: Provider) -> None:
        self.config = _Config(provider)
        self._agent = _Agent()


class _RunnerConfig:
    @staticmethod
    def get(key: str, default: str = "") -> str:
        del key
        return default


def _model(model_id: str) -> Model:
    return Model(
        id=model_id,
        display_name=model_id,
        context=4096,
        output=1024,
    )


def _key_pool(provider: Provider) -> KeyPool:
    key_pool = KeyPool.__new__(KeyPool)
    key_pool._kp = _LpmKeyPool(provider)
    key_pool._config = _RunnerConfig()
    key_pool._current_key_ctx = None
    key_pool._has_config = True
    return key_pool


def test_symmetric_opencode_key_context_uses_provider_model_reference() -> None:
    provider = Provider(
        id="zhipu",
        type="symmetric",
        display_name="Zhipu",
        base_urls={"openai": "https://example.test/v1"},
        keys=[Key(id="main", key="fixture-key")],
        models=[_model("glm-primary"), _model("glm-downgrade")],
    )

    context = _key_pool(provider)._resolve_entry(
        ("fixture-key", "zhipu", "main")
    )

    assert context.primary_model == "zhipu/glm-primary"
    assert context.downgrade_model == "zhipu/glm-downgrade"


def test_asymmetric_opencode_key_context_uses_provider_key_model_reference() -> None:
    provider = Provider(
        id="gateway",
        type="asymmetric",
        display_name="Gateway",
        base_urls={"openai": "https://example.test/v1"},
        keys=[
            Key(
                id="tenant-a",
                key="fixture-key",
                models=[
                    _model("vendor/model-primary"),
                    _model("vendor/model-downgrade"),
                ],
            )
        ],
    )

    context = _key_pool(provider)._resolve_entry(
        ("fixture-key", "gateway", "tenant-a")
    )

    assert context.primary_model == (
        "gateway-tenant-a/vendor/model-primary"
    )
    assert context.downgrade_model == (
        "gateway-tenant-a/vendor/model-downgrade"
    )


class _ExitedProcess:
    returncode = 0

    @staticmethod
    def poll() -> int:
        return 0


class _RetryBackend(OpencodeBackend):
    def __init__(self, config: Config) -> None:
        super().__init__(config=config)
        self.calls: list[tuple[list[str], Path | None]] = []

    def invoke(
        self,
        prompt,
        prefix,
        argv,
        key_ctx=None,
        *,
        working_directory=None,
    ):
        del prompt, key_ctx
        self.calls.append((list(argv), working_directory))
        event = (
            {
                "type": "error",
                "error": {
                    "data": {
                        "message": "rotate",
                        "statusCode": 429,
                    }
                },
            }
            if len(self.calls) == 1
            else {
                "type": "step_finish",
                "sessionID": "session-2",
                "part": {"reason": "stop"},
            }
        )
        Path(f"{prefix}.jsonl").write_text(
            json.dumps(event) + "\n",
            encoding="utf-8",
        )
        return _ExitedProcess()

    @staticmethod
    def stream(proc, prefix) -> None:
        del proc, prefix


class _RotatingKeyPool:
    @staticmethod
    def init() -> KeyContext:
        return KeyContext(
            key="key-a",
            primary_model="provider-a/model-a",
        )

    @staticmethod
    def rotate() -> KeyContext:
        return KeyContext(
            key="key-b",
            primary_model="provider-b/model-b",
        )

    @staticmethod
    def available_size() -> int:
        return 1

    @staticmethod
    def react(text: str) -> str:
        del text
        return "rotate"

    @staticmethod
    def on_success() -> None:
        return None

    @staticmethod
    def disable() -> None:
        return None


def test_internal_retry_routes_model_to_rotated_opencode_provider(
    tmp_path,
    monkeypatch,
) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    working_directory = tmp_path / "working"
    working_directory.mkdir()
    runner = Runner(
        config_overrides={
            "backend": "opencode",
            "run_dir": str(run_directory),
            "stall_timeout": 5,
            "total_timeout": 0,
        },
        discover_config_files=False,
    )
    backend = _RetryBackend(runner._config)
    key_pool = _RotatingKeyPool()
    monkeypatch.setattr(runner, "_get_backend", lambda: backend)
    monkeypatch.setattr(runner, "_ensure_keypool", lambda: key_pool)

    result = runner.agent_with_retry_session_new(
        "prompt",
        "run",
        "--agent",
        "agent-runtime",
        "--file",
        "/scope/input.txt",
        "--dir",
        str(working_directory),
        working_directory=working_directory,
    )

    assert result.rc == 0
    assert backend.calls == [
        (
            [
                "--model",
                "provider-a/model-a",
                "--agent",
                "agent-runtime",
                "--file",
                "/scope/input.txt",
                "--dir",
                str(working_directory),
            ],
            working_directory,
        ),
        (
            [
                "--model",
                "provider-b/model-b",
                "--agent",
                "agent-runtime",
                "--file",
                "/scope/input.txt",
                "--dir",
                str(working_directory),
            ],
            working_directory,
        ),
    ]
