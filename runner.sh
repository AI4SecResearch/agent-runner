#!/bin/bash

# Agent invocation with reliability features.
# Provides: agent_once, _agent_once_with_watchdog, agent_with_retry
#
# Prerequisites: $OUTPUT_DIR must be set by the caller (e.g. via setup_output_dir).
#
# Environment variables:
#   SANDBOX              Set to "1" in container to skip permission prompts
#   CLAUDE_MODEL         Primary model (default: glm-5-turbo)
#   AGENT_PROVIDER       Agent backend: claude|codex (default: claude)
#   CODEX_MODEL          Codex model when AGENT_PROVIDER=codex
#   CODEX_SANDBOX        Codex sandbox mode (default: danger-full-access)
#   CODEX_WEB_SEARCH     Codex web search mode: cached|live|disabled
#   CODEX_NETWORK_ACCESS Set to true/1 to enable command network access in workspace-write
#   CODEX_FORK_SESSION_ARG Codex resume fork flag (default: --fork-session; empty disables)
#   CLAUDE_STALL_TIMEOUT Seconds before killing a stalled process (default: 300)
#   CLAUDE_TIMEOUT       Hard total timeout, 0 = unlimited (default: 0)
#   KEY_POOL_CONFIG      Path to api-keys.json (default: api-keys.json)

_runner_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_runner_py="$_runner_dir/runner.py"

_kp_config() { echo "${KEY_POOL_CONFIG:-$_runner_dir/../../api-keys.json}"; }
_kp_state()  { echo "${DATA_DIR}/key-pool-state.json"; }
_codex_session_file() { echo "${OUTPUT_DIR}/codex-sessions.tsv"; }

# ── Key pool (delegates to runner.py) ──────────────────────────────

key_pool_init() {
    [ "${AGENT_PROVIDER:-claude}" = "codex" ] && return 0
    local key=$(python3 "$_runner_py" init --config "$(_kp_config)" --state "$(_kp_state)")
    [ -n "$key" ] && export ANTHROPIC_AUTH_TOKEN="$key"
}

key_pool_rotate() {
    [ "${AGENT_PROVIDER:-claude}" = "codex" ] && return 0
    local key=$(python3 "$_runner_py" rotate --config "$(_kp_config)" --state "$(_kp_state)")
    [ -n "$key" ] && export ANTHROPIC_AUTH_TOKEN="$key"
}

key_pool_on_success() {
    [ "${AGENT_PROVIDER:-claude}" = "codex" ] && return 0
    local key=$(python3 "$_runner_py" on-success --config "$(_kp_config)" --state "$(_kp_state)")
    [ -n "$key" ] && export ANTHROPIC_AUTH_TOKEN="$key"
}

key_pool_disable() {
    [ "${AGENT_PROVIDER:-claude}" = "codex" ] && return 0
    python3 "$_runner_py" disable --key "$ANTHROPIC_AUTH_TOKEN" --config "$(_kp_config)" --state "$(_kp_state)"
}

key_pool_available_size() {
    python3 "$_runner_py" available-size --config "$(_kp_config)" --state "$(_kp_state)"
}

# ── Result check & error classification (delegates to runner.py) ──

# 用法: check_agent_result <log_name>
# 返回 0=成功  1=失败
check_agent_result() {
    if [ "${AGENT_PROVIDER:-claude}" = "codex" ]; then
        [ -s "$OUTPUT_DIR/${1}.out" ]
        return $?
    fi
    python3 "$_runner_py" check-result "$OUTPUT_DIR/${1}.jsonl"
}

classify_agent_error() {
    if [ "${AGENT_PROVIDER:-claude}" = "codex" ]; then
        echo "downgrade:false"
        return 0
    fi
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
    case "${AGENT_PROVIDER:-claude}" in
        claude) agent_once_claude "$@" ;;
        codex)  agent_once_codex "$@" ;;
        *)
            echo "未知 AGENT_PROVIDER: $AGENT_PROVIDER" >&2
            return 2
            ;;
    esac
}

agent_once_claude() {
    local prompt="$1"
    local log_name="$2"
    shift 2
    local prefix="$OUTPUT_DIR/$log_name"
    mkdir -p "$(dirname "$prefix")"
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

_codex_session_lookup() {
    local logical_id="$1"
    local session_file
    session_file="$(_codex_session_file)"
    [ -f "$session_file" ] || return 1
    awk -F'\t' -v id="$logical_id" '$1 == id {value=$2} END {if (value) print value}' "$session_file"
}

_codex_session_record() {
    local logical_id="$1" thread_id="$2"
    [ -z "$logical_id" ] && return 0
    [ -z "$thread_id" ] && return 0
    local session_file
    session_file="$(_codex_session_file)"
    mkdir -p "$(dirname "$session_file")"
    flock "$session_file" printf "%s\t%s\n" "$logical_id" "$thread_id" >> "$session_file"
}

_codex_config_args() {
    if [ -n "${CODEX_WEB_SEARCH:-}" ]; then
        printf '%s\0%s\0' -c "web_search=\"${CODEX_WEB_SEARCH}\""
    fi
    case "${CODEX_NETWORK_ACCESS:-}" in
        1|true|TRUE|yes|YES)
            printf '%s\0%s\0' -c 'sandbox_workspace_write.network_access=true'
            ;;
    esac
}

agent_once_codex() {
    local prompt="$1"
    local log_name="$2"
    shift 2
    local prefix="$OUTPUT_DIR/$log_name"
    mkdir -p "$(dirname "$prefix")"

    local filtered_args=()
    local logical_session_id=""
    local resume_session_id=""
    local fork_session=0
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --model)
                shift 2
                ;;
            --session-id)
                logical_session_id="$2"
                shift 2
                ;;
            --resume)
                resume_session_id="$2"
                shift 2
                ;;
            --fork-session)
                fork_session=1
                shift
                ;;
            *)
                filtered_args+=("$1")
                shift
                ;;
        esac
    done

    local actual_prompt="$prompt"
    if [[ "$prompt" == /fix-json* ]]; then
        local file="${prompt#/fix-json }"
        actual_prompt="请修复 JSON 文件，使其成为严格合法 JSON。只修改该文件，不改变语义。文件路径：$file"
    fi

    local codex_config_args=()
    if command -v mapfile >/dev/null 2>&1; then
        mapfile -d '' -t codex_config_args < <(_codex_config_args)
    fi

    local codex_cmd=()
    if [ -n "$resume_session_id" ]; then
        local actual_session_id
        actual_session_id="$(_codex_session_lookup "$resume_session_id")"
        [ -z "$actual_session_id" ] && actual_session_id="$resume_session_id"
        codex_cmd=(codex exec resume
            --json
            -c "sandbox_mode=\"${CODEX_SANDBOX:-danger-full-access}\""
            --output-last-message "$prefix.out")
        codex_cmd+=("${codex_config_args[@]}")
        if [ -n "${CODEX_MODEL:-}" ]; then
            codex_cmd+=(--model "$CODEX_MODEL")
        fi
        local codex_fork_session_arg
        if [ "${CODEX_FORK_SESSION_ARG+x}" = "x" ]; then
            codex_fork_session_arg="$CODEX_FORK_SESSION_ARG"
        else
            codex_fork_session_arg="--fork-session"
        fi
        if [ "$fork_session" = "1" ] && [ -n "$codex_fork_session_arg" ]; then
            codex_cmd+=("$codex_fork_session_arg")
        fi
        codex_cmd+=("${filtered_args[@]}" "$actual_session_id" -)
    else
        codex_cmd=(codex exec
            --json
            -C "${PROJECT_ROOT:-$PWD}"
            --sandbox "${CODEX_SANDBOX:-danger-full-access}"
            --output-last-message "$prefix.out")
        codex_cmd+=("${codex_config_args[@]}")
        if [ -n "${CODEX_MODEL:-}" ]; then
            codex_cmd+=(--model "$CODEX_MODEL")
        fi
        codex_cmd+=("${filtered_args[@]}" -)
    fi

    printf '%s' "$actual_prompt" | "${codex_cmd[@]}" > "$prefix.jsonl" 2> "$prefix.err"
    local rc=$?
    if [ $rc -eq 0 ] && [ -n "$logical_session_id" ]; then
        local thread_id
        thread_id=$(jq -r 'select(.type == "thread.started") | .thread_id' "$prefix.jsonl" 2>/dev/null | head -1)
        _codex_session_record "$logical_session_id" "$thread_id"
    fi
    return $rc
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

        # result/turn.completed 行出现 → agent 已完成输出，立即退出
        if grep -q -E '"type":"(result|turn.completed)"' "$jsonl_file" 2>/dev/null; then
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
    local fallback_model="${CODEX_MODEL:-glm-4.7}"

    # ── No key pool: only downgrade available ──
    if [ ! -f "$(_kp_config)" ]; then
        action="downgrade"
    fi

    # ── Extract session id from primary attempt for resumption ──
    local session_id=""
    if [ -f "$OUTPUT_DIR/${log_name}.jsonl" ]; then
        if [ "${AGENT_PROVIDER:-claude}" = "codex" ]; then
            session_id=$(jq -r 'select(.type == "thread.started") | .thread_id' \
                "$OUTPUT_DIR/${log_name}.jsonl" 2>/dev/null | head -1)
        else
            session_id=$(jq -r 'select(.session_id != null) | .session_id' \
                "$OUTPUT_DIR/${log_name}.jsonl" 2>/dev/null | head -1)
        fi
    fi

    # ── Execute retry plan from Python ──
    local attempt=0

    local model count
    while IFS=' ' read -r model count; do
        local model_flag=""
        [ "$model" != "primary" ] && model_flag="--model $model"
        [ "${model}" = "${fallback_model}" ] && echo "          ⚠️ 降级至 ${fallback_model}: $log_name" >&2

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
                0) [ "${model}" != "${fallback_model}" ] && key_pool_on_success; return 0;;
                2) return 1;;
            esac
        done
    done < <(python3 "$_runner_py" retry-plan --action "$action" --config "$(_kp_config)" --state "$(_kp_state)" --fallback-model "$fallback_model")

    echo "          ⚠️ 所有模型和 key 均已耗尽: $log_name" >&2
    return 1
}
