"""Landlock wrapper — optional sandboxing for the agent subprocess.

When ``LANDLOCK_CONFIG`` points at an existing config, the agent command is
prepended with the landlock runner; otherwise it runs as-is. Sourced by both
backends' ``invoke``.

**Platform note**: landlock is a Linux LSM (Linux Security Module). On non-Linux
platforms this is a silent no-op (the concept doesn't exist there, so we skip
rather than error). On Linux, the wrap fires only when ``LANDLOCK_CONFIG`` is
set and the runner exists.
"""

from __future__ import annotations

import os
import sys


def _supported() -> bool:
    # landlock is Linux-only; everywhere else, silently skip.
    return sys.platform.startswith("linux")


def wrap(cmd: list[str]) -> list[str]:
    if not _supported():
        return cmd
    cfg = os.environ.get("LANDLOCK_CONFIG", "")
    if cfg and os.path.isfile(cfg):
        runner = os.environ.get(
            "LANDLOCK_RUNNER", "utils/landlock-runner/landlock_runner.py"
        )
        return ["python3", runner, cfg, *cmd]
    return cmd

