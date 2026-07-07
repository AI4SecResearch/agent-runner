#!/bin/bash
# Claude Code backend for the agent-runner.
#
# Implements the 11-op backend interface consumed by runner.sh:
#   agent_backend_invoke / agent_backend_is_complete / agent_backend_result_ok / agent_backend_result_text /
#   agent_backend_session_id / agent_backend_perm_args / agent_backend_model_args / agent_backend_resume_args /
#   agent_backend_fork_args / agent_backend_api_key_env_var / agent_backend_base_url_env_var
#
# This backend owns everything Claude-Code-specific: the `claude` binary, the
# prompt flag, the stream-json output format, how the jsonl log is parsed, and the
# flag vocabulary (--model/--resume/--fork-session/--permission-mode). Generic
# orchestration in runner.sh never references `claude` directly.
#
# Env vars (agent-agnostic; the key pool may override per provider):
#   SANDBOX             "1" → skip permission prompts (--dangerously-skip-permissions)
#   PRIMARY_MODEL       Primary model id (default: glm-5-turbo). The key pool exports the
#                       active provider's model when providers.jsonc declares one.
#   DOWNGRADE_MODEL     Downgrade-tier model id (default: glm-4.7)
#   ANTHROPIC_BASE_URL  base_url (read by the `claude` binary). The key pool exports
#                       the active provider's base_url; claude-code speaks anthropic only.

source "${BASH_SOURCE[0]%/*}/../landlock.sh"

# Defaults live in the backend so generic code stays agent-agnostic. Sourced via
# common.sh, so these are in scope wherever agent_with_retry is (incl. xargs
# children, which re-source common.sh).
PRIMARY_MODEL="${PRIMARY_MODEL:-glm-5-turbo}"
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
# Usage: agent_backend_is_complete <prefix>
agent_backend_is_complete() {
    local jsonl="${1}.jsonl"
    [ -f "$jsonl" ] && grep -q '"type":"result"' "$jsonl"
}

# Did the run succeed? Returns 0 if a non-error result line is present.
# Usage: agent_backend_result_ok <prefix>
agent_backend_result_ok() {
    local jsonl="${1}.jsonl"
    [ -s "$jsonl" ] || return 1
    jq -se 'any(.[]; .type == "result" and ((.is_error // false) | not))' \
        "$jsonl" >/dev/null 2>&1
}

# The error payload (JSON) fed to the provider layer. claude-code renders the
# provider error inline in its result text, so surface it as {message}.
# Usage: agent_backend_result_text <prefix>
agent_backend_result_text() {
    local jsonl="${1}.jsonl"
    [ -f "$jsonl" ] || return 0
    jq -c 'select(.type=="result") | {message: .result}' "$jsonl" 2>/dev/null | head -1
}

# The session id the agent recorded (empty = none / unsupported).
# Usage: agent_backend_session_id <prefix>
agent_backend_session_id() {
    local jsonl="${1}.jsonl"
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

# Model flag fragment. <tier> is a logical token from the retry plan, or a model id.
# Usage: agent_backend_model_args <primary|downgrade|<model_id>>
agent_backend_model_args() {
    case "$1" in
        primary)    echo "--model ${PRIMARY_MODEL}" ;;
        downgrade)  echo "--model ${DOWNGRADE_MODEL}" ;;
        *)          echo "--model $1" ;;
    esac
}

# Resume flag fragment (empty output ⇒ backend opts out of resume ⇒ degrade).
# Usage: agent_backend_resume_args <session_id>
agent_backend_resume_args() {
    [ -n "$1" ] && echo "--resume $1"
}

# All flags the agent needs to FORK from an existing session (resume + fork).
# Composes the resume fragment with the fork flag, so the caller passes a single
# source session_id and gets the complete arg set. Empty output when $1 is empty
# (= no source / unsupported) → caller degrades, same convention as resume_args.
# Usage: agent_backend_fork_args <session_id>
agent_backend_fork_args() {
    local r; r=$(agent_backend_resume_args "$1")
    [ -n "$r" ] && echo "$r --fork-session"
}

# The env var this agent reads for its API key (the key pool exports the current
# key here). claude-code always reads ANTHROPIC_AUTH_TOKEN.
# Usage: agent_backend_api_key_env_var
agent_backend_api_key_env_var() {
    echo "ANTHROPIC_AUTH_TOKEN"
}

# The env var this agent reads for its base_url (the key pool exports the
# active provider's base_url here). claude-code reads ANTHROPIC_BASE_URL and
# speaks the anthropic protocol only.
# Usage: agent_backend_base_url_env_var
agent_backend_base_url_env_var() {
    echo "ANTHROPIC_BASE_URL"
}
