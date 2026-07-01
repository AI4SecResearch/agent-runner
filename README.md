# agent-runner

Bash library for running AI agent CLI with reliability features — watchdog timeout, retry with fallback model, progress tracking, and interrupt recovery.

Designed for batch-processing pipelines where an agent (e.g. Claude Code) is invoked repeatedly across many iterations or documents.

## Files

| File | Purpose |
|------|---------|
| `runner.sh` | Generic agent invocation: `agent_with_retry`, `agent_once_session_resume`, `agent_once`. Calls 11 `agent_backend_*` ops defined by the active backend. |
| `runner.py` | Thin adapter: forwards key-pool ops (init/rotate/disable/classify/react) to the vendored lpm (`llm_provider_manager.keypool`). |
| `backends/<name>.sh` | Backend implementing the 11-op interface for a specific agent CLI (e.g. `claude-code.sh`, `opencode.sh`). |
| `progress.sh` | Iteration progress: `progress_read`, `progress_write`, `progress_iterations` |
| `llm-provider-manager/` | Vendored [llm-provider-manager](llm-provider-manager/) (git subtree) — provider/key config, key-pool rotation, error classification. |

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
| `AGENT_STALL_TIMEOUT` | `300` | Seconds of no output before killing a stalled process |
| `AGENT_TIMEOUT` | `0` | Hard total timeout in seconds; `0` = unlimited |
| `KEY_POOL_CONFIG` | `<workspace>/providers.jsonc` | Path to the lpm key-pool config (providers.jsonc) |
| `LPM_SRC` | vendored `llm-provider-manager/src` | Override the lpm copy the adapter imports (e.g. point at a dev checkout) |

## Backends

The runner is agent-agnostic; all agent-specific behavior lives in
`backends/<name>.sh`. The active backend is selected by `$AGENT_BACKEND`.

### claude-code (default)

- Binary: `claude`
- Prereqs: `PRIMARY_MODEL`, `DOWNGRADE_MODEL` env vars (the key pool exports the active provider's models; shell values serve as backend defaults)
- API key: `ANTHROPIC_AUTH_TOKEN` env var (set by the key pool); base_url via `ANTHROPIC_BASE_URL` (set by the key pool from the provider's `base_url`)

### opencode

- Binary: `opencode` (v1.17+)
- Prereqs:
  - `PRIMARY_MODEL`, `DOWNGRADE_MODEL` env vars (must be `provider/model` form, e.g. `bailian/glm-5.2`; the key pool exports the active provider's models)
  - Custom providers configured in `~/.config/opencode/opencode.json` (endpoint URL + protocol live here, keyed by the model's provider prefix; the key pool's per-provider `base_url` is unused for opencode)
- API key: env var configured in `opencode.json` (default `Z_AI_API_KEY`); the key pool exports the current key to `agent_backend_api_key_env_var`.

### Error handling codes

Error codes are matched as `[NNN]` (3 digits, HTTP status — emitted by opencode) or `[NNNN]` (4 digits, upstream-specific — emitted by claude-code's zhipu path). Each provider module (in vendored lpm's `providers/`) ships a default error-handling table; `providers.jsonc`'s optional `errorHandling` overrides it per deployment:

- claude-code + zhipu uses upstream codes (e.g., `"1305"`, `"1308"`) — defaults live in vendored `llm-provider-manager/src/llm_provider_manager/providers/zhipu.py`.
- opencode + bailian has no upstream code; the runner falls back to HTTP status codes (e.g., `"401"`, `"429"`).

## Key pool config (`providers.jsonc`)

Provider/key config is [llm-provider-manager](llm-provider-manager/)'s `providers.jsonc` (vendored); see its [README](llm-provider-manager/README.md) / [ARCHITECTURE.md](llm-provider-manager/ARCHITECTURE.md) for the full schema. Sketch:

```jsonc
{
  "settings": { "rotateEvery": 5, "disableTtlHours": 5 },
  "providers": [
    { "id": "zhipu", "type": "symmetric",
      "baseURLs": { "anthropic": "https://open.bigmodel.cn/api/anthropic" },
      "keys": [ { "id": "main", "key": "…" } ],
      "models": [ {"id":"glm-5-turbo",…}, {"id":"glm-4.7",…} ] }
  ]
}
```

Key points: `baseURLs` is a protocol→url map (the keypool picks by active agent); `models[0]`/`[1]` are primary/downgrade (overridable via `primaryModel`/`downgradeModel`); `errorHandling` overrides each provider module's built-in defaults. The pool rotates across all providers' keys as one flat pool; rotating into a different provider re-applies that provider's base_url + models (cross-provider failover). The API key env var stays backend-declared.

## Functions

### runner.sh

**`agent_once <prompt> <log_name> [extra_args...]`**

Run the agent CLI once. Writes `$OUTPUT_DIR/<log_name>.jsonl` (event log), `$OUTPUT_DIR/<log_name>.err` (stderr), and prints the result text to stdout.

**`check_agent_result <log_name>`**

Check whether a completed run produced a success result. Returns 0 on success, 1 on error.

**`_agent_once_with_watchdog <prompt> <log_name> [extra_args...]`**

Run the agent in the background with a watchdog that monitors JSONL file growth. Kills the process on stall timeout or total timeout. Returns 0 if completed normally, 1 if killed.

**`agent_with_retry <prompt> <log_name> [extra_args...]`**

Run with watchdog; on failure, retry reactively — after each failed attempt, lpm's `react` classifies that attempt's error and returns a one-step recovery strategy (comma-joined atoms: `disable`/`rotate`/`downgrade`, or `stop`), which the loop applies before the next attempt. The next failure is classified anew, so a fresh key that hits a different error code gets a fitting recovery. Returns 0 if any attempt succeeded, 1 if all failed.

**`agent_once_session_resume <prompt> <log_name> <session_id> [extra_args...]`**

Run once, resuming an existing session if the backend supports it (else fall back to a fresh session with `$prompt`). As a single-shot entry point (no outer retry loop), it disables the current key itself when the error calls for it. Returns 0 on success, 1 on retryable failure, 2 if the key is exhausted and no key pool is configured.

### progress.sh

**`progress_read <task_id>`**

Read the last completed iteration number for a task. Returns `0` if no state exists.

**`progress_write <task_id> <iteration>`**

Persist the completed iteration number. State is stored in `$OUTPUT_DIR/state/<task_id>`.

**`progress_iterations <task_id> <max_iterations> <callback> [callback_args...]`**

Run a callback for iterations 1..max, skipping already-completed ones. The callback receives `<iteration> <max_iterations> [args...]`. Returns: 0 = all done, 1 = callback failed, 2 = already complete (skipped).
