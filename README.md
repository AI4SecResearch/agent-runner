# agent-runner (Python)

一个可靠的 agent-CLI 执行层 —— 具备 **watchdog 超时、带模型降级的反应式重试、多 provider 密钥池轮换、会话复用** —— 以 **Python 库** 与 **进程**（`python -m` 或薄 `.sh` 封装）两种形态提供，既可充当 bash 流水线的"高可靠 agent"父进程，也可作为依赖嵌入 Python 代码库。项目独立、跨平台(POSIX)、无运行时依赖 bash 或 `jq`；内置的 `llm-provider-manager`（lpm）位于 `llm-provider-manager/`。实现与并发设计见 **[ARCHITECTURE.md](ARCHITECTURE.md)**。

## 两种调用形态

**库形态**(Python 上层):

```python
import os, sys
sys.path.insert(0, "/path/to/agent-runner-py")  # 或:pip install -e .

os.environ["AR_RUN_DIR"] = "/var/run/mytask"     # 必填(产出根)
os.environ["AR_BACKEND"] = "claude-code"          # 或 "opencode";默认 claude-code

from agent_runner import agent_with_retry, agent_with_retry_session_resume
res = agent_with_retry("总结这份文档", "summary")   # 返回 Result
if res:
    # 多步会话:用上一步的 session_id 续接
    agent_with_retry_session_resume("精修 markdown", "refined", res.session_id)
```

### 多线程使用

模块级函数(`agent_with_retry` 等)内部委托一个 **thread-local 默认 `Runner`** —— 即:单线程调用方**零改动**即可跨线程并发使用,每线程各自独立的编排状态、keypool、env 快照,无跨线程竞态。

需**多 Agent 微调**或显式隔离的场景,用 `Runner(config_overrides=...)` —— 每个实例持自己的 `Config`(优先级:`config_overrides` > `AR_` env > TOML > 默认),backend、keypool 全隔离。嵌入方已经提供完整显式配置视图、且不允许读取候选 TOML 文件时，传 keyword-only `discover_config_files=False`；该参数只关闭 TOML 文件发现，`AR_` 环境层仍保留，因此调用方必须显式覆盖全部安全相关配置键。参数只接受 `bool`，省略或传 `True` 均保持既有搜索行为:

```python
from agent_runner import Runner
from pathlib import Path

# 不同线程跑不同 backend / 模型 / 超时,互不干扰
r_claude = Runner(config_overrides={"backend": "claude-code", "primary_model": "glm-5.1"})
r_oc = Runner(config_overrides={
    "backend": "opencode",
    "primary_model": "glm-4.7",
    "stall_timeout": 600,
})
# 各自在自己的线程里调用:
res = r_claude.agent_with_retry("总结这份文档", "summary")

# 显式 Runner 的 new/resume/fork 可为单次调用指定 Agent 子进程工作目录:
res = r_claude.agent_with_retry_session_new(
    "检查这个工作区",
    "workspace-check",
    working_directory=Path("/srv/workspaces/task-1"),
)
```

`working_directory`是 explicit `Runner` 的 new/resume/fork 入口上的
keyword-only 参数，并逐次透传到所有内部重试的`Popen(cwd=...)`。默认`None`
保持既有调用行为；Runner 不调用进程级`os.chdir()`，因此不同线程可以安全使用
不同工作目录。目录存在性和授权范围仍由嵌入方负责。

隔离的实现机制(`KeyContext` 纯值透传、`Popen(env=...)` 隔离快照、per-call err 句柄脱单例、线程安全 ⟹ 进程安全的推理)见 [ARCHITECTURE.md § Concurrency model](ARCHITECTURE.md#concurrency-model-multi-threaded)。`tests/test_threading.py` 端到端验证了这些保证。

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
- 结果文本不进 stdout —— 已全量留存于 `$run_dir/<log_name>.jsonl`;库形态则经 `Result.text` 给到调用方。

entry(进程形态取一):`new` / `resume` / `fork` / `once` / `agent_with_retry`(全名如 `agent_with_retry_session_new` 亦接受)。`resume`/`fork`/`once` 在 `log_name` 之后还需 `session_id`;再之后的参数透传给 agent(如 `--model x`)。

## 配置(TOML 文件 + `AR_` env)

所有配置项走同一套机制：写进 TOML 文件，或用 `AR_` 前缀的环境变量覆盖(env 优先)，硬编码默认值兜底。优先级:**`AR_` env > TOML > 默认**。哪项放哪由调用方决定,agent-runner 不做规定。

**TOML 查找**(取第一个存在的)：`$AR_CONFIG_FILE` → `./agent-runner.toml` → `~/.config/agent-runner/config.toml`。完整注释模板见 `agent-runner.example.toml`。无 TOML 也能跑(默认 + env)。显式构造 `Runner(..., discover_config_files=False)`时跳过这三处候选文件；配置优先级变为 `config_overrides` > `AR_` env > 默认。

| TOML key | `AR_` env | 默认值 | 用途 |
|---|---|---|---|
| `backend` | `AR_BACKEND` | `claude-code` | agent 后端 |
| `primary_model` | `AR_PRIMARY_MODEL` | (无) | 调用方意愿的模型(provider 供应校验;见下) |
| `downgrade_model` | `AR_DOWNGRADE_MODEL` | (无) | 降级档模型(同上) |
| `key_pool_config` | `AR_KEY_POOL_CONFIG` | (无) | providers.jsonc 路径 |
| `keypool_state` | `AR_KEYPOOL_STATE` | = key_pool_config 同目录 | 密钥池状态文件 |
| `run_dir` | `AR_RUN_DIR` | (必填) | 产出根(jsonl/err/产物) |
| `sandbox` | `AR_SANDBOX` | `false` | 跳过权限提示 |
| `stall_timeout` | `AR_STALL_TIMEOUT` | `300` | 无输出多少秒后杀掉 |
| `total_timeout` | `AR_TOTAL_TIMEOUT` | `0` | 硬总超时(0 = 不限) |
| `lpm_src` | `AR_LPM_SRC` | 内置副本 | lpm 源目录覆盖(进程级) |
| `opencode_auth_env_var` | `AR_OPENCODE_AUTH_ENV_VAR` | `Z_AI_API_KEY` | opencode 读 API key 的 env 变量 |

**模型选择**：`primary_model`/`downgrade_model` 表达调用方意愿。运行时密钥池检查所求模型是否在 provider 的可用 `models` 列表(来自 providers.jsonc)里：在 → 用它；不在 → 回落 provider 声明的 `primaryModel`/`downgradeModel`。无密钥池 → 原样透传给 agent。agent-runner **从不硬编码**模型名——一律来自 config/provider。

## 测试

```bash
pip install pytest       # 唯一开发依赖
cd agent-runner
python -m pytest -q
```

- `test_backends_jq_equiv.py` —— Python 的 jsonl 解析与 bash `jq` 过滤器逐字节等价(bash 后端不存在时跳过)。
- `test_config.py` —— `Config`解析、优先级、类型转换，以及 `Runner`候选配置文件发现 Interface。
- `test_engine.py` —— watchdog 早退、反应式重试、续接 vs 重跑分支、退出码(mock backend,不起真 agent)。
- `test_working_directory.py` —— new/resume/fork逐调用工作目录透传、两种 backend 的`Popen(cwd=...)`映射，以及内部重试保持同一目录。
- `test_platform.py` —— POSIX 进程组拉起 + 树杀契约。
- `test_cli.py` —— `python -m agent_runner` 派发、参数顺序、退出码映射。
- `test_threading.py` —— 多线程隔离:per-thread key/子进程 env 隔离、err 句柄脱单例、thread-local 默认 `Runner`、`Runner(config_overrides=...)` 配置隔离。

## 目录结构

```
agent-runner/
├── agent-runner.sh              # bash → `python -m agent_runner` 封装(进程形态)
├── pyproject.toml               # 包元数据(纯 stdlib;支持 pip -e .)
├── README.md                    # 本文件 — 使用指南
├── ARCHITECTURE.md              # 实现与并发设计
├── agent_runner/                # 可 import 的包
│   ├── __init__.py              # 公开 API(库形态)
│   ├── __main__.py              # `python -m agent_runner`(进程形态)
│   ├── engine.py                # 编排(Runner)
│   ├── config.py                # Config(per-实例配置视图)
│   ├── platform.py              # 跨平台进程树抽象
│   ├── keypool.py               # KeyPool 包装 + KeyContext
│   └── backends/{claude_code,opencode,_jsonl}.py
├── llm-provider-manager/        # 内置 lpm(git subtree)— keypool/providers/agents
└── tests/
```
