#!/bin/bash
# Codex SDK backend for agent-runner.

PRIMARY_MODEL="${PRIMARY_MODEL:-${CODEX_MODEL:-}}"
DOWNGRADE_MODEL="${DOWNGRADE_MODEL:-${CODEX_MODEL:-}}"

_codex_runner="${BASH_SOURCE[0]%/*}/../codex_runner.py"

agent_backend_invoke() {
    local prompt="$1" prefix="$2"
    shift 2
    local output_dir log_name
    output_dir=$(dirname "$prefix")
    log_name=$(basename "$prefix")
    python3 "$_codex_runner" run \
        --prompt "$prompt" \
        --output-dir "$output_dir" \
        --log-name "$log_name" \
        --project-root "${PROJECT_ROOT:-$PWD}" \
        "$@"
}

agent_backend_is_complete() {
    local jsonl="${1}.jsonl"
    [ -f "$jsonl" ] && jq -e \
        'select(.type == "turn.completed" or .type == "turn/completed")' \
        "$jsonl" >/dev/null 2>&1
}

agent_backend_result_ok() {
    local jsonl="${1}.jsonl"
    [ -s "$jsonl" ] || return 1
    jq -se 'any(.[]; (.type == "turn.completed" or .type == "turn/completed") and ((.status // .turn.status // "completed") == "completed"))' \
        "$jsonl" >/dev/null 2>&1
}

agent_backend_result_text() {
    local prefix="$1"
    if [ -s "${prefix}.err" ]; then
        jq -nRc --arg message "$(tail -40 "${prefix}.err")" '{message: $message}'
    else
        printf '%s\n' '{"message":"Codex request failed"}'
    fi
}

agent_backend_session_id() {
    local jsonl="${1}.jsonl"
    [ -f "$jsonl" ] || return 0
    jq -r 'select(.type == "thread.started") | .thread_id' "$jsonl" 2>/dev/null | head -1
}

agent_backend_perm_args() {
    echo "--sandbox ${CODEX_SANDBOX:-workspace-write}"
}

agent_backend_model_args() {
    local model=""
    case "$1" in
        primary) model="$PRIMARY_MODEL" ;;
        downgrade) model="$DOWNGRADE_MODEL" ;;
        *) model="$1" ;;
    esac
    [ -n "$model" ] && echo "--model $model"
}

agent_backend_resume_args() {
    [ -n "$1" ] && echo "--resume $1"
}

agent_backend_fork_args() {
    [ -n "$1" ] && echo "--resume $1 --fork-session"
}

agent_backend_api_key_env_var() {
    echo ""
}

agent_backend_base_url_env_var() {
    echo ""
}
