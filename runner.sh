#!/bin/bash

# Agent invocation with reliability features.
# Provides: agent_once, _agent_once_with_watchdog, agent_with_retry
#
# Prerequisites: $OUTPUT_DIR must be set by the caller (e.g. via setup_output_dir).
#
# Environment variables:
#   SANDBOX              Set to "1" in container to skip permission prompts
#   CLAUDE_MODEL         Primary model (default: glm-5-turbo)
#   CLAUDE_STALL_TIMEOUT Seconds before killing a stalled process (default: 300)
#   CLAUDE_TIMEOUT       Hard total timeout, 0 = unlimited (default: 0)
#   KEY_POOL_CONFIG      Path to api-keys.json (default: api-keys.json)

_runner_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_runner_py="$_runner_dir/runner.py"

_kp_config() { echo "${KEY_POOL_CONFIG:-$_runner_dir/../../api-keys.json}"; }
_kp_state()  { echo "${DATA_DIR}/key-pool-state.json"; }

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

# ── Result check & error classification (delegates to runner.py) ──

# 用法: check_agent_result <log_name>
# 返回 0=成功  1=失败
check_agent_result() {
    python3 "$_runner_py" check-result "$OUTPUT_DIR/${1}.jsonl"
}

classify_agent_error() {
    python3 "$_runner_py" classify "$OUTPUT_DIR/${1}.jsonl" \
        --config "$(_kp_config)" --state "$(_kp_state)"
}

# ── Process execution (pure bash) ─────────────────────────────────

# Run a single agent step with standard output routing
# Usage: agent_once <prompt> <log_name> [extra_args...]
#
# 环境变量:
#   CLAUDE_MODEL    主模型（默认: glm-5-turbo）
#   LANDLOCK_CONFIG Landlock 配置文件路径（可选，设置后自动包裹）
#   LANDLOCK_RUNNER landlock_runner.py 路径（默认: utils/landlock-runner/landlock_runner.py）
agent_once() {
    local prompt="$1"
    local log_name="$2"
    shift 2
    local prefix="$OUTPUT_DIR/$log_name"
    local perm_flag
    if [ "${SANDBOX:-}" = "1" ]; then
        perm_flag="--dangerously-skip-permissions"
    else
        perm_flag="--permission-mode acceptEdits"
    fi

    local agent_cmd=(claude -p "$prompt" \
        --output-format stream-json --verbose \
        $perm_flag \
        --model "${CLAUDE_MODEL:-glm-5-turbo}" \
        "$@")

    if [ -n "$LANDLOCK_CONFIG" ] && [ -f "$LANDLOCK_CONFIG" ]; then
        local runner="${LANDLOCK_RUNNER:-utils/landlock-runner/landlock_runner.py}"
        agent_cmd=(python3 "$runner" "$LANDLOCK_CONFIG" "${agent_cmd[@]}")
    fi

    "${agent_cmd[@]}" 2>"$prefix.err" | tee "$prefix.jsonl" | \
        jq -r 'select(.type=="result") | .result'
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

        # result 行出现 → claude 已完成输出，立即退出
        if grep -q '"type":"result"' "$jsonl_file" 2>/dev/null; then
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
        # 先杀子进程（pipeline 中的 claude/tee/jq），再杀父 shell
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
    local session_id=""
    if [ -f "$OUTPUT_DIR/${log_name}.jsonl" ]; then
        session_id=$(jq -r 'select(.session_id != null) | .session_id' \
            "$OUTPUT_DIR/${log_name}.jsonl" 2>/dev/null | head -1)
    fi

    # ── Execute retry plan from Python ──
    local attempt=0

    local model count
    while IFS=' ' read -r model count; do
        local model_flag=""
        [ "$model" != "primary" ] && model_flag="--model $model"
        [ "$model" = "glm-4.7" ] && echo "          ⚠️ 降级至 glm-4.7: $log_name" >&2

        local i
        for ((i=0; i<count; i++)); do
            key_pool_rotate
            attempt=$((attempt + 1))
            local name="${log_name}-r${attempt}"

            if [ -n "$session_id" ]; then
                echo "          ⚠️ 续接会话 $((i+1))/$count ($model): $log_name" >&2
                agent_once_with_disable "继续" "$name" --resume "$session_id" $model_flag
            else
                echo "          ⚠️ 重试 $((i+1))/$count ($model): $log_name" >&2
                agent_once_with_disable "$prompt" "$name" $model_flag
            fi

            case $? in
                0) [ "$model" != "glm-4.7" ] && key_pool_on_success; return 0;;
                2) return 1;;
            esac
        done
    done < <(python3 "$_runner_py" retry-plan --action "$action" --config "$(_kp_config)" --state "$(_kp_state)")

    echo "          ⚠️ 所有模型和 key 均已耗尽: $log_name" >&2
    return 1
}
