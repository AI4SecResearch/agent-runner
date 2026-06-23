#!/bin/bash
# OpenCode backend for the agent-runner.
#
# Implements the 10-op backend interface consumed by runner.sh:
#   agent_backend_invoke / agent_backend_is_complete / agent_backend_result_ok / agent_backend_result_text /
#   agent_backend_session_id / agent_backend_perm_args / agent_backend_model_args / agent_backend_resume_args /
#   agent_backend_fork_args / agent_backend_auth_env_var
#
# This backend owns everything OpenCode-specific: the `opencode` binary, the
# `run` subcommand, the positional prompt, the --format json output, how the
# jsonl log is parsed, and the flag vocabulary (--model/-s/--fork). Generic
# orchestration in runner.sh never references `opencode` directly.
#
# Env vars (read here, not in generic code):
#   SANDBOX                  "1" → skip permission prompts (--dangerously-skip-permissions)
#   OPENCODE_MODEL           Primary model id, provider-prefixed (default: bailian/glm-5.2)
#   OPENCODE_DOWNGRADE_MODEL Downgrade-tier model id (default: bailian/glm-5.1)
#
# Auth: OpenCode reads the API key from the env var configured in
# ~/.config/opencode/opencode.json (default Z_AI_API_KEY). The key pool in
# runner.sh exports the current key to this agent's auth env var
# (agent_backend_auth_env_var) before each invocation.

# Defaults live in the backend so generic code stays agent-agnostic. Sourced via
# common.sh, so these are in scope wherever agent_with_retry is (incl. xargs
# children, which re-source common.sh).
OPENCODE_MODEL="${OPENCODE_MODEL:-bailian/glm-5.2}"
OPENCODE_DOWNGRADE_MODEL="${OPENCODE_DOWNGRADE_MODEL:-bailian/glm-5.1}"

# Run one agent step. Writes the json event stream to $prefix.jsonl, the stderr
# stream to $prefix.err, and prints the assistant text on stdout (concatenated
# across all `text` events in the turn).
# Usage: agent_backend_invoke <prompt> <prefix> [argv...]
# argv = composed flags (perm/model/resume/fork + caller pass-through).
agent_backend_invoke() {
    local prompt="$1" prefix="$2"
    shift 2
    _landlock_wrap opencode run "$prompt" --format json \
        "$@" 2>"$prefix.err" | tee "$prefix.jsonl" | \
        jq -r 'select(.type=="text") | .part.text'
}

# Has the agent emitted its terminal event? (watchdog early-exit)
# OpenCode emits step_finish on success or error on failure; both end the turn.
# Usage: agent_backend_is_complete <prefix>
agent_backend_is_complete() {
    local jsonl="${1}.jsonl"
    [ -f "$jsonl" ] && grep -qE '"type":"(step_finish|error)"' "$jsonl"
}

# Did the run succeed? Returns 0 if a step_finish is present and no error event.
# Note: opencode exits 1 on error, but agent_backend_invoke pipes through tee|jq,
# so the pipeline's exit status reflects jq, not opencode — must inspect JSONL.
# Usage: agent_backend_result_ok <prefix>
agent_backend_result_ok() {
    local jsonl="${1}.jsonl"
    [ -s "$jsonl" ] || return 1
    jq -se 'any(.[]; .type == "step_finish") and all(.[]; .type != "error")' \
        "$jsonl" >/dev/null 2>&1
}

# The error payload (JSON) fed to the provider layer (providers.classify).
# Surface the upstream code from responseBody and the HTTP statusCode as
# structured fields; the provider module picks/maps them.
# Usage: agent_backend_result_text <prefix>
agent_backend_result_text() {
    local jsonl="${1}.jsonl"
    [ -f "$jsonl" ] || return 0
    jq -c 'select(.type == "error") | {
        message: .error.data.message,
        code: (try (.error.data.responseBody | fromjson | .error.code) catch null),
        status: .error.data.statusCode
    }' "$jsonl" 2>/dev/null | head -1
}

# The session id the agent recorded (empty = none / unsupported).
# OpenCode uses sessionID (camelCase) at top level; emitted on every event.
# Usage: agent_backend_session_id <prefix>
agent_backend_session_id() {
    local jsonl="${1}.jsonl"
    [ -f "$jsonl" ] || return 0
    jq -r 'select(.sessionID != null) | .sessionID' "$jsonl" 2>/dev/null | head -1
}

# Permission flag fragment, chosen from $SANDBOX. In non-sandbox mode, emit
# nothing — OpenCode's permission model is configured in opencode.json, not via
# CLI flag, so we defer to the user's config.
# Usage: agent_backend_perm_args
agent_backend_perm_args() {
    [ "${SANDBOX:-}" = "1" ] && echo "--dangerously-skip-permissions"
}

# Model flag fragment. <tier> is a logical token from the retry plan, or a
# provider-prefixed model id (e.g., bailian/glm-5.2).
# Usage: agent_backend_model_args <primary|downgrade|<model_id>>
agent_backend_model_args() {
    case "$1" in
        primary)   echo "--model ${OPENCODE_MODEL}" ;;
        downgrade) echo "--model ${OPENCODE_DOWNGRADE_MODEL}" ;;
        *)         echo "--model $1" ;;
    esac
}

# Resume flag fragment (empty output ⇒ backend opts out of resume ⇒ degrade).
# OpenCode uses -s <sessionID> (long form: --session).
# Usage: agent_backend_resume_args <session_id>
agent_backend_resume_args() {
    [ -n "$1" ] && echo "-s $1"
}

# Fork flag fragment (start a divergent session from an existing one).
# OpenCode's --fork requires --continue or --session; the caller composes them.
# Usage: agent_backend_fork_args
agent_backend_fork_args() {
    echo "--fork"
}

# The env var this agent reads for its API key. OpenCode reads whatever its
# opencode.json is configured with (default Z_AI_API_KEY); override via
# OPENCODE_AUTH_ENV_VAR if your config differs.
# Usage: agent_backend_auth_env_var
agent_backend_auth_env_var() {
    echo "${OPENCODE_AUTH_ENV_VAR:-Z_AI_API_KEY}"
}
