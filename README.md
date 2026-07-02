# agent-runner

用于可靠地运行 AI agent CLI 的 Bash 库——具备 watchdog 超时检测、带模型降级能力的重试、进度跟踪与中断恢复等特性。

面向需要反复调用 agent（如 Claude Code）处理多轮迭代或多文档的批量流水线场景。

## 文件

| 文件 | 用途 |
|------|---------|
| `runner.sh` | 通用 agent 调用：`agent_with_retry`、`agent_once_session_resume`、`agent_once`。调用由当前 backend 定义的 11 个 `agent_backend_*` 操作。 |
| `runner.py` | 薄适配层：将 key-pool 操作（init/rotate/disable/classify/react）转发给 vendored 的 lpm（`llm_provider_manager.keypool`）。 |
| `backends/<name>.sh` | 针对特定 agent CLI 实现 11 操作接口的 backend（如 `claude-code.sh`、`opencode.sh`）。 |
| `progress.sh` | 迭代进度：`progress_read`、`progress_write`、`progress_iterations`。 |
| `llm-provider-manager/` | vendored 的 [llm-provider-manager](llm-provider-manager/)（git subtree）——provider/key 配置、key-pool 轮换、错误分类。 |

## 快速开始

```bash
source agent-runner/progress.sh
source agent-runner/runner.sh

OUTPUT_DIR="$PWD/output"
mkdir -p "$OUTPUT_DIR"

agent_with_retry "Summarize this document" "summary"
progress_iterations my-task 10 my_callback
```

## 前置条件

- 调用任何函数前必须已设置 `$OUTPUT_DIR`
- `$PATH` 中须有 `jq`
- `$PATH` 中须有当前 backend 的 CLI（见下文「后端」）

## 环境变量

| 变量 | 默认值 | 说明 |
|----------|---------|-------------|
| `OUTPUT_DIR` | （必填） | JSONL 日志、错误日志与进度状态的存放目录 |
| `AGENT_BACKEND` | `claude-code` | backend 名；据此 source `backends/<name>.sh` |
| `SANDBOX` | （未设置） | 设为 `"1"` 时使用 `--dangerously-skip-permissions` |
| `AGENT_STALL_TIMEOUT` | `300` | 无输出多少秒后杀死卡住的进程 |
| `AGENT_TIMEOUT` | `0` | 总硬超时（秒）；`0` = 无限制 |
| `KEY_POOL_CONFIG` | `<workspace>/providers.jsonc` | lpm key-pool 配置（providers.jsonc）的路径 |
| `LPM_SRC` | vendored `llm-provider-manager/src` | 覆盖 adapter 所 import 的 lpm 副本（如指向一个 dev checkout） |

## 后端 (Backends)

runner 与具体 agent 无关；所有 agent 相关行为都位于 `backends/<name>.sh`。当前 backend 由 `$AGENT_BACKEND` 选定。

### claude-code（默认）

- 二进制：`claude`
- 前置：`PRIMARY_MODEL`、`DOWNGRADE_MODEL` 环境变量（key pool 导出当前 provider 的 models；shell 值作为 backend 默认）
- API key：`ANTHROPIC_AUTH_TOKEN` 环境变量（由 key pool 设置）；base_url 经由 `ANTHROPIC_BASE_URL`（由 key pool 取自 provider 的 `base_url`）

### opencode

- 二进制：`opencode`（v1.17+）
- 前置：
  - `PRIMARY_MODEL`、`DOWNGRADE_MODEL` 环境变量（须为 `provider/model` 形式，如 `bailian/glm-5.2`；key pool 导出当前 provider 的 models）
  - 在 `~/.config/opencode/opencode.json` 中配置自定义 providers（endpoint URL + protocol 位于此，按 model 的 provider 前缀索引；key pool 中各 provider 的 `base_url` 对 opencode 不起作用）
- API key：在 `opencode.json` 中配置的环境变量（默认 `Z_AI_API_KEY`）；key pool 将当前 key 导出到 `agent_backend_api_key_env_var`。

### 错误处理码 (Error handling codes)

错误码以 `[NNN]`（3 位，HTTP 状态码——由 opencode 产生）或 `[NNNN]`（4 位，上游特定——由 claude-code 的 zhipu 路径产生）形式匹配。每个 provider 模块（位于 vendored lpm 的 `providers/`）自带一份默认错误处理表；`providers.jsonc` 中可选的 `errorHandling` 可按部署覆盖：

- claude-code + zhipu 使用上游码（如 `"1305"`、`"1308"`）——默认值位于 vendored 的 `llm-provider-manager/src/llm_provider_manager/providers/zhipu.py`。
- opencode + bailian 无上游码；runner 回退到 HTTP 状态码（如 `"401"`、`"429"`）。

## Key pool 配置（`providers.jsonc`）

Provider/key 配置即 [llm-provider-manager](llm-provider-manager/) 的 `providers.jsonc`（vendored）；完整 schema 见其 [README](llm-provider-manager/README.md) / [ARCHITECTURE.md](llm-provider-manager/ARCHITECTURE.md)。示例：

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

要点：`baseURLs` 是 protocol→url 的映射（keypool 按当前 agent 选取）；`models[0]`/`[1]` 为 primary/downgrade（可由 `primaryModel`/`downgradeModel` 覆盖）；`errorHandling` 覆盖各 provider 模块内置的默认值。整个池把所有 provider 的 key 作为一张扁平池轮换；轮换进入另一 provider 时重新套用该 provider 的 base_url + models（跨 provider failover）。API key 的环境变量名仍由 backend 声明。

## 公开 API

> 仅列对外公开的入口与工具函数。带 `_` 前缀的内部函数（`_agent_once_with_watchdog` / `_agent_once_with_check` / `_agent_once_with_disable` 等）及其分层包装关系见 [ARCHITECTURE.md §4](ARCHITECTURE.md)。

### runner.sh

**`agent_once <prompt> <log_name> [extra_args...]`**

运行一次 agent CLI。写入 `$OUTPUT_DIR/<log_name>.jsonl`（事件日志）、`$OUTPUT_DIR/<log_name>.err`（stderr），并将结果文本打印到 stdout。

**`check_agent_result <log_name>`**

检查一次已完成的运行是否产生了成功结果。成功返回 0，出错返回 1。

**`agent_with_retry <prompt> <log_name> [extra_args...]`**

带 watchdog 运行；失败时进行反应式重试——每次失败后，lpm 的 `react` 对该次错误做分类并返回一步恢复策略（逗号连接的原子：`disable`/`rotate`/`downgrade`，或 `stop`），由循环在下次尝试前应用。下一次失败会重新分类，因此换上的新 key 若遇到不同的错误码会得到相称的恢复策略。任一次成功即返回 0，全部失败返回 1。

**`agent_once_session_resume <prompt> <log_name> <session_id> [extra_args...]`**

运行一次，若 backend 支持则续接已有 session（否则回退为以 `$prompt` 开新 session）。作为单次入口（无外层重试循环），当错误需要时会自行 disable 当前 key。成功返回 0，可重试失败返回 1，key 耗尽且未配置 key pool 时返回 2。

### progress.sh

**`progress_read <task_id>`**

读取某任务最后完成的迭代号。无状态时返回 `0`。

**`progress_write <task_id> <iteration>`**

持久化已完成的迭代号。状态存于 `$OUTPUT_DIR/state/<task_id>`。

**`progress_iterations <task_id> <max_iterations> <callback> [callback_args...]`**

对 1..max 各迭代运行回调，跳过已完成的。回调收到 `<iteration> <max_iterations> [args...]`。返回值：0 = 全部完成，1 = 回调失败，2 = 已完成（被跳过）。
