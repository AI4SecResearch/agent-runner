# agent-runner

Bash library for running AI agent CLI with reliability features — watchdog timeout, retry with fallback model, progress tracking, and interrupt recovery.

Designed for batch-processing pipelines where an agent (e.g. Claude Code) is invoked repeatedly across many iterations or documents.

## Files

| File | Purpose |
|------|---------|
| `runner.sh` | Generic agent invocation: `agent_once`, `_agent_once_with_watchdog`, `agent_with_retry`, `check_agent_result`. Calls 9 `agent_backend_*` ops defined by the active backend. |
| `backends/<name>.sh` | Backend implementing the 9-op interface for a specific agent CLI (e.g. `claude-code.sh`, `opencode.sh`). |
| `progress.sh` | Iteration progress: `progress_read`, `progress_write`, `progress_iterations` |

## Quick start

```bash
source agent-runner/progress.sh
source agent-runner/runner.sh

OUTPUT_DIR="$PWD/output"
mkdir -p "$OUTPUT_DIR"

agent_with_retry "Summarize this document" "summary"
progress_iterations my-task 10 my_callback
```

## Prerequisites

- `$OUTPUT_DIR` must be set before calling any function
- `jq` must be available in `$PATH`
- The active backend's CLI must be available in `$PATH` (see Backends below)

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OUTPUT_DIR` | (required) | Directory for JSONL logs, error logs, and progress state |
| `AGENT_BACKEND` | `claude-code` | Backend name; sources `backends/<name>.sh` |
| `SANDBOX` | (unset) | Set to `"1"` to use `--dangerously-skip-permissions` |
| `CLAUDE_STALL_TIMEOUT` | `300` | Seconds of no output before killing a stalled process |
| `CLAUDE_TIMEOUT` | `0` | Hard total timeout in seconds; `0` = unlimited |
| `KEY_POOL_CONFIG` | `<repo>/api-keys.json` | Path to key-pool config |

## Backends

The runner is agent-agnostic; all agent-specific behavior lives in
`backends/<name>.sh`. The active backend is selected by `$AGENT_BACKEND`.

### claude-code (default)

- Binary: `claude`
- Prereqs: `CLAUDE_MODEL`, `DOWNGRADE_MODEL` env vars
- Auth: `ANTHROPIC_AUTH_TOKEN` env var (set by the key pool)

### opencode

- Binary: `opencode` (v1.17+)
- Prereqs:
  - `OPENCODE_MODEL`, `OPENCODE_DOWNGRADE_MODEL` env vars (must be `provider/model` form, e.g. `bailian/glm-5.2`)
  - Custom providers configured in `~/.config/opencode/opencode.json`
- Auth: per-provider env var (e.g., `Z_AI_API_KEY` for zhipuai-coding-plan, `BAILIAN_GLM5_API_KEY` for bailian). The key pool picks the var from the active provider's `env_var` field in `api-keys.json`.

### Error handling codes

Error codes in `api-keys.json` `error_handling` are matched as `[NNN]` (3 digits, HTTP status — emitted by opencode) or `[NNNN]` (4 digits, upstream-specific — emitted by claude-code's zhipu path). Map them per provider:

- claude-code + zhipu uses upstream codes (e.g., `"1305"`, `"1308"`).
- opencode + zhipuai-coding-plan uses upstream codes from `responseBody.error.code` (e.g., `"1000"` for auth failure).
- opencode + bailian has no upstream code; the runner falls back to HTTP status codes (e.g., `"401"`, `"429"`).

## Key pool config (`api-keys.json`)

```json
{
  "rotate_every": 5,
  "providers": {
    "zhipu": {
      "keys": ["xxxxx", "yyyyy", "zzzzz"],
      "env_var": "ANTHROPIC_AUTH_TOKEN",
      "error_handling": { "1305": "downgrade", "_default": "rotate_then_downgrade" }
    },
    "zhipuai-coding-plan": {
      "keys": ["aaaaa", "bbbbb"],
      "env_var": "Z_AI_API_KEY",
      "error_handling": { "1000": "rotate_key", "_default": "rotate_then_downgrade" }
    }
  }
}
```

Per-provider fields:
- `keys` (required): list of API keys.
- `env_var` (optional): env var the key pool exports the current key to. Defaults to `ANTHROPIC_AUTH_TOKEN`. Set to the value your `opencode.json` provider reads via `{env:VAR_NAME}`.
- `error_handling` (optional): map of `[NNN]` or `[NNNN]` codes → action. Actions: `rotate_key`, `downgrade`, `rotate_then_downgrade`. `_default` is the fallback.

## Functions

### runner.sh

**`agent_once <prompt> <log_name> [extra_args...]`**

Run the agent CLI once. Writes `$OUTPUT_DIR/<log_name>.jsonl` (event log), `$OUTPUT_DIR/<log_name>.err` (stderr), and prints the result text to stdout.

**`check_agent_result <log_name>`**

Check whether a completed run produced a success result. Returns 0 on success, 1 on error.

**`_agent_once_with_watchdog <prompt> <log_name> [extra_args...]`**

Run the agent in the background with a watchdog that monitors JSONL file growth. Kills the process on stall timeout or total timeout. Returns 0 if completed normally, 1 if killed.

**`agent_with_retry <prompt> <log_name> [extra_args...]`**

Run with watchdog, then retry with a fallback model on failure. Returns 0 if any attempt succeeded, 1 if all attempts failed.

### progress.sh

**`progress_read <task_id>`**

Read the last completed iteration number for a task. Returns `0` if no state exists.

**`progress_write <task_id> <iteration>`**

Persist the completed iteration number. State is stored in `$OUTPUT_DIR/state/<task_id>`.

**`progress_iterations <task_id> <max_iterations> <callback> [callback_args...]`**

Run a callback for iterations 1..max, skipping already-completed ones. The callback receives `<iteration> <max_iterations> [args...]`. Returns: 0 = all done, 1 = callback failed, 2 = already complete (skipped).
