#!/usr/bin/env bash
# agent-runner.sh — bash CLI front for the Python process-mode entry.
#
# This is the "high-reliability agent process" interface for bash consumers:
# the caller treats agent-runner as a single reliable agent invocation that
# internally handles watchdog timeouts, reactive retry with model downgrade,
# key-pool rotation, and session reuse. Exit codes mirror the Python API:
#   0 = success, 1 = all retries failed, 2 = quota exhausted (no key pool).
#
# User-facing CLI:
#   agent-runner.sh <entry> [--tier primary|downgrade] <prompt> <log_name> [session_id] [-- <passthrough>]
# where <entry> is one of: new | resume | fork | agent_with_retry
#   (full names also accepted: agent_with_retry_session_new, etc.)
#
# This wrapper owns CLI parsing of the leading [ARGUMENTS] (--tier): it extracts
# --tier and reorders into the internal python form
#   <entry> <model_tier> <prompt> <log_name> [session_id] [-- <passthrough>]
# so python's __main__ receives clean positionals (model_tier as argv[1]) and
# does no flag parsing. '--' and everything after is forwarded verbatim as
# passthrough to the agent.
#
# The Python package may live next to this script (sys.path is wired so the
# in-tree copy is found) or be pip-installed.
_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Prefer the in-tree package: prepend _HERE so `agent_runner/` next to this
# script is importable without installation.
export PYTHONPATH="${_HERE}${PYTHONPATH:+:${PYTHONPATH}}"

[ $# -ge 1 ] || { echo "agent-runner: needs <entry> (new|resume|fork|agent_with_retry) ..." >&2; exit 2; }
entry="$1"; shift

# Digest the leading [ARGUMENTS] (currently only --tier). Stop at the first
# non-flag token — that's the start of the positionals (<prompt>). '--' and the
# passthrough that follows are left for python, verbatim.
tier="primary"
while [ $# -gt 0 ]; do
    case "$1" in
        --tier)   tier="${2:-primary}"; shift 2 ;;
        --tier=*) tier="${1#--tier=}"; shift ;;
        *) break ;;
    esac
done

exec python3 -m agent_runner "$entry" "$tier" "$@"
