#!/bin/bash

# Agent invocation with reliability features (agent-agnostic).
# Provides: agent_once, _agent_once_with_watchdog, agent_with_retry, agent_once_session_resume
#
# All agent-specific behavior (binary, flags, output format, log parsing) lives in
# a backend implementing the 9-op interface (see backends/<name>.sh). The active
# backend is selected by $AGENT_BACKEND (default: claude-code) and sourced from
# common.sh. This file contains only generic orchestration.
#
# Prerequisites: $OUTPUT_DIR must be set by the caller (e.g. via setup_output_dir).
#
# Environment variables:
#   AGENT_BACKEND        Backend name (default: claude-code); sources backends/<name>.sh
#   SANDBOX              Set to "1" in container to skip permission prompts
#   CLAUDE_STALL_TIMEOUT Seconds before killing a stalled process (default: 300)
#   CLAUDE_TIMEOUT       Hard total timeout, 0 = unlimited (default: 0)
#   KEY_POOL_CONFIG      Path to api-keys.json (default: api-keys.json)
#   LANDLOCK_CONFIG      Optional landlock config; wraps each agent command
#   LANDLOCK_RUNNER      landlock_runner.py path (default: utils/landlock-runner/landlock_runner.py)

_runner_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_runner_py="$_runner_dir/runner.py"

_kp_config() { echo "${KEY_POOL_CONFIG:-$_runner_dir/../../api-keys.json}"; }
_kp_state()  { echo "${DATA_DIR}/key-pool-state.json"; }

# Run a command under landlock if LANDLOCK_CONFIG is set, else run it directly.
# Used by backends inside agent_backend_invoke so landlock stays generic.
_landlock_wrap() {
    if [ -n "$LANDLOCK_CONFIG" ] && [ -f "$LANDLOCK_CONFIG" ]; then
        python3 "${LANDLOCK_RUNNER:-utils/landlock-runner/landlock_runner.py}" "$LANDLOCK_CONFIG" "$@"
    else
        "$@"
    fi
}

# ── Key pool (delegates to runner.py) ──────────────────────────────

key_pool_init() {
    local key=$(python3 "$_runner_py" init --config "$(_kp_config)" --state "$(_kp_state)")
    [ -n "$key" ] && export ANTHROPIC_AUTH_TOKEN="$key"
}

key_pool_rotate() {
    local key=$(python3 "$_runner_py" rotate --config "$(_kp_config)" --state "$(_kp_state)")
    [ -n "$key" ] && export ANTHROPIC_AUTH_TOKEN="$key"
}

key_pool_on_success() {
    local key=$(python3 "$_runner_py" on-success --config "$(_kp_config)" --state "$(_kp_state)")
    [ -n "$key" ] && export ANTHROPIC_AUTH_TOKEN="$key"
}

key_pool_disable() {
    python3 "$_runner_py" disable --key "$ANTHROPIC_AUTH_TOKEN" --config "$(_kp_config)" --state "$(_kp_state)"
}

key_pool_available_size() {
    python3 "$_runner_py" available-size --config "$(_kp_config)" --state "$(_kp_state)"
}

# ── Result check & error classification ───────────────────────────

# 用法: check_agent_result <log_name>
# 返回 0=成功  1=失败
check_agent_result() {
    agent_backend_result_ok "$1"
}

# 用法: classify_agent_error <log_name>
# 输出 "<action>:<disable_flag>"
classify_agent_error() {
    local text=$(agent_backend_result_text "$1")
    printf '%s' "$text" | python3 "$_runner_py" classify --text - \
        --config "$(_kp_config)" --state "$(_kp_state)"
}

# ── Process execution (pure bash, generic) ────────────────────────

# Run a single agent step with standard output routing.
# Usage: agent_once <prompt> <log_name> [extra_args...]
# Extra args are passed through to the agent; a caller-supplied --model takes
# precedence over the primary model (exactly one --model is emitted).
agent_once() {
    local prompt="$1"
    local log_name="$2"
    shift 2
    local prefix="$OUTPUT_DIR/$log_name"

    local -a argv=( $(agent_backend_perm_args) )
    local a has_model=0
    for a in "$@"; do [ "$a" = "--model" ] && has_model=1; done
    [ "$has_model" -eq 0 ] && argv+=( $(agent_backend_model_args primary) )
    argv+=( "$@" )

    agent_backend_invoke "$prompt" "$prefix" "${argv[@]}"
}

# 后台执行 agent_once 并监控 jsonl 增长，超时则 kill
# 用法与 agent_once 一致: _agent_once_with_watchdog <prompt> <log_name> [extra_args...]
# 返回 0=正常结束  1=超时被 kill
_agent_once_with_watchdog() {
    local prompt="$1"
    local log_name="$2"
    shift 2
    local jsonl_file="$OUTPUT_DIR/${log_name}.jsonl"
    local stall_timeout="${CLAUDE_STALL_TIMEOUT:-300}"
    local max_timeout="${CLAUDE_TIMEOUT:-0}"

    agent_once "$prompt" "$log_name" "$@" > /dev/null &
    local job_pid=$!

    local start_time last_size last_growth
    start_time=$(date +%s)
    last_size=0
    last_growth=$start_time
    local timed_out=0 timeout_reason=""

    while kill -0 "$job_pid" 2>/dev/null; do
        local now elapsed stall_elapsed current_size
        now=$(date +%s)
        elapsed=$((now - start_time))

        # agent 已完成输出 → 立即退出（由后端判断其原生日志是否出现结果）
        if agent_backend_is_complete "$log_name"; then
            break
        fi

        # 总超时
        if [ "$max_timeout" -gt 0 ] && [ "$elapsed" -ge "$max_timeout" ]; then
            timed_out=1; timeout_reason="总超时 ${elapsed}s >= ${max_timeout}s"
            break
        fi

        # 文件增长检测
        current_size=$(stat -c %s "$jsonl_file" 2>/dev/null || echo 0)
        if [ "$current_size" -gt "$last_size" ]; then
            last_size="$current_size"
            last_growth=$now
        fi

        # 无进展超时
        stall_elapsed=$((now - last_growth))
        if [ "$stall_timeout" -gt 0 ] && [ "$stall_elapsed" -ge "$stall_timeout" ]; then
            timed_out=1; timeout_reason="无进展 ${stall_elapsed}s >= ${stall_timeout}s"
            break
        fi

        sleep 10
    done

    if [ "$timed_out" -eq 1 ]; then
        # 先杀子进程（pipeline 中的 agent/tee/jq），再杀父 shell
        # 反过来会导致子 shell 先死，子进程被 init 收养，pkill -P 找不到
        pkill -P "$job_pid" 2>/dev/null
        kill "$job_pid" 2>/dev/null
        wait "$job_pid" 2>/dev/null
        echo "          ⚠️ 超时($timeout_reason): $log_name" >&2
        return 1
    fi

    wait "$job_pid"
    return 0
}

# 运行 agent_once_with_watchdog 并检查结果，处理额度耗尽
# 返回: 0=成功  1=失败(可重试)  2=额度耗尽且无key pool(放弃)
agent_once_with_disable() {
    local prompt="$1"
    local log_name="$2"
    shift 2

    _agent_once_with_watchdog "$prompt" "$log_name" "$@" \
        && check_agent_result "$log_name" \
        && return 0

    local classify_result=$(classify_agent_error "$log_name")
    local should_disable="${classify_result##*:}"

    if [ "$should_disable" = "true" ]; then
        if [ -f "$(_kp_config)" ]; then
            key_pool_disable
        else
            echo "          ⚠️ 额度耗尽且无 key pool: $log_name" >&2
            return 2
        fi
    fi

    return 1
}

# 恢复会话执行；若后端不支持恢复（agent_backend_resume_args 为空），则退化为全新会话。
# 用法: agent_once_session_resume <prompt> <log_name> <session_id> [extra_args...]
agent_once_session_resume() {
    local prompt="$1"
    local log_name="$2"
    local sid="$3"
    shift 3
    local resume_args=$(agent_backend_resume_args "$sid")
    if [ -n "$resume_args" ]; then
        agent_once_with_disable "$prompt" "$log_name" $resume_args "$@"
    else
        agent_once_with_disable "$prompt" "$log_name" "$@"
    fi
}

# ── Retry orchestration (bash loop, Python-driven plan) ───────────

# 用法: agent_with_retry <prompt> <log_name> [extra_args...]
# 返回 0=成功  1=均失败
#
# Python 生成重试计划 (retry-plan)，bash 通用循环执行。
# 恢复策略由 api-keys.json 中 provider 的 error_handling 配置决定。
agent_with_retry() {
    local prompt="$1"
    local log_name="$2"
    shift 2

    key_pool_init

    # ── Primary attempt (current key, primary model) ──
    agent_once_with_disable "$prompt" "$log_name" "$@"
    case $? in
        0) key_pool_on_success; return 0;;
        2) return 1;;
    esac

    local classify_result=$(classify_agent_error "$log_name")
    local action="${classify_result%%:*}"

    # ── No key pool: only downgrade available ──
    if [ ! -f "$(_kp_config)" ]; then
        action="downgrade"
    fi

    # ── Extract session_id from primary attempt for resumption ──
    local session_id=$(agent_backend_session_id "$log_name")

    # ── Execute retry plan from Python ──
    local attempt=0

    local model count
    while IFS=' ' read -r model count; do
        local model_args=$(agent_backend_model_args "$model")

        local i
        for ((i=0; i<count; i++)); do
            key_pool_rotate
            attempt=$((attempt + 1))
            local name="${log_name}-r${attempt}"

            if [ -n "$session_id" ]; then
                echo "          ⚠️ 续接会话 $((i+1))/$count ($model): $log_name" >&2
                agent_once_session_resume "继续" "$name" "$session_id" $model_args
            else
                echo "          ⚠️ 重试 $((i+1))/$count ($model): $log_name" >&2
                agent_once_with_disable "$prompt" "$name" $model_args
            fi

            case $? in
                0) [ "$model" != "downgrade" ] && key_pool_on_success; return 0;;
                2) return 1;;
            esac
        done
    done < <(python3 "$_runner_py" retry-plan --action "$action" --config "$(_kp_config)" --state "$(_kp_state)")

    echo "          ⚠️ 所有模型和 key 均已耗尽: $log_name" >&2
    return 1
}
