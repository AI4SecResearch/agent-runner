# 架构设计

`llm-provider-manager` 是一个 **agent × provider 正交**的 LLM 切换工具。两条轴（agent 轴 + provider 轴）各自可插拔，通用层不硬编码任何 agent 或 provider 名。

## 1. 架构总览

```
+----------------------------------------------------------+
|  CLI (cli.py)                                            |
|  use / generate / list / status - registry dispatch      |
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
|    preferred_protocols |  |  opencsitool.py (quota)      |
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
    __init__.py          #   ATOMS/DEFAULT_ACTION + ProviderBackend 协议 + REGISTRY + classify/effective_error_handling
    default.py           #   DefaultProvider（兜底；不解析 payload，直接 _default=rotate）
    zhipu.py             #   ZhipuProvider + 自带 payload 解析（[NNNN] 码 → Signals）
    bailian.py           #   BailianProvider（纯继承 default）
    opencsitool.py       #   OpencsitoolProvider（自带文本匹配：预算/429 → disable,rotate）
  cli.py                 # 通用编排层：use / generate / list / status
  config.py              # JSONC 加载 + 权限检查
  env_contract.py        # 通用 shell helper（sh_export 等）
  schema.py              # providers.jsonc 的 typed schema
  use.py                 # use 命令核心 + shell hook + active.env.sh 持久化
  status.py              # status 命令核心：三层反解 + drift 检测
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

每个 provider 模块**内置** `default_error_handling`（码→动作映射），**不依赖配置文件**。`providers.jsonc` 的 `errorHandling` 是覆盖层，合并到内置默认之上。provider 在零配置时仍能正确分类。**框架对 payload 盲眼**——`classify()` 把原始 payload 文本透传给 backend，解析方式由各 provider 自定：

| provider 模块 | payload 解析 | 内置码 | 说明 |
|---|---|---|---|
| `default.py` | 不解析 | （空） | 兜底；直接返回 `_default=rotate` |
| `zhipu.py` | `[NNNN]` 方括号码 / JSON `code`/`status` | 1301/1305/1308/1310 | GLM 上游码 |
| `bailian.py` | （继承 default） | （空，待填充） | 当前靠配置或兜底 |
| `opencsitool.py` | 自文本匹配 budget/429 | budget/429 | 预算耗尽 → `disable,rotate`；其余落 `_default` |

动作词表（原子，逗号组合）：`disable` / `rotate` / `downgrade`。策略是有序原子串，如 `disable,rotate`、`rotate,downgrade`。`disable` 是一等原子——出现才禁用 key（不再像旧版那样隐含在 `rotate` 里），所以内容安全类错误（1301/1305）可以换 key/降级而**不**禁用 key。`classify` 返回原子策略串本身（如 `disable,rotate`、`downgrade`）。zhipu 内置：1301→`rotate,downgrade`、1305→`downgrade`、1308/1310→`disable,rotate`、`_default`→`rotate`（未知错误只换 key，不赌 key 坏或该降级）。

反应式重试（§9）：消费者每步调 `react` 子命令拿单步策略，执行后重新分类下一个错误，而非一次性预算完整计划。

## 5. Shell hook 机制

`init-shell-hook` 在 shell rc 里加一段带标记的块（重跑时**先删旧块再写新块**，故 hook 升级会自动传播）：

```sh
# >>> llm-provider-manager >>>
# restore last selection (or initialise from config 'default')
export LLM_PROVIDER_ACTIVE_ENV="$HOME/.config/llm-provider-manager/active.env.sh"
[ -f "$LLM_PROVIDER_ACTIVE_ENV" ] || eval "$($HOME/.local/bin/lpm use 2>/dev/null)"
source "$LLM_PROVIDER_ACTIVE_ENV" 2>/dev/null
# lpm: intercepts 'use' to source active.env.sh after running; all else passes through
lpm() {
    if [ "$1" = "use" ]; then
        command $HOME/.local/bin/lpm use "${@:2}" && source "$LLM_PROVIDER_ACTIVE_ENV"
    else
        command $HOME/.local/bin/lpm "$@"
    fi
}
# <<< llm-provider-manager <<<
```

- **新 shell**：`source active.env.sh` 恢复上次选择；无文件时从 config `default` 初始化。
- **`lpm use`**：跑 CLI 写 `active.env.sh` + `source` 进当前 shell（立即生效 + 持久化）。
- **`lpm list`/`lpm status` 等**：透传给 CLI。

`lpm()` 函数拦截 `use` 子命令做副作用（source），其余子命令透传给 `command lpm`（绕过函数）。与 `nvm`/`rbenv` 同模式。

hook 内部用**绝对路径** `$HOME/.local/bin/lpm` 而非裸 `lpm` 调用 CLI——这样即便 `~/.local/bin` 不在 PATH（非交互 shell、脚本、cron、PATH 被重置的子 shell 等），rc 时的初始化与 `lpm()` 内部转发仍能找到二进制。`lpm()` 仍以名字定义，交互式敲 `lpm` 照常命中函数。

## 5.5. status：当前终端的实际配置

`status` 是 `use` 的逆操作：`use` 写一个选择进 env + active.env.sh；`status` 读回"**当前终端、当前目录**下各 agent 实际生效的是什么"。它**不**信任单一来源，而是按三层优先级合并：

| 层 | 来源 | 说明 |
|---|---|---|
| 1（高） | 项目/用户 agent 配置文件字面量 | `./opencode.json`、`.claude/settings{.local,}.json`、`~/.claude/settings.local.json` 等。`lpm agent --inline` 烘焙的真实 key/model 在此，**覆盖** env。只取字面量值——`{env:...}` 占位符（`--template` 产物）不算覆盖，它延迟到层 2。 |
| 2 | 进程 env（`os.environ`） | `lpm use` 导出的值，继承自父 shell。这是"当前终端"的真相。 |
| 3（基准） | `active.env.sh` | **不是**配置来源，仅作 drift 对比基准：当层 2 的值与本文件记录不一致时标注 drift（如另一终端 `lpm use` 改了文件但本终端未 source）。 |

**正交性**：每层"该查哪些 env 变量/哪些文件/怎么反解出 provider/key/model"的知识都在 `agents/` 包里（`probe` / `probe_config_file` / `config_probe_paths`），通用层 `status.py` 只做编排——遍历注册表、合并覆盖（把覆盖值并进 env 后**复用 agent 自己的 `probe`** 重新反解，避免匹配逻辑重复）、算 drift、渲染。通用层不出现 `ANTHROPIC_*` / `LLM_KEY_*` 名。

**反解策略**：
- Claude：provider 按 `base_urls["anthropic"] == ANTHROPIC_BASE_URL` 匹配；key 按 `key.key == ANTHROPIC_AUTH_TOKEN` 匹配；model 直接读 env。
- opencode：`LLM_DEFAULT_MODEL`（`<entryId>/<modelId>`）的 entryId 直接给 provider(+keyid)——`provider` 形 = symmetric/单 key，`provider-keyid` 形 = 多 key asymmetric（与 `render_config` 的 `opencode_entry_id` 对称）。各 `LLM_KEY_*` 收集为佐证；blacklist key 的空字符串属正常。

**范围（MVP）**：只查 `<cwd>` 下项目文件 + 用户默认输出路径，不向上遍历父目录（可预测）。输出人类可读（无 `--json`）。真实 key 脱敏（只显示首尾）。

## 6. 扩展点

### 新增一个 agent

1. 在 `agents/` 加 `<name>.py`，实现 `Agent` 协议（`id`/`preferred_protocols`/`base_url_for`/`is_usable`/`exports_for`/`render_config`/`probe`/`probe_config_file`/`config_probe_paths`/`default_config_path`/`config_path_env_var`）。
2. 在 `agents/__init__.py` 的 `_build_registry()` 加一行。
3. 无需改 `cli.py`/`use.py`/`status.py`/`schema.py`——注册表自动发现。`status` 借新增的 `probe*` 方法自动支持新 agent。

### 新增一个 provider

1. 在 `providers/` 加 `<name>.py`，实现 `ProviderBackend` 协议（`id`/`default_error_handling`/`classify`）。`classify` 自行解析 payload（框架不做预解析）；可继承 `DefaultProvider` 拿到"直接返回 `_default`"的兜底行为，再按需 override。
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

`dispatch(argv)` 子命令：`init / rotate / on-success / disable / size / available-size / classify / react`。`init/rotate/on-success` 产出一行 JSON `{key, base_url, primary_model, downgrade_model}`（空行=无 key/未轮换）；`classify` 与 `react` 均产出原子策略串（如 `disable,rotate`、`downgrade`），`react` 额外可产出 `stop`——批量消费者每步调用它驱动**反应式**重试（每步重新分类新错误），例如：

```bash
python -m llm_provider_manager.keypool init --config providers.jsonc --state /tmp/s.json --agent claude
```
