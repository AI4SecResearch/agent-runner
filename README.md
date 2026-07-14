# agent-runner (Python)

A reliable agent-CLI execution layer — **watchdog timeouts, reactive retry with
model downgrade, multi-provider key-pool rotation, and session reuse** — exposed
as both a **Python library** and a **process** (`python -m` or a thin `.sh`
wrapper), so it can serve as a "high-reliability agent" parent process for bash
pipelines or drop into a Python codebase as a dependency.

This is a **self-contained, cross-platform rewrite** of the original
[bash agent-runner](..): the orchestration logic is ported 1:1 (same public
API names, signatures, exit codes, and invariants) but reimplemented natively
in Python with **no runtime dependency on bash or `jq`**, and **no coupling to
the bash project**. The vendored `llm-provider-manager` (lpm) is bundled inside
(`agent_runner/_vendor/`).

## Two call shapes

**库形态**(Python 上层):

```python
import os, sys
sys.path.insert(0, "/path/to/agent-runner-py")  # 或:pip install -e .

os.environ["OUTPUT_DIR"] = "/var/run/mytask"     # 必填(日志 + 状态根)
os.environ["AGENT_BACKEND"] = "claude-code"       # 或 "opencode";默认 claude-code

from agent_runner import agent_with_retry, agent_with_retry_session_resume
res = agent_with_retry("总结这份文档", "summary")   # 返回 Result
if res:
    # 多步会话:用上一步的 session_id 续接
    agent_with_retry_session_resume("精修 markdown", "refined", res.session_id)
```

**进程形态**(bash / 任意语言上层 —— 本项目作为一个"高可靠 agent"子进程):

```bash
# 经 .sh 封装(自动设 PYTHONPATH,exec python -m):
sid=$(agent-runner.sh new "总结这份文档" "summary")   # stdout = session_id
echo $?    # 0 = 成功,1 = 均失败,2 = 额度耗尽且无密钥池

# 多步会话:把上一步的 sid 传给 resume
agent-runner.sh resume "精修 markdown" "refined" "$sid"
```

进程形态的输出通道分工:
- `$?` —— 成败(0 / 1 / 2)。
- **stdout —— session_id 一行**(无则空行),供 `sid=$(...)` 捕获。
- **stderr —— 诊断**(重试 / 超时 / 资源耗尽通告,给人看)。
- 结果文本不进 stdout —— 已全量留存于 `$OUTPUT_DIR/<log_name>.jsonl`;库形态则经 `Result.text` 给到调用方。

entry(进程形态取一):`new` / `resume` / `fork` / `once` / `agent_with_retry`
(全名如 `agent_with_retry_session_new` 亦接受)。`resume`/`fork`/`once` 在
`log_name` 之后还需 `session_id`;再之后的参数透传给 agent(如 `--model x`)。

## What it does

For each invocation it: (1) selects a key + provider config from the pool, (2)
runs the agent in a backgrounded process group, (3) watches for early completion
/ stall / hard timeout (killing the whole tree on timeout), (4) on failure
**reactively** classifies the error and applies one recovery step (disable bad
key / rotate to next / downgrade model) then retries, re-classifying each new
failure. Same layered design and 7 invariants as the bash engine — see the
parent repo's `ARCHITECTURE.md`.

## Configuration (environment variables)

Same surface as the bash engine, so both behave identically under the same config:

| Var | Default | Purpose |
|-----|---------|---------|
| `OUTPUT_DIR` | (required) | JSONL logs, error logs, run state |
| `AGENT_BACKEND` | `claude-code` | backend name → `agent_runner.backends` registry |
| `DATA_DIR` | `OUTPUT_DIR` | key-pool state root (`key-pool-state.json`) |
| `SANDBOX` | (unset) | `"1"` → `--dangerously-skip-permissions` |
| `AGENT_STALL_TIMEOUT` | `300` | seconds of no output before killing a stalled process |
| `AGENT_TIMEOUT` | `0` | hard total timeout (0 = unlimited) |
| `KEY_POOL_CONFIG` | `<DATA_DIR>/providers.jsonc` | lpm provider/key config path |
| `LPM_SRC` | bundled `_vendor` | override the lpm copy (e.g. a dev checkout) |
| `LANDLOCK_CONFIG` / `LANDLOCK_RUNNER` | (unset) | landlock-sandbox the agent (Linux only) |

## Backends

`agent_runner/backends/` — one module per agent CLI implementing the 11-op
contract (`invoke` / `is_complete` / `result_ok` / `result_text` / `session_id`
/ perm/model/resume/fork flag fragments / env-var names), using `subprocess` +
the `json` stdlib (no `jq`). Current backends: `claude_code.py`, `opencode.py`.
Add a backend = new module + one `REGISTRY` entry; the engine never references a
concrete agent.

## Cross-platform

- **Paths**: `pathlib` throughout; no hardcoded Unix separators; agent binaries
  resolved via `PATH`. Output/state live under the caller-supplied `OUTPUT_DIR`
  (never hardcoded `/tmp`).
- **Key-pool locking** (`agent_runner/_vendor/.../keypool.py`): the advisory file
  lock that guards the shared state file is platform-abstracted into `_flock_*`
  helpers — `fcntl.flock` on POSIX, `msvcrt.locking` (degraded to exclusive) on
  Windows. Mutual exclusion is preserved on both; only read concurrency drops
  on Windows. (This helper is written to be lift-and-shift into upstream lpm.)
- **Watchdog process-tree kill** (`agent_runner/platform.py`): `kill_tree` is
  abstracted behind a `Platform` interface — POSIX uses process groups
  (`setsid` + `killpg`); the Windows implementation is a stub (extension point).
- **landlock**: Linux-only LSM; silently skipped on other platforms.

## Self-containment

This project has **zero coupling** to the parent bash `agent-runner` repo:
- No `source`/import of `runner.sh`, `runner.py`, or `backends/*.sh`.
- lpm is vendored as a source copy at `agent_runner/_vendor/llm_provider_manager/`
  (stdlib-only). It evolves with this project; upstream lpm changes are synced
  by re-copying. `$LPM_SRC` can point elsewhere for development.

## Tests

```bash
pip install pytest       # only dev dependency
cd agent-runner-py
python -m pytest -q
```

- `test_backends_jq_equiv.py` — Python jsonl parsing is byte-equivalent to the
  bash `jq` filters (cross-checked against the parent repo's bash backends when
  present; skips otherwise).
- `test_engine.py` — watchdog early-exit, reactive retry, continue-vs-redo
  branches, exit codes (mock backend, no real agent).
- `test_platform.py` — the POSIX process-group spawn + tree-kill contract.
- `test_cli.py` — `python -m agent_runner` dispatch, arg ordering, exit-code
  mapping.

## Layout

```
agent-runner/
├── agent-runner.sh              # bash → `python -m agent_runner` wrapper (process mode)
├── pyproject.toml               # package metadata (stdlib-only; pip -e . works)
├── README.md
├── agent_runner/                # the importable package
│   ├── __init__.py              # public API (library mode)
│   ├── __main__.py              # `python -m agent_runner` (process mode)
│   ├── engine.py                # orchestration
│   ├── platform.py              # cross-platform process-tree abstraction
│   ├── keypool.py / landlock.py
│   └── backends/{claude_code,opencode,_jsonl}.py
├── llm-provider-manager/        # vendored lpm (git subtree) — keypool/providers/agents
└── tests/
```

Progress tracking (the orthogonal `progress_read`/`progress_write`/
`progress_iterations` state machine) is maintained separately; this project
does not provide it.
```
