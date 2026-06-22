#!/bin/bash
# Claude Code backend for the agent-runner.
#
# Implements the 9-op backend interface consumed by runner.sh:
#   agent_backend_invoke / agent_backend_is_complete / agent_backend_result_ok / agent_backend_result_text /
#   agent_backend_session_id / agent_backend_perm_args / agent_backend_model_args / agent_backend_resume_args /
#   agent_backend_fork_args
#
# This backend owns everything Claude-Code-specific: the `claude` binary, the
# prompt flag, the stream-json output format, how the jsonl log is parsed, and the
# flag vocabulary (--model/--resume/--fork-session/--permission-mode). Generic
# orchestration in runner.sh never references `claude` directly.
#
# Env vars (read here, not in generic code):
#   SANDBOX           "1" → skip permission prompts (--dangerously-skip-permissions)
#   CLAUDE_MODEL      Primary model id (default: glm-5-turbo)
#   DOWNGRADE_MODEL   Downgrade-tier model id (default: glm-4.7)

# Defaults live in the backend so generic code stays agent-agnostic. Sourced via
# common.sh, so these are in scope wherever agent_with_retry is (incl. xargs
# children, which re-source common.sh).
CLAUDE_MODEL="${CLAUDE_MODEL:-glm-5-turbo}"
DOWNGRADE_MODEL="${DOWNGRADE_MODEL:-glm-4.7}"

# Run one agent step. Writes the stream-json log to $prefix.jsonl, the stderr
# stream to $prefix.err, and prints the result text on stdout.
# Usage: agent_backend_invoke <prompt> <prefix> [argv...]
# argv = composed flags (perm/model/resume/fork + caller pass-through).
agent_backend_invoke() {
    local prompt="$1" prefix="$2"
    shift 2
    _landlock_wrap claude -p "$prompt" \
        --output-format stream-json --verbose \
        "$@" 2>"$prefix.err" | tee "$prefix.jsonl" | \
        jq -r 'select(.type=="result") | .result'
}

# Has the agent emitted its final result line? (watchdog early-exit)
# Usage: agent_backend_is_complete <log_name>
agent_backend_is_complete() {
    local jsonl="$OUTPUT_DIR/${1}.jsonl"
    [ -f "$jsonl" ] && grep -q '"type":"result"' "$jsonl"
}

# Did the run succeed? Returns 0 if a non-error result line is present.
# Usage: agent_backend_result_ok <log_name>
agent_backend_result_ok() {
    local jsonl="$OUTPUT_DIR/${1}.jsonl"
    [ -s "$jsonl" ] || return 1
    jq -se 'any(.[]; .type == "result" and ((.is_error // false) | not))' \
        "$jsonl" >/dev/null 2>&1
}

# The result text (used by error classification).
# Usage: agent_backend_result_text <log_name>
agent_backend_result_text() {
    local jsonl="$OUTPUT_DIR/${1}.jsonl"
    [ -f "$jsonl" ] || return 0
    jq -r 'select(.type=="result") | .result' "$jsonl" 2>/dev/null | head -1
}

# The session id the agent recorded (empty = none / unsupported).
# Usage: agent_backend_session_id <log_name>
agent_backend_session_id() {
    local jsonl="$OUTPUT_DIR/${1}.jsonl"
    [ -f "$jsonl" ] || return 0
    jq -r 'select(.session_id != null) | .session_id' "$jsonl" 2>/dev/null | head -1
}

# Permission flag fragment, chosen from $SANDBOX.
# Usage: agent_backend_perm_args
agent_backend_perm_args() {
    if [ "${SANDBOX:-}" = "1" ]; then
        echo "--dangerously-skip-permissions"
    else
        echo "--permission-mode acceptEdits"
    fi
}

# Model flag fragment. <tier> is a logical token from the retry plan or "primary".
# Usage: agent_backend_model_args <primary|downgrade|explicit:<id>>
agent_backend_model_args() {
    case "$1" in
        downgrade)  echo "--model ${DOWNGRADE_MODEL}" ;;
        explicit:*) echo "--model ${1#explicit:}" ;;
        *)          echo "--model ${CLAUDE_MODEL}" ;;
    esac
}

# Resume flag fragment (empty output ⇒ backend opts out of resume ⇒ degrade).
# Usage: agent_backend_resume_args <session_id>
agent_backend_resume_args() {
    [ -n "$1" ] && echo "--resume $1"
}

# Fork flag fragment (start a divergent session from an existing one).
# Usage: agent_backend_fork_args
agent_backend_fork_args() {
    echo "--fork-session"
}
