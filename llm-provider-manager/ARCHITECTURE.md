# 架构设计

`llm-provider-manager` 是一个 **agent × provider 正交**的 LLM 切换工具。两条轴（agent 轴 + provider 轴）各自可插拔，通用层不硬编码任何 agent 或 provider 名。

## 1. 架构总览

```
+----------------------------------------------------------+
|  CLI (cli.py)                                            |
|  use / generate / list -- dispatch via registries        |
+--------------------------+-------------------------------+
                           |
              +------------+-------------+
              | use / generate           | list (error handling)
              v                          v
+------------------------+  +------------------------------+
|  agents/  (axis 1)     |  |  providers/  (axis 2)        |
|  Agent protocol        |  |  ProviderBackend protocol    |
|  + REGISTRY            |  |  + REGISTRY                  |
|                        |  |                              |
|  claude.py             |  |  base.py        Signals      |
|  opencode.py           |  |  default.py     fallback     |
|                        |  |  zhipu.py       builtin codes|
|  per-agent:            |  |  bailian.py     (empty)      |
|    preferred_protocols |  |  opencsitool.py (empty)      |
|    exports_for         |  |                              |
|    render_config       |  |  per-provider:               |
|                        |  |    default_error_handling    |
|                        |  |    classify                  |
|                        |  |  config errorHandling =      |
|                        |  |    override layer            |
+------------------------+  +------------------------------+
```

- **agent** 回答"怎么跟某个 coding-agent CLI 对接"：preferred_protocols（接受哪些 baseURL 协议）、exports_for（导出哪些环境变量）、render_config（渲染什么配置文件）。
- **provider** 回答"某个 LLM 服务怎么解读自己的错误"：default_error_handling（内置码→动作）、classify（解析 payload→恢复动作）。**不依赖配置文件**——provider 模块自带内置默认，`providers.jsonc` 的 `errorHandling` 是覆盖层。
- 两者互不直连，各自由注册表调度。新增 agent / provider = 加一个模块 + 注册一行，不改通用层。

## 2. 文件布局

```
src/llm_provider_manager/
  agents/                # 轴 1：agent 后端（env 契约 + 配置渲染）
    __init__.py          #   Agent 协议 + REGISTRY + get_agent/known_agent_ids
    claude.py            #   ClaudeAgent：preferred_protocols=("anthropic",)
    opencode.py          #   OpencodeAgent：preferred_protocols=("openai","anthropic")
  providers/             # 轴 2：provider 后端（错误分类）
    __init__.py          #   ProviderBackend 协议 + REGISTRY + classify/effective_error_handling
    base.py              #   Signals + extract_signals（共享 payload 预解析）
    default.py           #   DefaultProvider（兜底；_default=rotate_then_downgrade）
    zhipu.py             #   ZhipuProvider（内置 1305/1308/1310 码默认动作）
    bailian.py           #   BailianProvider（内置留空，待填充）
    opencsitool.py       #   OpencsitoolProvider（同上）
  cli.py                 # 通用编排层：use / generate / list
  config.py              # JSONC 加载 + 权限检查
  env_contract.py        # 通用 shell helper（sh_export 等）
  schema.py              # providers.jsonc 的 typed schema
  use.py                 # use 命令核心 + shell hook + active.env.sh 持久化
  keypool.py             # 运行时密钥池轮换 + 错误分类（批量消费的库入口 dispatch()）
```

## 3. 与各 agent 的对接

### Claude Code

- `settings.local.json` 从内置模板生成（env 超时 + 只读权限），不含 `ANTHROPIC_*`。
- Claude 启动时原生读取 `ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_BASE_URL`/`ANTHROPIC_DEFAULT_*_MODEL`——由 `lpm use` 导出。
- 切换 provider/key = `lpm use <P> <K>`（当前 shell 生效；新终端从 `active.env.sh` 恢复）。
- Claude 只接受 `anthropic` 协议的 baseURL。

### opencode

- `opencode.json` 从内置模板 + providers 烘焙，`baseURL`/`models`/`npm` 静态；`apiKey={env:LLM_KEY_*}`、顶层 `model={env:LLM_DEFAULT_MODEL}`。
- `lpm use --agent opencode` 导出每 provider 的 `LLM_KEY_*` + `LLM_DEFAULT_MODEL`；key 失效/blacklist 时该 env 为空，opencode 该 entry 不可用。
- opencode 默认接受 `openai` 协议，无 `openai` 时退到 `anthropic`。
- asymmetric 多 key 时 entry id 为 `<provider>-<keyid>`，默认模型形如 `bailian-account-a/glm-5.2`；单 key asymmetric 不带后缀。

## 4. Provider 后端（错误分类）

每个 provider 模块**内置** `default_error_handling`（码→动作映射），**不依赖配置文件**。`providers.jsonc` 的 `errorHandling` 是覆盖层，合并到内置默认之上。provider 在零配置时仍能正确分类。

| provider 模块 | 内置码 | 说明 |
|---|---|---|
| `default.py` | （空） | 兜底；`_default=rotate_then_downgrade` |
| `zhipu.py` | 1305/1308/1310 | GLM 上游码 |
| `bailian.py` | （空，待填充） | 当前靠配置或兜底 |
| `opencsitool.py` | （空，待填充） | 同上 |

动作词表：`rotate_key` / `downgrade` / `rotate_then_downgrade`。`classify` 返回 `"action:disable"`（`disable=true` 当动作含 `rotate`）。

## 5. Shell hook 机制

`init-shell-hook` 在 shell rc 里加一段带标记的块：

```sh
# >>> llm-provider-manager >>>
export LLM_PROVIDER_ACTIVE_ENV="~/.config/llm-provider-manager/active.env.sh"
[ -f "$LLM_PROVIDER_ACTIVE_ENV" ] || eval "$(lpm use 2>/dev/null)"
source "$LLM_PROVIDER_ACTIVE_ENV" 2>/dev/null
lpm() {
    if [ "$1" = "use" ]; then
        command lpm use "${@:2}" && source "$LLM_PROVIDER_ACTIVE_ENV"
    else
        command lpm "$@"
    fi
}
# <<< llm-provider-manager <<<
```

- **新 shell**：`source active.env.sh` 恢复上次选择；无文件时从 config `default` 初始化。
- **`lpm use`**：跑 CLI 写 `active.env.sh` + `source` 进当前 shell（立即生效 + 持久化）。
- **`lpm list`/`lpm generate` 等**：透传给 CLI。

`lpm()` 函数拦截 `use` 子命令做副作用（source），其余子命令透传给 `command lpm`（绕过函数）。与 `nvm`/`rbenv` 同模式。

## 6. 扩展点

### 新增一个 agent

1. 在 `agents/` 加 `<name>.py`，实现 `Agent` 协议（`id`/`preferred_protocols`/`base_url_for`/`is_usable`/`exports_for`/`render_config`/`default_config_path`/`config_path_env_var`）。
2. 在 `agents/__init__.py` 的 `_build_registry()` 加一行。
3. 无需改 `cli.py`/`use.py`/`schema.py`——注册表自动发现。

### 新增一个 provider

1. 在 `providers/` 加 `<name>.py`，实现 `ProviderBackend` 协议（`id`/`default_error_handling`/`classify`）。可直接继承 `DefaultProvider` 复用通用逻辑。
2. 在 `providers/__init__.py` 的 `_build_registry()` 加一行。
3. 在 `providers.jsonc` 加该 provider 的配置（`errorHandling` 可选——覆盖内置默认）。

## 7. 安全

| 文件 | 含密钥 | 处理 |
|---|---|---|
| `providers.jsonc` | ✅ | 0600 + gitignore；启动检权限过宽告警 |
| `active.env.sh` | ✅ | 0600 + gitignore；仅当前用户可读 |
| `settings.local.json` (Claude) | ❌ | 可提交/软链 git |
| `opencode.json` | ❌ | 可提交/软链 git |
| `providers.jsonc.example` | ❌ | `sk-REPLACE-ME` 占位 |
| `use` 的 stdout | ❌（仅当前 shell 进程） | 不落盘 |

`.gitignore` 已忽略 `providers.jsonc` 和 `active.env.sh`。**切勿**把真实 `providers.jsonc` / `active.env.sh` 提交。

## 8. 环境变量契约

| 变量 | 消费方 | 含义 |
|---|---|---|
| `LLM_KEY_<PROVIDER>` | opencode（symmetric） | 该 provider 当前 key |
| `LLM_KEY_<PROVIDER>_<KEYID>` | opencode（asymmetric 每 key） | 该 key 的值（blacklist 则空） |
| `LLM_DEFAULT_MODEL` | opencode | 默认模型 `<entryId>/<modelId>` |
| `ANTHROPIC_BASE_URL` | Claude Code | 当前 provider 的 anthropic baseURL |
| `ANTHROPIC_AUTH_TOKEN` | Claude Code | 当前 key 的值（Bearer 鉴权） |
| `ANTHROPIC_DEFAULT_OPUS_MODEL` | Claude Code | 选定模型 id（与 sonnet 槽相同） |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | Claude Code | 选定模型 id（与 opus 槽相同） |

| 环境变量 | 默认 |
|---|---|
| `LLM_PROVIDER_CONFIG` | `~/.config/llm-provider-manager/providers.jsonc` |
| `LLM_PROVIDER_ACTIVE_ENV` | `~/.config/llm-provider-manager/active.env.sh` |
| `LLM_PROVIDER_CLAUDE_OUT` | `~/.claude/settings.local.json` |
| `LLM_PROVIDER_OPENCODE_OUT` | `~/.config/opencode/opencode.json` |

## 9. 运行时密钥池（keypool.py）

`use` 是**交互式**地在 shell 里选一个 provider+key；`keypool` 是其**运行时**对应物——给批量任务自动跨密钥池轮转、按错误禁用 key、把错误 payload 分类成恢复动作。它是**库**（入口 `dispatch()`），不是 CLI 子命令。

`KeyPool` 把所有 provider 的 key 拍平成一个池，并按 active agent（`--agent`）过滤：

- 跳过 `agentBlacklist` 含当前 agent 的 key；
- 跳过对该 agent 不可用的 provider（`Agent.base_url_for(provider)` 返回空，即没有匹配协议的 baseURL）；
- base_url 由 `Agent.base_url_for(provider)` 选协议（claude→anthropic，opencode→openai）。

状态文件由**调用方提供**（`--state`）：`{current_index, disabled{idx: 过期时间}, success_count}`——运行时状态留在调用方上下文，不进 lpm 的用户配置目录。并发用 `fcntl.flock`（读 `LOCK_SH`、读-改-写 `LOCK_EX`，`json.dump` 后紧跟 `f.truncate()` 把 write 收进锁临界区）。禁用 TTL、轮换阈值取自 `settings.disableTtlHours` / `settings.rotateEvery`。

模型分层：默认 `models[0]`=primary、`models[1]`=downgrade（单元素则两者相同）；可选 `primaryModel`/`downgradeModel`（provider/key 级，见 schema 校验）覆盖。

错误分类复用 §4 的 provider 后端：从状态解析当前 provider → 取其 `errorHandling` 覆盖 → `providers.classify(pid, payload, handling)`。

`dispatch(argv)` 子命令：`init / rotate / on-success / disable / size / available-size / classify / retry-plan`。`init/rotate/on-success` 产出一行 JSON `{key, base_url, primary_model, downgrade_model}`（空行=无 key/未轮换）；`classify` 产出 `"action:disable"`；`retry-plan` 产出 `(tier, count)` 行。批量消费者调用这些驱动轮转与重试，例如：

```bash
python -m llm_provider_manager.keypool init --config providers.jsonc --state /tmp/s.json --agent claude
```
