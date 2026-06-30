# agent-runner 架构设计

`agent-runner` 是一个 **agent-agnostic** 的可靠执行层（执行、看门狗、重试、会话复用、进度跟踪），通用编排逻辑不绑定任何具体 agent。**provider / key / 密钥池 / 错误分类**不在 agent-runner 内——由 vendoring 进来的 [llm-provider-manager](llm-provider-manager/) (lpm) 提供（详见 §1 与 §5）。

## 1. 架构总览

agent-runner 只保留**编排层**（`runner.sh`）与 **Agent backend**（`backends/*.sh`）。**密钥池 / provider / 错误分类 / 协议选择**由 vendoring 进来的 lpm 提供，`runner.py` 是把它们接起来的薄适配器。编排层对外暴露一组通用 API（`agent_with_retry`、`agent_once`、`agent_once_session_resume` 等），供上层任务调用。各模块的 `loop.sh`（doc-parse、baseline-vote-mapping 等）是 agent-runner 的**调用方**，不属于 agent-runner——下图用 `═══` 边界线标出范围：

```
┌────────────────────────────────────────────────────────────────┐
│ Task (consumer; not part of agent-runner)                      │
│ loop.sh: doc-parse, baseline-vote-mapping, ...                 │
└───────────────────────────────┬────────────────────────────────┘
                                │ calls
                                ▼
══════════════════════════════════════════════════════════════════
 agent-runner
══════════════════════════════════════════════════════════════════
┌────────────────────────────────────────────────────────────────┐
│ Orchestration (runner.sh)                                      │
│ agent_with_retry / agent_once / agent_once_session_resume      │
└─────────────┬───────────────────────────────┬──────────────────┘
              │ execute                       │ keypool ops
              ▼                               ▼
 ┌────────────────────────┐  ┌────────────────────────────────┐
 │ Agent backend          │  │ runner.py  (thin adapter)      │
 │ backends/*.sh          │  │  map AGENT_BACKEND -> agent    │
 │ 11-op exec interface:  │  │  inject --agent; forward to    │
 │  invoke / log parse /  │  │  keypool.dispatch              │
 │  resume / ...          │  └────────────────────────────────┘
 │ declares env-var names:│                   │ imports
 │  api_key_env_var       │                   ▼
 │  base_url_env_var      │  ┌────────────────────────────────┐
 └────────────────────────┘  │ llm-provider-manager/          │
                             │ (vendored; git subtree)        │
                             │  keypool.py  KeyPool+dispatch  │
                             │  providers/  error classify    │
                             │  agents/     base_url/proto    │
                             └────────────────────────────────┘
══════════════════════════════════════════════════════════════════
```

职责划分：

- **Agent backend**（`backends/<name>.sh`，在 agent-runner 内）：实现 11-op 执行接口（调二进制、解析日志、续接、env-var 名声明）。由 `$AGENT_BACKEND` 选择（默认 `claude-code`）。
- **密钥池 / provider / 错误分类 / 协议选择**（vendored `llm-provider-manager/`）：lpm 的 `keypool.py` 管轮转/禁用/分类，`providers/` 解读错误码，`agents/` 按 active agent 选 base_url 协议。`runner.py` 是适配器：找到 vendored 的 lpm、把 `$AGENT_BACKEND` 映射成 lpm agent id、注入 `--agent`、转发到 `keypool.dispatch`。

两条主要通路：

- **错误解读**（运行失败时）：Agent backend 产出错误 payload → 编排层 → `runner.py` 适配器 → lpm `keypool.classify`（按当前 provider 路由到对应 provider 模块）→ 输出恢复动作。
- **认证/配置**（运行前）：lpm keypool 取出当前 key + 其 provider 的 `base_url`/模型 → 产出一行 JSON → 编排层 `_kp_apply` 导出到 Agent backend 声明的环境变量（API key：`agent_backend_api_key_env_var`、base_url：`agent_backend_base_url_env_var`、模型 `PRIMARY_MODEL`/`DOWNGRADE_MODEL`）。

## 2. 文件布局

```
utils/agent-runner/
├── runner.sh              通用编排（纯 bash，不含任何 agent 专有逻辑）
├── runner.py              薄适配器：转发密钥池操作到 vendored lpm（keypool.dispatch）
├── progress.sh            断点恢复 / 迭代进度
├── backends/
│   ├── claude-code.sh     Claude Code 后端（11-op 接口的实现）
│   └── opencode.sh        OpenCode 后端
└── llm-provider-manager/  vendored lpm（git subtree）：keypool.py / providers/ / agents/ / schema.py …
```

> provider/key 配置、密钥池轮换、错误分类都不在 agent-runner 里改——它们属于 vendored 的 `llm-provider-manager/`（一个独立项目，详见其 `ARCHITECTURE.md`）。同步上游 lpm：`git subtree pull --prefix=llm-provider-manager https://gitee.com/raverstern/llm-provider-manager.git master --squash`。

`utils/common.sh`（在**父仓库**中）负责拼装：依次 source `progress.sh` → `runner.sh` → `backends/${AGENT_BACKEND:-claude-code}.sh`。模块的 `loop.sh` 只要 `source common.sh` 即获得全部能力。

## 3. Agent 后端接口（11-op 契约）

每个后端是一个 bash 文件，定义以下 11 个函数。通用编排 `runner.sh` 只调用这些函数，从不直接引用具体 agent 的二进制或日志格式。`$AGENT_BACKEND` 决定 source 哪个后端文件。

| op | 通用层用途 | claude-code 实现 | opencode 实现 |
|---|---|---|---|
| `agent_backend_invoke <prompt> <prefix> [argv…]` | 跑一步；写 `$prefix.jsonl`，stdout 打印结果文本 | `claude -p … --output-format stream-json \| tee \| jq .result`（经 `_landlock_wrap`） | `opencode run … --format json \| tee \| jq .text` |
| `agent_backend_is_complete <prefix>` | 看门狗早退（agent 写完结果即退出，不等进程结束） | grep `"type":"result"` | grep `"type":"(step_finish\|error)"` |
| `agent_backend_result_ok <prefix>` | 判定成功/失败 | `type==result` 且 `is_error` 为假 | 有 `step_finish` 且无 `error` 事件 |
| `agent_backend_result_text <prefix>` | 取错误 payload（JSON） | `{"message": .result}` | `{"message", "code"(responseBody.code), "status"(statusCode)}` |
| `agent_backend_session_id <prefix>` | 读 agent 生成的会话 id（用于续接） | `select(.session_id!=null)` | `select(.sessionID!=null)` |
| `agent_backend_perm_args` | 权限 flag 片段 | `--dangerously-skip-permissions` / `--permission-mode acceptEdits` | 沙箱下 `--dangerously-skip-permissions`，否则空（交由 opencode.json 配置） |
| `agent_backend_model_args <tier>` | 模型 flag 片段；`tier` = `primary`/`downgrade`/裸 id。读 agent 无关的 `$PRIMARY_MODEL` / `$DOWNGRADE_MODEL`（密钥池按 provider 导出） | `--model $PRIMARY_MODEL` / `$DOWNGRADE_MODEL` / `$1` | `--model $PRIMARY_MODEL` / `$DOWNGRADE_MODEL` / `$1` |
| `agent_backend_resume_args <id>` | 续接 flag（**空输出 = 不支持续接 → 退化为全新会话**） | `--resume <id>` | `-s <id>` |
| `agent_backend_fork_args` | 分叉会话 flag | `--fork-session` | `--fork` |
| `agent_backend_api_key_env_var` | 该 agent 读取 API key 的环境变量名（密钥池导出 key 到此） | `ANTHROPIC_AUTH_TOKEN` | `${OPENCODE_AUTH_ENV_VAR:-Z_AI_API_KEY}` |
| `agent_backend_base_url_env_var` | 该 agent 读取 base_url 的环境变量名（密钥池导出 base_url 到此）。**空输出 = 该 backend 不从密钥池读 base_url**（opencode 按 model 前缀在 opencode.json 路由） | `ANTHROPIC_BASE_URL` | _(空)_ |

> **关键约束**：后端不读 `$OUTPUT_DIR`——日志路径以 `<prefix>` 参数传入（后端自行追加 `.jsonl`/`.err`）。`<prefix>` = `$OUTPUT_DIR/$log_name`，由通用层构造。

后端在 source 时设置自己的默认值（如 `PRIMARY_MODEL`、`DOWNGRADE_MODEL`，agent 无关；密钥池可按 provider 覆盖），这样具体模型 id 默认值也留在后端内、不污染通用代码。

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
| `_agent_once_with_watchdog` | 后台跑 `agent_once`；轮询：`agent_backend_is_complete` 命中则早退；否则检测总超时（`AGENT_TIMEOUT`）与无进展超时（`AGENT_STALL_TIMEOUT`，看 jsonl 文件增长）；超时则按"先杀子进程再杀父 shell"的顺序清理。 |
| `agent_once_with_disable` | 跑 watchdog + `check_agent_result`（=`agent_backend_result_ok`）；失败则 `classify_agent_error`，若返回的 `disable` 标志为真则 `key_pool_disable`；返回码 0/1/2（2=额度耗尽且无密钥池，放弃）。 |
| `agent_once_session_resume <prompt> <log_name> <sid> [extra…]` | 续接会话：取 `agent_backend_resume_args <sid>`；非空则带续接 flag 跑 `agent_once_with_disable`，为空则**退化为全新会话**（用原 prompt 重跑）。模块多步流（doc-parse 的 TOC→精修）用它。 |
| `agent_with_retry <prompt> <log_name> [extra…]` | 顶层入口。① `key_pool_init` 导出当前密钥；② 首次 `agent_once_with_disable`（primary 模型）；③ 失败则 `classify_agent_error` 取 action；④ `runner.py retry-plan` 生成 `(tier, count)` 计划；⑤ 逐轮：`key_pool_rotate` + 按 tier 选模型 + （若有 session_id）`agent_once_session_resume "继续"`，否则重试。 |

### 4.1 密钥池（`runner.sh` 包装 + `runner.py` 核心）

`runner.sh` 的 `key_pool_*` 是对 `runner.py` 子命令的薄包装：`init`/`rotate`/`on-success` 各产出一行 JSON（`{key, base_url, primary_model, downgrade_model}`），交给 `_kp_apply` 落到环境变量。

`_kp_apply` 的关键（每项仅在该 provider 给出非空值时才导出，省略则保留 backend 默认）：
- `key` → `agent_backend_api_key_env_var` 声明的API key 变量（若变量名变了，先 `unset` 旧的，防止旧值泄漏）；
- `base_url` → `agent_backend_base_url_env_var` 声明的 base_url 变量（backend 返回空则跳过）；
- `primary_model` / `downgrade_model` → agent 无关的 `PRIMARY_MODEL` / `DOWNGRADE_MODEL`。

→ 切 provider（跨 provider 轮转）时，base_url + 模型随 key 一起切换；API key 变量名由 backend 声明、与 provider 无关。

## 5. 密钥池（`runner.py` 适配器 + vendored lpm `keypool.py`）

`runner.py` 是个薄适配器（~50 行）：把 lpm 的 `src/` 加进 `sys.path`（默认 vendored 的 `llm-provider-manager/src`；`$LPM_SRC` 可覆盖；`~/.local/share/llm-provider-manager/src` 兜底），把 `$AGENT_BACKEND` 映射成 lpm agent id（`claude-code`→`claude`、`opencode`→`opencode`），注入 `--agent`，转发 `sys.argv` 到 `llm_provider_manager.keypool.dispatch`。

密钥池的**实现**（拍平、轮转、禁用、TTL、并发、分类）全在 vendored lpm 的 `keypool.py`（详见 lpm `ARCHITECTURE.md §9`）。要点：

- **配置**：lpm 的 `providers.jsonc`——`providers.{id}.{keys, baseURLs, models, errorHandling}` + `settings.{rotateEvery, disableTtlHours}`。路径由 `$KEY_POOL_CONFIG` / `$LLM_PROVIDER_CONFIG` 决定，默认 `<workspace>/providers.jsonc`。
- **状态** `$DATA_DIR/key-pool-state.json`：`{current_index, disabled{idx: 过期时间}, success_count}`——调用方提供，运行时状态留在执行上下文，不进 lpm 用户配置目录。
- **并发**：`fcntl.flock`（读 `LOCK_SH`、读-改-写 `LOCK_EX`，`json.dump` 后紧跟 `f.truncate()` 把 write 收进锁临界区）。
- **跨 provider**：拍平所有 provider 的 key 为一个池；按 active agent 过滤（跳过 `agentBlacklist` 命中的 key、跳过无匹配协议 baseURL 的 provider）；`base_url` 由 lpm 的 `Agent.base_url_for` 按协议选（claude→anthropic，opencode→openai）。
- **子命令**（`runner.py` 透传给 `keypool.dispatch`）：`init / rotate / on-success / disable / size / available-size / classify / retry-plan`。`init/rotate/on-success` 产出 JSON 行 `{key, base_url, primary_model, downgrade_model}`（空行=无 key/未轮换）。

## 6. 错误分类（vendored lpm `providers/`）

**契约**：agent 用 `agent_backend_result_text` 产出一个 **JSON 错误 payload** `{message, code, status}`（结构化地从日志取，不做语义解读）。解读在 lpm 的 provider 层。

**流程**：

```
agent 失败
  → agent_backend_result_text(prefix)             产出 {message, code?, status?}（JSON）
  → runner.sh classify_agent_error                管道给 runner.py classify --text -
  → runner.py 适配器 → lpm keypool.classify
        provider = 当前 key 所属 provider（从状态解析）
        handling = 该 provider 的 errorHandling 覆盖
        → providers.classify(provider, payload, handling)
            signals = base.extract_signals(payload)   ← [NNN]/[NNNN] 码提取
            module  = REGISTRY.get(provider, default)
            → module.classify(signals, handling)      → "action:disable"
```

provider 模块（`zhipu`/`default`/…）、`extract_signals`、`REGISTRY`、码→动作映射、`errorHandling` 覆盖语义都在 vendored lpm 的 `providers/`（详见 lpm `ARCHITECTURE.md §4`）。动作词表：`rotate_key` / `downgrade` / `rotate_then_downgrade`（`disable=true` 当动作含 `rotate`）。

> **agent 无关性**：同一 provider 配置下，claude 风格 payload（码在 message）与 opencode 风格 payload（码在字段）都映射到同一动作——解读在 provider 层，与 agent 无关。

## 7. 进度跟踪（progress.sh）

`progress_read <task_id>` / `progress_write <task_id> <iter>` / `progress_iterations <task_id> <max> <cb>`。

- 状态存 `$OUTPUT_DIR/state/<task_id>`。
- `progress_iterations` 跳过已完成轮次、支持断点恢复；模块（如 doc-parse）用它做迭代级恢复。
- 与密钥池、agent 调用正交，纯任务级状态。

## 8. 配置与环境变量

**`providers.jsonc`**（密钥池配置，lpm 格式；见 vendored `llm-provider-manager/providers.jsonc.example` 或 lpm 文档）：
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
字段（`keys` / `baseURLs` / `models` / `errorHandling` / `primaryModel` / `downgradeModel`、symmetric|asymmetric、`agentBlacklist`）由 lpm 的 schema 定义；`errorHandling` 是 provider 模块内置默认表之上的覆盖。路径由 `$KEY_POOL_CONFIG` / `$LLM_PROVIDER_CONFIG` 决定，默认 `<workspace>/providers.jsonc`。

**环境变量**：

| 变量 | 作用 | 默认 |
|---|---|---|
| `AGENT_BACKEND` | 选择 agent 后端 | `claude-code` |
| `DATA_DIR` | 运行实例根（必填，由 common.sh 校验） | — |
| `KEY_POOL_CONFIG` | providers.jsonc 路径（lpm 配置） | `<workspace>/providers.jsonc` |
| `LLM_PROVIDER_CONFIG` | 同上的 fallback（`KEY_POOL_CONFIG` 未设时） | — |
| `LPM_SRC` | 覆盖 vendored lpm（指向另一个 lpm 的 `src/`，如 dev checkout） | vendored `llm-provider-manager/src` |
| `SANDBOX` | `1` 跳过权限提示 | — |
| `AGENT_STALL_TIMEOUT` / `AGENT_TIMEOUT` | 看门狗无进展/总超时（秒） | 300 / 0 |
| `LANDLOCK_CONFIG` / `LANDLOCK_RUNNER` | 用 landlock 包裹 agent 命令 | — |
| `PRIMARY_MODEL` / `DOWNGRADE_MODEL` | agent 无关的 primary / 降级模型 id；密钥池按 provider 导出，省略则用 backend 默认 | claude-code: glm-5-turbo / glm-4.7；opencode: bailian/glm-5.2 / bailian/glm-5.1 |
| `ANTHROPIC_BASE_URL` | claude-code 的 base_url（密钥池按 provider 导出 base_url） | — |
| `OPENCODE_AUTH_ENV_VAR` | opencode 读 key 的环境变量名 | `Z_AI_API_KEY` |

模型 id 是 agent 无关的（`PRIMARY_MODEL`/`DOWNGRADE_MODEL`，密钥池可按 provider 覆盖）；其默认**值**与API key 变量**名**等定义在对应后端文件里（source 时生效），通用代码只感知 agent 无关的符号。

## 9. 关键数据流

### 9.1 一次成功调用
```
module loop.sh
  → agent_with_retry(prompt, log_name)
       key_pool_init ──▶ _kp_apply: export <auth_env_var>=<key>（+ base_url / PRIMARY_MODEL / DOWNGRADE_MODEL）
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
      key_pool_rotate ──▶ _kp_apply 重应用当前 provider 的 base_url + 模型 + key
      session_id = agent_backend_session_id(log)          # 从首次尝试的日志取
      agent_once_session_resume("继续", name, sid, agent_backend_model_args(tier))
        # 会话是客户端本地历史、与 provider 无关：跨 provider 续接也沿用旧会话 + 新 provider 配置
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
新建 `backends/<name>.sh`，实现 11 个 `agent_backend_*` 函数（参考 opencode.sh），并在 `common.sh` 通过 `AGENT_BACKEND=<name>` 选用。若它对应一个 lpm 里尚未注册的 agent，还需在 vendored lpm 的 `agents/` 注册该 agent + 在 `runner.py` 的 `_BACKEND_TO_AGENT` 加 `"<backend>"→"<lpm-agent-id>"`。否则无需改 `runner.sh` / `runner.py`。

### 10.2 新增一个 LLM provider
provider 模块（错误码→动作）属于 vendored lpm 的 `providers/`，**不在 agent-runner 里加**：在 lpm 那边加 `<name>.py` + 注册（见 lpm `ARCHITECTURE.md §6`），`git subtree pull` 同步进 agent-runner，再在 `providers.jsonc` 加该 provider 的 `keys`/`baseURLs`/`models`。agent 不需要任何改动——只要它们能把该 provider 的错误码结构化进 payload。

## 附：不变量与约定

- **通用层不出现 agent 专有符号**：`runner.sh` 的编排逻辑里不出现 `claude`/`opencode`/`--output-format`/`ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_BASE_URL` 等；这些都在 `backends/` 与 vendored lpm 的 `agents/`。`runner.py` 是适配器，仅含 `$AGENT_BACKEND` → lpm agent id 的映射表（`claude-code`→`claude` 等）这一必要 glue。模型变量 `PRIMARY_MODEL`/`DOWNGRADE_MODEL` 是 agent 无关的，故可在通用层导出。
- **`$OUTPUT_DIR` 不进后端**：后端只接收 `<prefix>`。
- **会话续接可降级**：后端不实现 resume 时，`agent_once_session_resume` 退化为全新会话而非报错。
- **退出码约定**：`agent_once_with_disable` → `0` 成功 / `1` 可重试 / `2` 额度耗尽且无密钥池（放弃）。
- **管道退出状态不被依赖**：`agent_once` 的 pipeline 返回的是 `jq` 的状态（无 `set -o pipefail`）；成功与否一律由 `agent_backend_result_ok` 读日志判定。
