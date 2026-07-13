#!/bin/bash
# Landlock wrapper — sourced by agent backends.
#
# Each backend's agent_backend_invoke wraps the agent command with this so
# that, when LANDLOCK_CONFIG is set, the agent runs sandboxed.
#
# Environment variables:
#   LANDLOCK_CONFIG  Optional landlock config; wraps each agent command
#   LANDLOCK_RUNNER  landlock_runner.py path (default: utils/landlock-runner/landlock_runner.py)

_landlock_wrap() {
    if [ -n "$LANDLOCK_CONFIG" ] && [ -f "$LANDLOCK_CONFIG" ]; then
        python3 "${LANDLOCK_RUNNER:-utils/landlock-runner/landlock_runner.py}" "$LANDLOCK_CONFIG" "$@"
    else
        "$@"
    fi
}
