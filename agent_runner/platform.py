"""Platform abstraction for watchdog process management.

All Unix-specific process-tree mechanics are funneled through this module so
the rest of the codebase (backends' ``invoke``, the engine's watchdog) calls
abstract APIs — ``new_session_kwargs()`` and ``kill_tree(proc)`` — and never
touches ``os.killpg``/``start_new_session`` directly.

The POSIX implementation uses a new session and ``killpg``. Windows uses a
new process group plus ``taskkill /T /F`` so watchdog timeout and cancellation
also tear down descendants instead of only the wrapper process.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys


_CREATE_NEW_PROCESS_GROUP = getattr(
    subprocess,
    "CREATE_NEW_PROCESS_GROUP",
    0x00000200,
)


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
    @property
    def supports_process_tree_kill(self) -> bool:
        return True

    def new_session_kwargs(self) -> dict:
        return {"creationflags": _CREATE_NEW_PROCESS_GROUP}

    def kill_tree(self, proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )
            except (OSError, subprocess.SubprocessError):
                pass
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass


def get_platform() -> Platform:
    return _WindowsPlatform() if sys.platform == "win32" else _PosixPlatform()


# module singleton — the rest of the codebase imports this.
PLATFORM: Platform = get_platform()
