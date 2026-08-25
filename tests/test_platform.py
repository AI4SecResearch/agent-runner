"""Platform abstraction tests — verify the POSIX path is selected on Linux/macOS
and that the abstraction API behaves (process group spawn + tree kill).

These exercise both the POSIX implementation and the Windows process-group
contract without requiring the host running the tests to be Windows.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from agent_runner import platform  # noqa: E402


def test_posix_selected_on_unix():
    if sys.platform == "win32":
        pytest.skip("POSIX-only check")
    assert isinstance(platform.PLATFORM, platform._PosixPlatform)
    assert platform.PLATFORM.supports_process_tree_kill is True


def test_windows_reports_process_tree_kill_is_supported():
    windows = platform._WindowsPlatform()

    assert windows.supports_process_tree_kill is True
    assert windows.new_session_kwargs() == {
        "creationflags": platform._CREATE_NEW_PROCESS_GROUP,
    }


def test_windows_kill_tree_uses_taskkill_and_reaps(monkeypatch):
    class FakeProcess:
        pid = 1234

        def __init__(self):
            self.alive = True
            self.wait_timeouts = []

        def poll(self):
            return None if self.alive else 0

        def wait(self, *, timeout):
            self.wait_timeouts.append(timeout)
            return 0

        def kill(self):
            raise AssertionError("taskkill success must not use leader-only kill")

    proc = FakeProcess()
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        proc.alive = False

    monkeypatch.setattr(platform.subprocess, "run", fake_run)

    platform._WindowsPlatform().kill_tree(proc)

    assert calls == [
        (
            ["taskkill", "/PID", "1234", "/T", "/F"],
            {
                "check": False,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "timeout": 10,
            },
        )
    ]
    assert proc.wait_timeouts == [5]


def test_new_session_kwargs_is_a_separate_group():
    if sys.platform == "win32":
        pytest.skip("POSIX-only behavior")
    kwargs = platform.PLATFORM.new_session_kwargs()
    assert kwargs == {"start_new_session": True}


def test_kill_tree_terminates_a_separate_group():
    """A child started with new_session_kwargs lands in its own process group;
    kill_tree must reap it and any descendants."""
    if sys.platform == "win32":
        pytest.skip("POSIX-only behavior")
    # Spawn a long-sleeping child that itself spawns a grandchild, so the tree
    # has >1 member.
    code = (
        "import os, time, subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "time.sleep(30)\n"
    )
    kwargs = platform.PLATFORM.new_session_kwargs()
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        **kwargs,
    )
    # let the grandchild spawn
    time.sleep(0.5)
    child_pid = proc.pid
    child_pgid = os.getpgid(child_pid)
    assert child_pgid == child_pid, "child should be its own group leader"

    platform.PLATFORM.kill_tree(proc)

    # proc reaped
    assert proc.poll() is not None
    # the whole process group is gone (killpg hit it). Probe by sending 0.
    with pytest.raises((ProcessLookupError, OSError)):
        os.killpg(child_pgid, 0)


def test_kill_tree_idempotent_on_dead_proc():
    if sys.platform == "win32":
        pytest.skip("POSIX-only behavior")
    kwargs = platform.PLATFORM.new_session_kwargs()
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.exit(0)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs,
    )
    proc.wait()
    # already dead — kill_tree must not raise
    platform.PLATFORM.kill_tree(proc)
