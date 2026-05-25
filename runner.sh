#!/bin/bash

# Agent invocation with reliability features.
# Provides: run_claude, check_claude_result, _run_with_watchdog, run_claude_with_retry
#
# Prerequisites: $OUTPUT_DIR must be set by the caller (e.g. via setup_output_dir).
#
# Environment variables:
#   SANDBOX              Set to "1" in container to skip permission prompts
#   CLAUDE_MODEL         Primary model (default: glm-5-turbo)
#   CLAUDE_STALL_TIMEOUT Seconds before killing a stalled process (default: 300)
#   CLAUDE_TIMEOUT       Hard total timeout, 0 = unlimited (default: 0)
#   CLAUDE_RETRIES       Number of fallback retries (default: 1)

# 检查 run_claude 的输出是否为成功结果
# 用法: check_claude_result <log_name>
# 返回 0=成功  1=失败
check_claude_result() {
    local log_name="$1"
    local jsonl="$OUTPUT_DIR/${log_name}.jsonl"
    local is_error
    is_error=$(jq -r 'select(.type=="result") | .is_error // false' \
        "$jsonl" 2>/dev/null)
    [ "$is_error" != "true" ]
}

# 后台执行 run_claude 并监控 jsonl 增长，超时则 kill
# 用法与 run_claude 一致: _run_with_watchdog <prompt> <log_name> [extra_args...]
# 返回 0=正常结束  1=超时被 kill
_run_with_watchdog() {
    local prompt="$1"
    local log_name="$2"
    shift 2
    local jsonl_file="$OUTPUT_DIR/${log_name}.jsonl"
    local stall_timeout="${CLAUDE_STALL_TIMEOUT:-300}"
    local max_timeout="${CLAUDE_TIMEOUT:-0}"

    run_claude "$prompt" "$log_name" "$@" > /dev/null &
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

# 运行 Claude 调用，失败或超时时自动用 glm-4.7 重试
# 用法: run_claude_with_retry <prompt> <log_name> [extra_args...]
# 返回 0=成功  1=均失败
# 无 stdout 输出，调用方从 jsonl 日志读取结果文本
#
# 环境变量:
#   CLAUDE_RETRIES  重试次数（默认: 1）
run_claude_with_retry() {
    local prompt="$1"
    local log_name="$2"
    shift 2

    # 首次调用（主模型）
    if _run_with_watchdog "$prompt" "$log_name" "$@" \
       && check_claude_result "$log_name"; then
        return 0
    fi

    # 重试（glm-4.7）
    local retries="${CLAUDE_RETRIES:-1}"
    local attempt
    for ((attempt=1; attempt<=retries; attempt++)); do
        echo "          ⚠️ 主模型失败，glm-4.7 重试 $attempt/$retries: $log_name" >&2
        local retry_name="${log_name}-retry${attempt}"
        if _run_with_watchdog "$prompt" "$retry_name" "$@" --model glm-4.7 \
           && check_claude_result "$retry_name"; then
            return 0
        fi
    done

    echo "          ⚠️ glm-4.7 重试全部失败 ($retries 次): $log_name" >&2
    return 1
}

# Run a single Claude step with standard output routing
# Usage: run_claude <prompt> <log_name> [extra_claude_args...]
#
# 环境变量:
#   CLAUDE_MODEL    主模型（默认: glm-5-turbo）
#   LANDLOCK_CONFIG Landlock 配置文件路径（可选，设置后自动包裹）
#   LANDLOCK_RUNNER landlock_runner.py 路径（默认: utils/landlock-runner/landlock_runner.py）
run_claude() {
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

    # 检测会话标志：传入 --session-id / --resume / --continue 时保留会话持久化
    local no_persist="--no-session-persistence"
    for arg in "$@"; do
        case "$arg" in
            --session-id|--resume|-r|--continue|-c) no_persist=""; break ;;
        esac
    done

    local claude_cmd=(claude -p "$prompt" \
        --output-format stream-json --verbose \
        $no_persist \
        $perm_flag \
        --model "${CLAUDE_MODEL:-glm-5-turbo}" \
        "$@")

    if [ -n "$LANDLOCK_CONFIG" ] && [ -f "$LANDLOCK_CONFIG" ]; then
        local runner="${LANDLOCK_RUNNER:-utils/landlock-runner/landlock_runner.py}"
        claude_cmd=(python3 "$runner" "$LANDLOCK_CONFIG" "${claude_cmd[@]}")
    fi

    "${claude_cmd[@]}" 2>"$prefix.err" | tee "$prefix.jsonl" | \
        jq -r 'select(.type=="result") | .result'
}
