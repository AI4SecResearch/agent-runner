# llm-provider-manager

管理多个大模型服务提供商，在终端中手动切换某个 agent 当前使用的 LLM（provider + key）。

## 安装

```bash
curl -fsSL https://raw.giteeusercontent.com/raverstern/llm-provider-manager/raw/master/install.sh | bash
```

安装脚本会：克隆代码到 `~/.local/share/llm-provider-manager` → 链接 `lpm` 命令到 `~/.local/bin` → 在 shell rc 装 `lpm()` 函数。幂等，重跑即更新。

## 快速开始

```bash
# 1. 创建你的 provider 配置并填入真实 key
#    （模板在 ~/.local/share/llm-provider-manager/providers.jsonc.example）
$EDITOR ~/.config/llm-provider-manager/providers.jsonc

# 2. 为各 agent 渲染静态配置（无密钥，可提交）
lpm agent --template all

# 3. 激活 shell hook（定义 lpm 函数）
source ~/.zshrc

# 4. 切换 LLM
lpm use                               # 默认 agent+provider+key（见 config 'default'）
lpm use <provider>                    # 默认 agent + 指定 provider/defaultKey
lpm use <provider> <key>              # 默认 agent + 指定 provider/key
lpm use --agent opencode <provider>   # 切换 opencode 的 provider
lpm list                              # 查看可选 provider
lpm list <provider>                   # 查看某 provider 的 key/model/error handling
```

之后启动 `claude` 或 `opencode` 即用上当前选择。新终端自动恢复上次切换。

## Provider 配置

顶层 `providers.jsonc`（JSONC，支持 `//` 与 `/* */` 注释）：

```jsonc
{
  "default": { "agent": "claude", "provider": "zhipu", "key": "grq" },  // use 无参时的默认
  "providers": [ /* 模板 A 或 B */ ]
}
```

### 模板 A — symmetric（所有 key 共享同一组模型）

```jsonc
{
  "id": "zhipu",
  "type": "symmetric",
  "displayName": "智谱",
  "defaultKey": "grq",
  "baseURLs": {
    "anthropic": "https://open.bigmodel.cn/api/anthropic",
    "openai":    "https://open.bigmodel.cn/api/coding/paas/v4"
  },
  "keys": [
    { "id": "grq", "key": "sk-zhipu-xxxxxxxx" },
    { "id": "backup", "key": "sk-zhipu-yyyyyyyy", "agentBlacklist": ["claude"] }
  ],
  "models": [
    { "id": "glm-5.2", "displayName": "GLM-5.2", "context": 1000000, "output": 131072 }
  ],
  "errorHandling": {              // 可选；覆盖 provider 模块的内置默认
    "1305": "downgrade",
    "_default": "rotate_then_downgrade"
  }
}
```

### 模板 B — asymmetric（不同 key 可用模型不同）

```jsonc
{
  "id": "bailian",
  "type": "asymmetric",
  "displayName": "百炼",
  "defaultKey": "grq",
  "baseURLs": { "openai": "https://dashscope.aliyuncs.com/compatible-mode/v1" },
  "keys": [
    { "id": "grq", "key": "sk-bailian-aaa",
      "models": [ { "id": "glm-5.2", "displayName": "GLM-5.2", "context": 1000000, "output": 131072 } ] }
  ],
  "errorHandling": { "_default": "rotate_key" }   // 可选
}
```

> 禁止某 key 用于某 agent——在 key 上加 `"agentBlacklist": ["claude"]`。`lpm use --agent claude` 时该 key 被 blocked；`lpm use --agent opencode` 时该 key 的 `LLM_KEY_*` 为空。

### 字段说明

| 字段 | 适用 | 说明 |
|---|---|---|
| `id` | 两者 | provider 唯一 id |
| `type` | 两者 | `symmetric` \| `asymmetric` |
| `displayName` | 两者 | 展示名 |
| `defaultKey` | 两者 | 可选；缺省取第一个 key |
| `baseURLs` | 两者 | 协议→URL 映射。`anthropic` 供 Claude；`openai` 供 opencode（openai 优先，无则退到 anthropic） |
| `keys[].id` | 两者 | key 的逻辑名 |
| `keys[].key` | 两者 | **真实 API key**（内联） |
| `keys[].agentBlacklist` | 两者 | 可选；禁止该 key 用于指定 agent |
| `keys[].models` | asymmetric | 该 key 可用模型（symmetric 不许写） |
| `models` | symmetric | provider 级模型列表（asymmetric 不许写） |
| `errorHandling` | 两者 | 可选；覆盖 provider 模块内置默认的码→动作映射 |
| `primaryModel` / `downgradeModel` | provider/key | 可选；keypool 轮转/重试用——显式指定主/次模型 id，覆盖默认的 `models[0]`/`[1]` |
| `default` | 顶层 | 可选；`{agent?, provider, key?}`，`use` 无参时的默认选择 |

### 校验规则

- symmetric：`models` 在 provider 级；key 下不许有 `models`。
- asymmetric：`models` 在每个 key 下；provider 级不许有 `models`。
- `defaultKey` / `default.provider` / `default.key` 必须存在；`default.agent`（若给）须为已注册 agent id。
- provider id 不可重复；同一 provider 内 key id 不可重复。
- `baseURLs` 至少一项。
- `agentBlacklist` 值须为已注册 agent id。
- 文件 group/other 可读会告警并提示 `chmod 600`。
- key 含 `REPLACE-ME` 会告警。

## 命令参考

### `use`

切换当前 shell 要用的 LLM。**agent-scoped**——只导出该 agent 的变量。写入 `active.env.sh`（0600）并 `source` 进当前 shell。

```bash
lpm use                              # 默认 agent+provider+key
lpm use <provider>                   # 默认 agent + provider/defaultKey
lpm use <provider> <key>             # 默认 agent + provider/key
lpm use --agent opencode <provider>  # opencode + provider/defaultKey
lpm use --agent opencode <provider> <key> --model <model>
```

`lpm use` 会自动 `source` `active.env.sh` 使环境变量立即生效（通过 shell hook 装的 `lpm()` 函数）。新终端从 `active.env.sh` 恢复上次选择。

若所选 key 被 agent-blacklist 或 provider 无该 agent 所需协议的 baseURL → blocked，不导出该 agent 变量，stderr 告警。

### `agent`

渲染某个 agent 的配置文件。两种模式（互斥）：

- **`--template`**：生成含 `{env:}` 占位符的模板（无密钥，可提交）。配合 `lpm use` 在运行时注入实际值。
- **`--inline`**：生成含真实 key 的配置（**含密钥，勿提交到公开 repo**）。可放到项目里供 agent 直接使用，无需 `lpm use`。

```bash
# 模板模式
lpm agent --template claude              # → ~/.claude/settings.local.json
lpm agent --template opencode            # → ~/.config/opencode/opencode.json
lpm agent --template all                 # 渲染所有注册 agent
lpm agent --template claude -o path.json

# inline 模式（烘焙真实 key）
lpm agent --inline claude                             # 用 config default 的 provider/key
lpm agent --inline opencode --provider <p> --key <k>  # 指定 provider/key
lpm agent --inline all                                # 所有 agent 的 inline 配置
```

文件已存在时默认跳过，`-f` 覆盖。输出路径优先级：`-o` > 环境变量 > agent 默认路径。

### `list` / `init-shell-hook`

```bash
lpm list                            # 列所有 provider
lpm list zhipu                      # 列某 provider 的 key/model/error handling
lpm init-shell-hook --rc ~/.zshrc   # 幂等装 lpm() 函数 + active.env.sh 恢复
```

## 作为运行时库（keypool）

除了交互式 `use`，lpm 还提供**运行时密钥池库** `llm_provider_manager.keypool`：给批量任务跨密钥池轮转、按错误禁用 key、把错误 payload 分类成恢复动作。它是库（入口 `dispatch()`），不是 CLI 子命令。`settings.rotateEvery`/`disableTtlHours`、`primaryModel`/`downgradeModel` 即为它配置。详见 [ARCHITECTURE.md](ARCHITECTURE.md) 的「运行时密钥池」一节。

## 故障排查

- **`warning: ... appears to contain REPLACE-ME placeholders`**：用了 example 没填 key。编辑 `providers.jsonc` 填入真实 key。
- **`error: no provider given and no 'default' configured`**：`use` 无参且未配 `default`。在 providers.jsonc 加 `default`，或 `lpm use <provider>`。
- **`error: unknown agent 'xxx'`**：`--agent` 传了未注册的 agent id。
- **Claude 没用上 provider**：确认 `lpm use` 已执行（`echo $ANTHROPIC_AUTH_TOKEN`）；确认当前 provider 有 `anthropic` baseURL 且 key 未 claude-blacklist。
- **opencode provider 报 apiKey 空**：该 key 被 opencode-blacklist；`lpm list` 查看；换 provider 或换 key。
- **`lpm: command not found`**：跑安装脚本，或手动 `ln -sf ~/.local/share/llm-provider-manager/llm-provider-manager ~/.local/bin/lpm`。
- **新终端没恢复上次选择**：确认 shell rc 有 hook 块（`lpm init-shell-hook --rc ~/.zshrc`）；确认 `active.env.sh` 存在（`lpm use` 一次后生成）。

## 测试

```bash
.venv/bin/python -m pytest -q           # 54 项
.venv/bin/python -m pyflakes src tests  # 零告警
```

---

技术细节（架构、扩展点、环境变量契约、安全）见 [ARCHITECTURE.md](ARCHITECTURE.md)。
