#!/bin/bash

# Agent invocation with reliability features (agent-agnostic).
# Provides: agent_once, _agent_once_with_watchdog, agent_with_retry, agent_once_session_resume
#
# All agent-specific behavior (binary, flags, output format, log parsing) lives in
# a backend implementing the 10-op interface (see backends/<name>.sh). The active
# backend is selected by $AGENT_BACKEND (default: claude-code) and sourced from
# common.sh. This file contains only generic orchestration.
#
# Prerequisites: $OUTPUT_DIR must be set by the caller (e.g. via setup_output_dir).
#
# Environment variables:
#   AGENT_BACKEND        Backend name (default: claude-code); sources backends/<name>.sh
#   SANDBOX              Set to "1" in container to skip permission prompts
#   AGENT_STALL_TIMEOUT Seconds before killing a stalled process (default: 300)
#   AGENT_TIMEOUT       Hard total timeout, 0 = unlimited (default: 0)
#   KEY_POOL_CONFIG      Path to providers.jsonc (lpm config; default: <workspace>/providers.jsonc)
#   LLM_PROVIDER_CONFIG  Fallback config path (honored when KEY_POOL_CONFIG unset)

_runner_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_runner_py="$_runner_dir/runner.py"

_kp_config() { echo "${KEY_POOL_CONFIG:-${LLM_PROVIDER_CONFIG:-$_runner_dir/../../providers.jsonc}}"; }
_kp_state()  { echo "${DATA_DIR}/key-pool-state.json"; }

# ── Key pool (delegates to runner.py) ──────────────────────────────

# Tracks the API key env var the last key was exported to, so a changed var name
# unsets the previous one rather than leaking it into the next invocation.
_kp_current_env_var=""

# Apply a key-pool JSON line {key, base_url, primary_model, downgrade_model}:
# export the key to the backend's API key env var, the base_url to the backend's
# base_url env var, and the resolved models to the agent-agnostic
# PRIMARY_MODEL / DOWNGRADE_MODEL. Each is exported only when non-empty, so a
# provider that omits base_url/models leaves the backend defaults in place.
# No-op when the line is empty (no key / no rotation).
_kp_apply() {
    local line="$1"
    [ -n "$line" ] || return 0

    local key base_url pmodel dmodel
    key=$(printf '%s' "$line" | jq -r '.key')
    [ -n "$key" ] || return 0
    base_url=$(printf '%s' "$line" | jq -r '.base_url // ""')
    pmodel=$(printf '%s' "$line" | jq -r '.primary_model // ""')
    dmodel=$(printf '%s' "$line" | jq -r '.downgrade_model // ""')

    # key → API key env var (backend-declared); unset a previously-exported var if
    # its name changed.
    local api_key_var; api_key_var=$(agent_backend_api_key_env_var)
    if [ -n "$api_key_var" ]; then
        if [ -n "$_kp_current_env_var" ] && [ "$_kp_current_env_var" != "$api_key_var" ]; then
            unset "$_kp_current_env_var"
        fi
        export "$api_key_var=$key"
        _kp_current_env_var="$api_key_var"
    fi

    # base_url → base_url env var (backend-declared; empty for backends that
    # route via their own config rather than an env var). Exported only when
    # both the var name and the value are non-empty.
    local base_url_var; base_url_var=$(agent_backend_base_url_env_var)
    if [ -n "$base_url_var" ] && [ -n "$base_url" ]; then
        export "$base_url_var=$base_url"
    fi

    # models → agent-agnostic PRIMARY_MODEL / DOWNGRADE_MODEL. Exported only when
    # the provider specifies them, so an omitted field keeps the backend default.
    [ -n "$pmodel" ] && export "PRIMARY_MODEL=$pmodel"
    [ -n "$dmodel" ] && export "DOWNGRADE_MODEL=$dmodel"
}

key_pool_init() {
    _kp_apply "$(python3 "$_runner_py" init --config "$(_kp_config)" --state "$(_kp_state)")"
}

key_pool_rotate() {
    _kp_apply "$(python3 "$_runner_py" rotate --config "$(_kp_config)" --state "$(_kp_state)")"
}

key_pool_on_success() {
    _kp_apply "$(python3 "$_runner_py" on-success --config "$(_kp_config)" --state "$(_kp_state)")"
}

key_pool_disable() {
    [ -n "$_kp_current_env_var" ] || return 0
    python3 "$_runner_py" disable --key "${!_kp_current_env_var}" \
        --config "$(_kp_config)" --state "$(_kp_state)"
}

key_pool_available_size() {
    python3 "$_runner_py" available-size --config "$(_kp_config)" --state "$(_kp_state)"
}

# ── Result check & error classification ───────────────────────────

# 用法: check_agent_result <log_name>
# 返回 0=成功  1=失败
check_agent_result() {
    agent_backend_result_ok "$OUTPUT_DIR/$1"
}

# 用法: classify_agent_error <log_name>
# 输出 "<action>:<disable_flag>"
classify_agent_error() {
    local text=$(agent_backend_result_text "$OUTPUT_DIR/$1")
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
    local stall_timeout="${AGENT_STALL_TIMEOUT:-300}"
    local max_timeout="${AGENT_TIMEOUT:-0}"

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
        if agent_backend_is_complete "$OUTPUT_DIR/$log_name"; then
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

# 运行 agent_once_with_watchdog 并检查结果。
# 返回: 0=成功  1=失败(可重试)
# （历史名：disable 决策现已上移到 react 驱动的 agent_with_retry 循环，
# 此函数只做"执行 + 检查结果"，不再内部 disable。函数名保留以减少改动面。）
agent_once_with_disable() {
    local prompt="$1"
    local log_name="$2"
    shift 2

    _agent_once_with_watchdog "$prompt" "$log_name" "$@" \
        && check_agent_result "$log_name" \
        && return 0

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

# ── Retry orchestration (reactive: one step per failure) ─────────

# 用法: agent_with_retry <prompt> <log_name> [extra_args...]
# 返回 0=成功  1=均失败
#
# 反应式重试：不预先制定完整计划，而是每次失败后调 `react` 子命令拿到
# 单步恢复策略（逗号组合的原子 disable/rotate/downgrade，或 stop），执行
# 该步后再试；下次失败重新 react，按【新错误】重新决策。策略由 providers.jsonc
# 中 provider 的 errorHandling 决定。
agent_with_retry() {
    local prompt="$1"
    local log_name="$2"
    shift 2

    key_pool_init

    # ── Primary attempt (current key, primary model) ──
    agent_once_with_disable "$prompt" "$log_name" "$@"
    case $? in
        0) key_pool_on_success; return 0;;
    esac

    # ── Extract session_id from primary attempt for resumption ──
    local base_log="$log_name"
    local session_id=$(agent_backend_session_id "$OUTPUT_DIR/$base_log")

    # ── Reactive loop ──
    local attempt=0
    local cur_log="$base_log"   # most recent failure's log name (react reads it)
    # 兜底上限：防 react 与池状态不同步导致死循环。正常靠 react 返回 stop 终止。
    # n=可用 key 数；+2 给降级档留余量。无 key pool 时 n=0 → 2 次。
    local n=$(key_pool_available_size 2>/dev/null || echo 0)
    local max_attempts=$(( n + 2 ))
    [ "$max_attempts" -gt 0 ] || max_attempts=2

    while [ "$attempt" -lt "$max_attempts" ]; do
        # 把上次失败喂给 react，拿单步策略（对新错误重新分类）
        local step
        step=$(agent_backend_result_text "$OUTPUT_DIR/$cur_log" \
               | python3 "$_runner_py" react --text - \
                 --config "$(_kp_config)" --state "$(_kp_state)")
        if [ "$step" = "stop" ]; then
            echo "          ⚠️ 资源耗尽（无可用 key/模型）: $cur_log" >&2
            return 1
        fi

        # 执行策略里的原子
        case ",$step," in *,disable,*) key_pool_disable;; esac
        case ",$step," in *,rotate,*)  key_pool_rotate;; esac
        local model=primary
        case ",$step," in *,downgrade,*) model=downgrade;; esac

        attempt=$((attempt + 1))
        local name="${base_log}-r${attempt}"
        local model_args=$(agent_backend_model_args "$model")
        echo "          ⚠️ 重试 $attempt/$max_attempts ($model / $step): $base_log" >&2

        if [ -n "$session_id" ]; then
            agent_once_session_resume "继续" "$name" "$session_id" $model_args
        else
            agent_once_with_disable "$prompt" "$name" $model_args
        fi

        case $? in
            0) [ "$model" != "downgrade" ] && key_pool_on_success; return 0;;
        esac
        # 失败 → 下次循环 react 读这次重试的日志
        cur_log="$name"
    done

    echo "          ⚠️ 重试次数达上限 ($max_attempts): $log_name" >&2
    return 1
}
