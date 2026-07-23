"""Platform abstraction for watchdog process management.

All Unix-specific process-tree mechanics are funneled through this module so
the rest of the codebase (backends' ``invoke``, the engine's watchdog) calls
abstract APIs — ``new_session_kwargs()`` and ``kill_tree(proc)`` — and never
touches ``os.killpg``/``start_new_session`` directly.

The POSIX implementation is the supported path (Linux/macOS/BSD). A Windows
implementation is stubbed as an extension point: the project's keypool and
path handling are cross-platform (see the vendored lpm copy's flock helpers),
but watchdog *process-tree kill* is genuinely Unix-shaped semantics and a
correct Windows port (``taskkill /T /F`` + job objects) is left for later.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys


class Platform:
    """Abstract platform API for process-tree lifecycle management."""

    @property
    def supports_process_tree_kill(self) -> bool:
        raise NotImplementedError

    def new_session_kwargs(self) -> dict:
        """``subprocess.Popen`` kwargs that put the child in an independently-
        killable group (so the watchdog can tear down the whole tree on
        timeout, mirroring bash's ``pkill -P <pid>`` + ``kill <pid>``)."""
        raise NotImplementedError

    def kill_tree(self, proc: subprocess.Popen) -> None:
        """Kill the agent's whole process tree (agent + any wrappers), then
        reap the leader. Idempotent — safe to call on an already-exited proc."""
        raise NotImplementedError


class _PosixPlatform(Platform):
    @property
    def supports_process_tree_kill(self) -> bool:
        return True

    def new_session_kwargs(self) -> dict:
        # start_new_session=True → setsid() in the child, making it a new
        # session/group leader; killpg on its pid hits the whole tree.
        return {"start_new_session": True}

    def kill_tree(self, proc: subprocess.Popen) -> None:
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            # already gone, or not a leader — nothing to kill
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass


class _WindowsPlatform(Platform):
    # Extension point — not implemented in this revision. The correct
    # approach is CREATE_NEW_PROCESS_GROUP on spawn + taskkill /T /F /PID
    # (or a Win32 Job Object) for tree kill. Surfacing a clear error here
    # is better than silent Unix semantics on Windows.
    @property
    def supports_process_tree_kill(self) -> bool:
        return False

    def new_session_kwargs(self) -> dict:
        raise NotImplementedError(
            "agent-runner watchdog process-group kill is not yet "
            "implemented on Windows. See agent_runner/platform.py. "
            "Use a POSIX system, or contribute a Windows implementation."
        )

    def kill_tree(self, proc: subprocess.Popen) -> None:
        raise NotImplementedError(
            "agent-runner watchdog process-tree kill is not yet "
            "implemented on Windows."
        )


def get_platform() -> Platform:
    return _WindowsPlatform() if sys.platform == "win32" else _PosixPlatform()


# module singleton — the rest of the codebase imports this.
PLATFORM: Platform = get_platform()
