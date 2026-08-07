"""进程形态入口:``python -m agent_runner``(经 ``agent-runner.sh`` 供 bash 上层调用)。

把库形态的 agent 函数暴露为命令行,使上层进程(尤其 bash 流水线)能像调用一个
普通 agent 二进制那样驱动本项目——一个自带 watchdog/重试/密钥池的"高可靠 agent"
子进程。

输出通道分工:
  - ``$?``   成败(0 成功 / 1 均失败 / 2 额度耗尽且无密钥池)。
  - stdout   session_id 一行(无则空行)。bash 上层用 ``sid=$(...)`` 取之。
  - stderr   诊断(重试/超时/资源耗尽通告,给人看)。
结果文本不进 stdout——已留存于 ``$OUTPUT_DIR/<log_name>.jsonl``。

用法(经 ``agent-runner.sh``:bash 已把 ``--tier`` 解析为第 2 个位置参数):
    python -m agent_runner <entry> <model_tier> <prompt> <log_name> [session_id] [-- extra...]
    python -m agent_runner --help

<entry> 选操作(resume/fork 还需 session_id):
    new | resume | fork | agent_with_retry
    (全名亦接受:agent_with_retry_session_new 等)

<model_tier> = primary | downgrade(agent-runner.sh 从用户的 ``--tier`` 解析注入,
默认 primary;直接调 ``python -m`` 时自行提供)。``--`` 之后的参数透传给 agent
(如 --model)。CLI 标志解析在 agent-runner.sh;本模块只收干净的位置参数。
"""

from __future__ import annotations

import sys

from . import engine as _engine

# entry 名 → (engine 上的属性名, 是否吃 session_id)。调用时按名查(不在 import
# 时绑定),使测试 monkeypatch engine 属性后此处可见。
_ENTRIES = {
    "new": ("agent_with_retry_session_new", False),
    "resume": ("agent_with_retry_session_resume", True),
    "fork": ("agent_with_retry_session_fork", True),
    "agent_with_retry": ("agent_with_retry", False),
    # 全名亦接受
    "agent_with_retry_session_new": ("agent_with_retry_session_new", False),
    "agent_with_retry_session_resume": ("agent_with_retry_session_resume", True),
    "agent_with_retry_session_fork": ("agent_with_retry_session_fork", True),
}

_USAGE = """\
agent-runner —— 可靠的 agent 执行(进程形态)

用户面(经 agent-runner.sh,bash 解析 [ARGUMENTS] 并重排):
  agent-runner.sh <entry> [--tier primary|downgrade] <prompt> <log_name> [session_id] [-- <passthrough>]

内部面(本模块直接接收):
  python -m agent_runner <entry> <model_tier> <prompt> <log_name> [session_id] [-- <passthrough>]

entry(取一):
  new / agent_with_retry_session_new        全新会话(== 别名 agent_with_retry)
  resume / agent_with_retry_session_resume  在已有 session 上续接
  fork / agent_with_retry_session_fork      从已有 session 分叉独立会话

<model_tier> = primary | downgrade(agent-runner.sh 从 --tier 注入,默认 primary)。
resume/fork 需 session_id。'--' 之后的参数透传给 agent(如 --model x)。

输出通道:$? = 成败(0 成功 / 1 均失败),stdout = session_id 一行(供
``sid=$(...)`` 捕获),stderr = 诊断。配置经环境变量(OUTPUT_DIR、AGENT_BACKEND、
KEY_POOL_CONFIG 等),与库形态一致。
"""


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        sys.stdout.write(_USAGE)
        return 0

    entry = argv[0]
    if entry not in _ENTRIES:
        sys.stderr.write(f"agent-runner: unknown entry '{entry}'.\n\n")
        sys.stdout.write(_USAGE)
        return 2
    if len(argv) < 2:
        sys.stderr.write(f"agent-runner: missing <model_tier> after entry '{entry}'.\n")
        return 2

    fn_name, takes_sid = _ENTRIES[entry]
    fn = getattr(_engine, fn_name)
    tier = argv[1]

    # argv[2:] = 位置参数(prompt, log_name[, sid]) + 可选 '--' + 透传
    rest = argv[2:]
    try:
        sep = rest.index("--")
    except ValueError:
        sep = len(rest)
    positionals, passthrough = rest[:sep], tuple(rest[sep + 1:])

    n_pos = 3 if takes_sid else 2
    if len(positionals) < n_pos:
        need = "<prompt> <log_name> <session_id>" if takes_sid else "<prompt> <log_name>"
        sys.stderr.write(f"agent-runner: entry '{entry}' needs {need}.\n")
        return 2

    if takes_sid:
        prompt, log_name, sid = positionals[0], positionals[1], positionals[2]
        res = fn(prompt, log_name, sid, tier, passthrough)
    else:
        prompt, log_name = positionals[0], positionals[1]
        res = fn(prompt, log_name, tier, passthrough)

    # 进程形态的输出通道分工:$? = 成败(0/1/2),stdout = session_id 一行
    # (无则空行),stderr = 诊断(由 engine 内部写)。结果文本不进 stdout——
    # 它已全量留存于 <prefix>.jsonl,库形态则经 Result.text 给到调用方。
    sid_out = getattr(res, "session_id", "") or ""
    sys.stdout.write(sid_out + "\n")
    sys.stdout.flush()
    return int(res)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
