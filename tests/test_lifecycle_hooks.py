from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from agent_runner import (
    LifecycleEvent,
    LifecycleEventType,
    Result,
    Runner,
)


def test_lifecycle_hook_contract_is_runner_owned() -> None:
    event = LifecycleEvent(type=LifecycleEventType.BACKEND_STARTED)

    assert event.type is LifecycleEventType.BACKEND_STARTED
    assert event.retry_index is None


class _Sink:
    def __init__(self) -> None:
        self.events: list[LifecycleEvent] = []

    def on_lifecycle_event(self, event: LifecycleEvent) -> None:
        self.events.append(event)


class _RetryingKeyPool:
    def init(self) -> None:
        return None

    def on_success(self) -> None:
        return None

    def available_size(self) -> int:
        return 1

    def react(self, text: str):
        del text
        return SimpleNamespace(action="rotate", stop_reason=None)

    def rotate(self) -> None:
        return None


class _Backend:
    agent_id = "lifecycle-test"

    def result_text(self, prefix: str) -> str:
        del prefix
        return "failed"

    def session_id(self, prefix: str) -> str:
        del prefix
        return ""

    def resume_args(self, session_id: str) -> list[str]:
        del session_id
        return []

    def model_args(
        self,
        model: str,
        *,
        resolved_model: str = "",
    ) -> list[str]:
        del model, resolved_model
        return []


def test_lifecycle_order_and_retry_index(
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
    key_pool = _RetryingKeyPool()
    backend = _Backend()
    monkeypatch.setattr(runner, "_ensure_keypool", lambda: key_pool)
    monkeypatch.setattr(runner, "_get_backend", lambda: backend)
    results = iter((Result(1), Result(0, "session", "done")))
    monkeypatch.setattr(
        runner,
        "_agent_once_with_check",
        lambda *args, **kwargs: next(results),
    )
    sink = _Sink()

    result = runner.agent_with_retry_session_new(
        "prompt",
        "run",
        lifecycle_sink=sink,
    )

    assert result.rc == 0
    assert sink.events == [
        LifecycleEvent(type=LifecycleEventType.BACKEND_STARTED),
        LifecycleEvent(
            type=LifecycleEventType.INTERNAL_RETRY_STARTED,
            retry_index=1,
        ),
        LifecycleEvent(type=LifecycleEventType.SUCCEEDED),
    ]


def test_lifecycle_sink_failure_does_not_change_result(
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
    monkeypatch.setattr(runner, "_ensure_keypool", lambda: _RetryingKeyPool())
    monkeypatch.setattr(
        runner,
        "_agent_once_with_check",
        lambda *args, **kwargs: Result(0, "session", "done"),
    )

    class BrokenSink:
        def on_lifecycle_event(self, event: LifecycleEvent) -> None:
            del event
            raise RuntimeError("observer secret")

    result = runner.agent_with_retry_session_new(
        "prompt",
        "run",
        lifecycle_sink=BrokenSink(),
    )

    assert result.rc == 0
