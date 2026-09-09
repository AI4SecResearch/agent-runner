"""Exercise the real watchdog with a deterministic process/clock, not a CLI."""
from types import SimpleNamespace
from unittest.mock import Mock
import subprocess

import pytest

from agent_runner import engine


@pytest.fixture
def watchdog(monkeypatch):
    clock = SimpleNamespace(now=0.0)

    class Process:
        returncode = None

        def __init__(self, finishes_at, exit_code=0):
            self.finishes_at = finishes_at
            self.exit_code = exit_code
            self.waits = []

        def poll(self):
            if clock.now >= self.finishes_at:
                self.returncode = self.exit_code
            return self.returncode

        def wait(self, timeout):
            self.waits.append(timeout)
            if self.finishes_at > clock.now + timeout:
                clock.now += timeout
                raise subprocess.TimeoutExpired('fake-agent', timeout)
            clock.now = self.finishes_at
            return self.poll()

    def sleep(seconds):
        clock.now += seconds

    monkeypatch.setattr(engine, 'time', SimpleNamespace(time=lambda: clock.now, sleep=sleep))
    reader = Mock()
    monkeypatch.setattr(engine.threading, 'Thread', Mock(return_value=reader))
    monkeypatch.setattr(engine.os.path, 'getsize', lambda path: 0)
    backend = SimpleNamespace(stream=Mock(), is_complete=Mock(return_value=False))
    runner = object.__new__(engine.Runner)
    runner._config = {'run_dir': '.', 'stall_timeout': 300, 'total_timeout': 1200}
    runner._get_backend = lambda: backend

    def run(process, **kwargs):
        runner._agent_once = lambda *args, **kwargs: process
        return runner._agent_once_with_watchdog('prompt', 'run', [], **kwargs)

    return SimpleNamespace(clock=clock, Process=Process, runner=runner, run=run,
                           backend=backend, reader=reader)


@pytest.mark.parametrize('finishes_at', [0.05, 9.95, 10.05])
@pytest.mark.parametrize('exit_code', [0, 1])
def test_process_exit_wakes_watchdog_without_polling_delay(watchdog, finishes_at, exit_code):
    process = watchdog.Process(finishes_at, exit_code)
    rc, outcome = watchdog.run(process)

    assert rc == exit_code
    assert outcome is engine.RunOutcome.SUCCEEDED  # result_ok owns success classification
    assert watchdog.clock.now - finishes_at <= 0.1
    assert process.waits
    assert all(timeout == engine._WATCHDOG_POLL_SECONDS for timeout in process.waits)
    assert watchdog.backend.is_complete.call_count <= 2
    watchdog.reader.join.assert_called_once_with()


@pytest.mark.parametrize('boundary,expected', [
    ('total_timeout', engine.RunOutcome.ATTEMPT_TIMEOUT),
    ('stall_timeout', engine.RunOutcome.STALL_TIMEOUT),
    ('cancel', engine.RunOutcome.CANCELED),
])
def test_wait_timeout_returns_to_existing_watchdog_boundaries(watchdog, boundary, expected):
    process = watchdog.Process(9999)
    if boundary != 'cancel':
        watchdog.runner._config[boundary] = 5

    class Cancellation:
        @property
        def is_cancellation_requested(self):
            return boundary == 'cancel' and watchdog.clock.now >= 5

    def reap(proc):
        proc.returncode = -15

    watchdog.runner._kill_process_group = Mock(side_effect=reap)
    watchdog.reader.is_alive.return_value = False
    _, outcome = watchdog.run(process, cancellation=Cancellation())

    assert outcome is expected
    watchdog.runner._kill_process_group.assert_called_once_with(process)
    watchdog.reader.join.assert_called_once_with(timeout=5)


def test_terminal_log_still_exits_before_waiting(watchdog):
    process = watchdog.Process(9999)
    watchdog.backend.is_complete.return_value = True

    assert watchdog.run(process) == (0, engine.RunOutcome.SUCCEEDED)
    assert process.waits == []
    assert watchdog.clock.now == 0
    watchdog.reader.join.assert_called_once_with()
