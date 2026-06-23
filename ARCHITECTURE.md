# agent-runner 架构设计

`agent-runner` 是一个 **agent-agnostic / provider-agnostic** 的可靠执行层。它把"调用哪个 coding agent（claude-code / opencode / …）"和"背后是哪个 LLM provider"都做成可插拔的后端，而通用的编排逻辑（执行、看门狗、密钥池轮换、错误分类、重试、会话复用、进度跟踪）不绑定任何具体 agent 或 provider。

## 1. 架构总览

`agent-runner` 提供**编排层**（`runner.sh`）与三个**策略模块**（Agent backend / Key pool / Provider）。编排层对外暴露一组通用 API（`agent_with_retry`、`agent_once`、`agent_once_session_resume` 等），供上层任务调用。各模块的 `loop.sh`（doc-parse、baseline-vote-mapping 等）是 agent-runner 的**调用方**，不属于 agent-runner——下图用 `═══` 边界线标出范围：

```
┌──────────────────────────────────────────────────────────────┐
│  Task  (the consumer; NOT part of agent-runner)              │
│  module loop.sh: doc-parse, baseline-vote-mapping, ...       │
└────────────────────────────┬─────────────────────────────────┘
                             │ calls
                             ▼
═════════════════════════════════════════════════════════════════
 agent-runner
═════════════════════════════════════════════════════════════════
┌──────────────────────────────────────────────────────────────┐
│  Orchestration   (runner.sh)                                 │
│  public API: agent_with_retry, agent_once, ...               │
│  drives the three strategies below                           │
└──────┬──────────────────────┬──────────────────────┬─────────┘
       │ execute              │ classify errors      │ auth
       │                      │                      │ (pre-run)
┌──────▼──────────┐  ┌────────▼─────────┐  ┌─────────▼─────────┐
│ Agent backend   │  │ Provider         │  │ Key pool           │
│ backends/*.sh   │  │ providers/*.py   │  │ runner.py          │
│                 │  │                  │  │                    │
│ implements the  │  │ error payload    │  │ key rotation;      │
│ 10-op interface │  │ -> recovery      │  │ resolve the active │
│                 │  │ action           │  │ provider           │
└─────────────────┘  └──────────────────┘  └────────────────────┘
═════════════════════════════════════════════════════════════════
```

三个策略各司其职（均在 agent-runner 内）：

- **Agent backend**（`backends/<name>.sh`）：实现 10-op 接口，负责具体 coding agent 的执行与日志解析。由 `$AGENT_BACKEND` 选择（默认 `claude-code`）。
- **Key pool**（`runner.py`）：管理 API 密钥的轮换与禁用，并解析当前使用的是哪个 provider。
- **Provider**（`providers/<name>.py`）：把错误 payload 解读为恢复动作（错误码 → 动作），按 provider 区分。

三个策略互不直接调用，都由编排层统一调度；策略之间没有直连，所有数据都经编排层流转。两条主要通路：

- **错误解读**（运行失败时）：Agent backend 产出错误 payload → 编排层 → 按密钥池解析出的 provider 路由到对应 Provider 模块 → 输出恢复动作。
- **认证**（运行前）：密钥池取出当前 key → 编排层导出到 Agent backend 声明的认证环境变量（`agent_backend_auth_env_var`）。

## 2. 文件布局

```
utils/agent-runner/
├── runner.sh            通用编排（纯 bash，不含任何 agent 专有逻辑）
├── runner.py            密钥池 + 错误分类分发 + 重试计划（Python，fcntl 并发）
├── progress.sh          断点恢复 / 迭代进度
├── backends/
│   ├── claude-code.sh   Claude Code 后端（10-op 接口的实现）
│   └── opencode.sh      OpenCode 后端
├── providers/
│   ├── __init__.py      公共入口 classify() + 注册表 + payload 解析
│   ├── base.py          初步解析 extract_signals()
│   ├── default.py       通用 code→action 映射（兜底）
│   └── zhipu.py         zhipu provider（当前复用 default，留作特化点）
├── example-api-keys.json 密钥池配置示例
└── README.md / ARCHITECTURE.md
```

`utils/common.sh`（在**父仓库**中）负责拼装：依次 source `progress.sh` → `runner.sh` → `backends/${AGENT_BACKEND:-claude-code}.sh`。模块的 `loop.sh` 只要 `source common.sh` 即获得全部能力。

## 3. Agent 后端接口（10-op 契约）

每个后端是一个 bash 文件，定义以下 10 个函数。通用编排 `runner.sh` 只调用这些函数，从不直接引用具体 agent 的二进制或日志格式。`$AGENT_BACKEND` 决定 source 哪个后端文件。

| op | 通用层用途 | claude-code 实现 | opencode 实现 |
|---|---|---|---|
| `agent_backend_invoke <prompt> <prefix> [argv…]` | 跑一步；写 `$prefix.jsonl`，stdout 打印结果文本 | `claude -p … --output-format stream-json \| tee \| jq .result`（经 `_landlock_wrap`） | `opencode run … --format json \| tee \| jq .text` |
| `agent_backend_is_complete <prefix>` | 看门狗早退（agent 写完结果即退出，不等进程结束） | grep `"type":"result"` | grep `"type":"(step_finish\|error)"` |
| `agent_backend_result_ok <prefix>` | 判定成功/失败 | `type==result` 且 `is_error` 为假 | 有 `step_finish` 且无 `error` 事件 |
| `agent_backend_result_text <prefix>` | 取错误 payload（JSON） | `{"message": .result}` | `{"message", "code"(responseBody.code), "status"(statusCode)}` |
| `agent_backend_session_id <prefix>` | 读 agent 生成的会话 id（用于续接） | `select(.session_id!=null)` | `select(.sessionID!=null)` |
| `agent_backend_perm_args` | 权限 flag 片段 | `--dangerously-skip-permissions` / `--permission-mode acceptEdits` | 沙箱下 `--dangerously-skip-permissions`，否则空（交由 opencode.json 配置） |
| `agent_backend_model_args <tier>` | 模型 flag 片段；`tier` = `primary`/`downgrade`/裸 id | `--model $CLAUDE_MODEL` / `$DOWNGRADE_MODEL` / `$1` | `--model $OPENCODE_MODEL` / `$OPENCODE_DOWNGRADE_MODEL` / `$1` |
| `agent_backend_resume_args <id>` | 续接 flag（**空输出 = 不支持续接 → 退化为全新会话**） | `--resume <id>` | `-s <id>` |
| `agent_backend_fork_args` | 分叉会话 flag | `--fork-session` | `--fork` |
| `agent_backend_auth_env_var` | 该 agent 读取 API key 的环境变量名（密钥池导出到此） | `ANTHROPIC_AUTH_TOKEN` | `${OPENCODE_AUTH_ENV_VAR:-Z_AI_API_KEY}` |

> **关键约束**：后端不读 `$OUTPUT_DIR`——日志路径以 `<prefix>` 参数传入（后端自行追加 `.jsonl`/`.err`）。`<prefix>` = `$OUTPUT_DIR/$log_name`，由通用层构造。

后端在 source 时设置自己的默认值（如 `CLAUDE_MODEL`、`OPENCODE_MODEL`），这样具体模型 id 也留在后端内、不污染通用代码。

## 4. 通用编排（runner.sh）

函数自底向上分层包装：

```
agent_with_retry            ← 模块入口：首次尝试 + 失败重试
  └─ agent_once_with_disable    看门狗执行 + 结果检查 + 额度耗尽处理
       └─ _agent_once_with_watchdog  后台执行 + 早退/超时/无进展监控
            └─ agent_once            组装 argv 并调 agent_backend_invoke
```

| 函数 | 职责 |
|---|---|
| `agent_once <prompt> <log_name> [extra…]` | 组装 argv：`agent_backend_perm_args` + （调用方未给 `--model` 时注入 `agent_backend_model_args primary`）+ 调用方透传参数 → `agent_backend_invoke`。这保证**只有一个 `--model`**（调用方覆盖 primary）。 |
| `_agent_once_with_watchdog` | 后台跑 `agent_once`；轮询：`agent_backend_is_complete` 命中则早退；否则检测总超时（`CLAUDE_TIMEOUT`）与无进展超时（`CLAUDE_STALL_TIMEOUT`，看 jsonl 文件增长）；超时则按"先杀子进程再杀父 shell"的顺序清理。 |
| `agent_once_with_disable` | 跑 watchdog + `check_agent_result`（=`agent_backend_result_ok`）；失败则 `classify_agent_error`，若返回的 `disable` 标志为真则 `key_pool_disable`；返回码 0/1/2（2=额度耗尽且无密钥池，放弃）。 |
| `agent_once_session_resume <prompt> <log_name> <sid> [extra…]` | 续接会话：取 `agent_backend_resume_args <sid>`；非空则带续接 flag 跑 `agent_once_with_disable`，为空则**退化为全新会话**（用原 prompt 重跑）。模块多步流（doc-parse 的 TOC→精修）用它。 |
| `agent_with_retry <prompt> <log_name> [extra…]` | 顶层入口。① `key_pool_init` 导出当前密钥；② 首次 `agent_once_with_disable`（primary 模型）；③ 失败则 `classify_agent_error` 取 action；④ `runner.py retry-plan` 生成 `(tier, count)` 计划；⑤ 逐轮：`key_pool_rotate` + 按 tier 选模型 + （若有 session_id）`agent_once_session_resume "继续"`，否则重试。 |

### 4.1 密钥池（`runner.sh` 包装 + `runner.py` 核心）

`runner.sh` 的 `key_pool_*` 是对 `runner.py` 子命令的薄包装：取到 key 后交给 `_kp_export_key`。

`_kp_export_key` 的关键：
- 从 `agent_backend_auth_env_var` 取目标环境变量名；
- 若与上一次导出的变量名不同，先 `unset` 旧的（防止跨 agent/env-var 轮换时旧值泄漏到下一次调用）；
- `export <env_var>=<key>`。

→ 因此切换 agent（claude↔opencode）或 provider 时，密钥总能导到正确的环境变量。

## 5. 密钥池核心（runner.py）

- **配置** `api-keys.json`：`providers.{name}.{keys, error_handling}`（认证环境变量不在此处，由 agent 后端声明）。
- **状态** `$DATA_DIR/key-pool-state.json`：`{current_index, disabled{idx: 过期时间}, success_count}`。
- **并发**：`fcntl.flock`——读用 `LOCK_SH`，读-改-写用 `LOCK_EX`。`json.dump` 后紧跟 `f.truncate()`，确保 write 落在锁临界区内（见 `_modify_state` 注释）。
- **禁用 TTL**：`DISABLE_TTL = 5h`；`disabled` 在每次 rotate/disable 时清理过期项。

`KeyPool` 主要方法：

| 方法 | 作用 |
|---|---|
| `init` | 初始化状态文件，返回当前 key |
| `rotate` | 原子推进到下一个未禁用的 key（`LOCK_EX`） |
| `on_success` | 成功计数 +1，达 `rotate_every` 阈值则主动轮换 |
| `disable(key)` | 按值禁用某 key，设 TTL |
| `size` / `available_size` | 总数 / 未禁用数 |
| `_provider_for_current()` | 当前 key 索引 → 所属 provider 名（错误分类的 dispatch key） |

子命令：`init / rotate / on-success / disable / size / available-size / classify / retry-plan`。

## 6. 错误分类（providers/ + runner.py）

**契约**：agent 用 `agent_backend_result_text` 产出一个 **JSON 错误 payload** `{message, code, status}`（结构化地从日志里取，不做语义解读）。provider 层负责解读。

**流程**：

```
agent 失败
  → agent_backend_result_text(prefix)          产出 {message, code?, status?}（JSON）
  → runner.sh classify_agent_error            管道给 runner.py classify --text -
  → runner.py classify_error(payload, cfg, state)
        provider = KeyPool._provider_for_current()       ← 当前密钥的 provider
        handling = config[providers][provider][error_handling]
        return providers.classify(provider, payload, handling)
  → providers.classify(provider, payload, handling)
        payload = _parse_payload(payload_text)            容错（空/非JSON → {message:...}）
        signals = base.extract_signals(payload)           ← 初步解析
        module  = REGISTRY.get(provider, default)
        return module.classify(signals, handling)         → "action:disable"
```

- **`base.extract_signals`**（初步解析，所有 provider 共享）：payload 有 `code` 就用它；否则在 `message` 上正则匹配 `[NNN]`（HTTP 状态）或 `[NNNN]`（上游码，如 zhipu 的 1305）。这样 claude（错误内联在 `.result` 文本里）与 opencode（结构化字段）都能被统一解析。
- **provider 模块 `classify(signals, error_handling)`**：用 `error_handling` 把码映射成动作；`disable = "true" if "rotate" in action`。
- **动作**：`rotate_key` / `downgrade` / `rotate_then_downgrade`。
- **`REGISTRY`**（`providers/__init__.py`）：provider 名 → 模块。未注册的 provider 走 `default`。`zhipu` 与 `zhipuai-coding-plan` 同属一家，都指向 `zhipu` 模块。
- `error_handling` 留在 `api-keys.json`（可配置），提取逻辑写在模块里。

> **agent 无关性**：同一 provider 配置下，claude 风格 payload（码在 message）与 opencode 风格 payload（码在字段）都映射到同一动作——因为解读在 provider 层，与 agent 无关。

## 7. 进度跟踪（progress.sh）

`progress_read <task_id>` / `progress_write <task_id> <iter>` / `progress_iterations <task_id> <max> <cb>`。

- 状态存 `$OUTPUT_DIR/state/<task_id>`。
- `progress_iterations` 跳过已完成轮次、支持断点恢复；模块（如 doc-parse）用它做迭代级恢复。
- 与密钥池、agent 调用正交，纯任务级状态。

## 8. 配置与环境变量

**`api-keys.json`**（密钥池配置，见 `example-api-keys.json`）：
```json
{
  "rotate_every": 5,
  "providers": {
    "zhipu": {
      "keys": ["...", "..."],
      "error_handling": { "1305": "downgrade", "1308": "rotate_key", "_default": "rotate_then_downgrade" }
    }
  }
}
```

**环境变量**：

| 变量 | 作用 | 默认 |
|---|---|---|
| `AGENT_BACKEND` | 选择 agent 后端 | `claude-code` |
| `DATA_DIR` | 运行实例根（必填，由 common.sh 校验） | — |
| `KEY_POOL_CONFIG` | api-keys.json 路径 | `<agent-runner>/../../api-keys.json` |
| `SANDBOX` | `1` 跳过权限提示 | — |
| `CLAUDE_STALL_TIMEOUT` / `CLAUDE_TIMEOUT` | 看门狗无进展/总超时（秒） | 300 / 0 |
| `LANDLOCK_CONFIG` / `LANDLOCK_RUNNER` | 用 landlock 包裹 agent 命令 | — |
| `CLAUDE_MODEL` / `DOWNGRADE_MODEL` | claude-code 的 primary / 降级模型 id | glm-5-turbo / glm-4.7 |
| `OPENCODE_MODEL` / `OPENCODE_DOWNGRADE_MODEL` | opencode 的模型 id | bailian/glm-5.2 / bailian/glm-5.1 |
| `OPENCODE_AUTH_ENV_VAR` | opencode 读 key 的环境变量名 | `Z_AI_API_KEY` |

模型 id / 认证变量等"agent 专有"默认值都定义在对应后端文件里（source 时生效），通用代码不感知。

## 9. 关键数据流

### 9.1 一次成功调用
```
module loop.sh
  → agent_with_retry(prompt, log_name)
       key_pool_init ──▶ export <auth_env_var>=<key>
       agent_once_with_disable
         _agent_once_with_watchdog
           agent_once ──▶ agent_backend_invoke ──▶ claude/opencode ──▶ $prefix.jsonl
           (is_complete 早退 / 超时监控)
         check_agent_result = agent_backend_result_ok ──▶ 0
       key_pool_on_success ──▶ 成功计数（可能轮换）
       return 0
```

### 9.2 失败 → 重试
```
agent_once_with_disable 返回失败
  classify_agent_error ──▶ runner.py classify ──▶ providers.classify ──▶ "rotate_then_downgrade:true"
  (disable=true) key_pool_disable
  runner.py retry-plan(action) ──▶ [("primary",n),("downgrade",m)]
  for each (tier, count):
      key_pool_rotate
      session_id = agent_backend_session_id(log)          # 从首次尝试的日志取
      agent_once_session_resume("继续", name, sid, agent_backend_model_args(tier))
        └─ 或全新会话（后端不支持 resume 时）
      成功则 return 0
```

### 9.3 多步会话复用（模块级，如 doc-parse）
```
step1: agent_with_retry(toc_prompt, "toc-…")              # agent 创建会话，id 入日志
       sid = agent_backend_session_id("toc-…")
step2: agent_once_session_resume(refine_prompt, "refine-…", sid)
         └─ agent_backend_resume_args(sid) → "--resume <sid>"（或 opencode 的 -s）
         └─ 退化为全新会话（若后端不支持续接）
```

### 9.4 跨进程会话（baseline-vote-mapping）
阶段 1 在 xargs 子进程建会话、把 id 落 TSV；阶段 2 在另一批 xargs 子进程读 TSV 取 id、用 `agent_once_session_resume(... $(agent_backend_fork_args))` 分叉投票。会话状态靠模块自己的 TSV 传递（与 agent-runner 接口无关）。

## 10. 扩展点

### 10.1 新增一个 agent
新建 `backends/<name>.sh`，实现 10 个 `agent_backend_*` 函数（参考 opencode.sh），并在 `common.sh` 通过 `AGENT_BACKEND=<name>` 选用。无需改 `runner.sh` / `runner.py` / `providers/`。

### 10.2 新增一个 LLM provider
1. 在 `providers/` 加 `<name>.py`，实现 `classify(signals, error_handling) -> "action:disable"`（可直接 `from .default import classify` 复用通用逻辑）。
2. 在 `providers/__init__.py` 的 `REGISTRY` 注册 `"<name>": <module>`。
3. 在 `api-keys.json` 加该 provider 的 `keys` + `error_handling`。
agent 不需要任何改动——只要它们能把该 provider 的错误码结构化进 payload。

## 附：不变量与约定

- **通用层不出现 agent 专有符号**：`runner.sh` / `runner.py` 中不直接出现 `claude`、`opencode`、`--output-format`、`ANTHROPIC_AUTH_TOKEN` 等（grep 可验）。
- **`$OUTPUT_DIR` 不进后端**：后端只接收 `<prefix>`。
- **会话续接可降级**：后端不实现 resume 时，`agent_once_session_resume` 退化为全新会话而非报错。
- **退出码约定**：`agent_once_with_disable` → `0` 成功 / `1` 可重试 / `2` 额度耗尽且无密钥池（放弃）。
- **管道退出状态不被依赖**：`agent_once` 的 pipeline 返回的是 `jq` 的状态（无 `set -o pipefail`）；成功与否一律由 `agent_backend_result_ok` 读日志判定。
