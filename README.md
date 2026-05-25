# agent-runner

Bash library for running AI agent CLI with reliability features — watchdog timeout, retry with fallback model, progress tracking, and interrupt recovery.

Designed for batch-processing pipelines where an agent (e.g. Claude Code) is invoked repeatedly across many iterations or documents.

## Files

| File | Purpose |
|------|---------|
| `runner.sh` | Agent invocation: `run_claude`, `_run_with_watchdog`, `run_claude_with_retry`, `check_claude_result` |
| `progress.sh` | Iteration progress: `progress_read`, `progress_write`, `progress_iterations` |

## Quick start

```bash
source agent-runner/progress.sh
source agent-runner/runner.sh

OUTPUT_DIR="$PWD/output"
mkdir -p "$OUTPUT_DIR"

run_claude_with_retry "Summarize this document" "summary"
progress_iterations my-task 10 my_callback
```

## Prerequisites

- `$OUTPUT_DIR` must be set before calling any function
- `jq` must be available in `$PATH`
- `claude` CLI must be available for `run_claude` and friends

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OUTPUT_DIR` | (required) | Directory for JSONL logs, error logs, and progress state |
| `SANDBOX` | (unset) | Set to `"1"` to use `--dangerously-skip-permissions` |
| `CLAUDE_MODEL` | `glm-5-turbo` | Primary model for agent invocation |
| `CLAUDE_STALL_TIMEOUT` | `300` | Seconds of no output before killing a stalled process |
| `CLAUDE_TIMEOUT` | `0` | Hard total timeout in seconds; `0` = unlimited |
| `CLAUDE_RETRIES` | `1` | Number of fallback retries on failure |

## Functions

### runner.sh

**`run_claude <prompt> <log_name> [extra_args...]`**

Run the agent CLI once. Writes `$OUTPUT_DIR/<log_name>.jsonl` (stream-json log), `$OUTPUT_DIR/<log_name>.err` (stderr), and prints the result text to stdout.

**`check_claude_result <log_name>`**

Check whether a completed run produced a success result. Returns 0 on success, 1 on error.

**`_run_with_watchdog <prompt> <log_name> [extra_args...]`**

Run the agent in the background with a watchdog that monitors JSONL file growth. Kills the process on stall timeout or total timeout. Returns 0 if completed normally, 1 if killed.

**`run_claude_with_retry <prompt> <log_name> [extra_args...]`**

Run with watchdog, then retry with a fallback model on failure. Returns 0 if any attempt succeeded, 1 if all attempts failed.

### progress.sh

**`progress_read <task_id>`**

Read the last completed iteration number for a task. Returns `0` if no state exists.

**`progress_write <task_id> <iteration>`**

Persist the completed iteration number. State is stored in `$OUTPUT_DIR/state/<task_id>`.

**`progress_iterations <task_id> <max_iterations> <callback> [callback_args...]`**

Run a callback for iterations 1..max, skipping already-completed ones. The callback receives `<iteration> <max_iterations> [args...]`. Returns: 0 = all done, 1 = callback failed, 2 = already complete (skipped).
