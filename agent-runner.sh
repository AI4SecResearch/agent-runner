#!/usr/bin/env bash
# agent-runner.sh — thin bash wrapper around the Python process-mode entry.
#
# This is the "high-reliability agent process" interface for bash consumers:
# the caller treats agent-runner as a single reliable agent invocation that
# internally handles watchdog timeouts, reactive retry with model downgrade,
# key-pool rotation, and session reuse. Exit codes mirror the Python API:
#   0 = success, 1 = all retries failed, 2 = quota exhausted (no key pool).
#
# Usage:
#   agent-runner.sh <entry> <prompt> <log_name> [session_id] [-- extra...]
# where <entry> is one of: new | resume | fork | once | agent_with_retry
#   (full names also accepted: agent_with_retry_session_new, etc.)
#
# The Python package may live next to this script (sys.path is wired so the
# in-tree copy is found) or be pip-installed. Either way this wrapper just
# delegates to `python3 -m agent_runner`.
_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Prefer the in-tree package: prepend _HERE so `agent_runner/` next to this
# script is importable without installation.
export PYTHONPATH="${_HERE}${PYTHONPATH:+:${PYTHONPATH}}"

exec python3 -m agent_runner "$@"
