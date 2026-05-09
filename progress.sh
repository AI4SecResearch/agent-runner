#!/bin/bash

# Iteration progress tracking with interrupt recovery.
# Provides: progress_read, progress_write, run_iterations
#
# Prerequisites: $OUTPUT_DIR must be set by the caller (e.g. via setup_output_dir).

# --- 任务进度管理 ---

# 读取任务的已完成迭代数
# 用法: progress_read <task_id>
progress_read() {
    local state_file="$OUTPUT_DIR/state/$1"
    if [ -f "$state_file" ]; then
        cat "$state_file"
    else
        echo 0
    fi
}

# 写入任务的已完成迭代数
# 用法: progress_write <task_id> <iteration>
progress_write() {
    mkdir -p "$OUTPUT_DIR/state"
    echo "$2" > "$OUTPUT_DIR/state/$1"
}

# 对用户指定任务执行多轮迭代，自动跳过已完成轮次并支持断点恢复
# 用法: run_iterations <task_id> <max_iterations> <callback> [callback_args...]
# 回调签名: callback <iteration> <max_iterations> [callback_args...]
# 返回: 0=完成  1=失败中止  2=已跳过
run_iterations() {
    local task_id="$1" max_iter="$2" callback="$3"
    shift 3

    local completed
    completed=$(progress_read "$task_id")

    if [ "$completed" -ge "$max_iter" ]; then
        return 2
    fi

    for ((i=completed+1; i<=max_iter; i++)); do
        "$callback" "$i" "$max_iter" "$@"
        if [ $? -ne 0 ]; then
            return 1
        fi
        progress_write "$task_id" "$i"
    done
    return 0
}
