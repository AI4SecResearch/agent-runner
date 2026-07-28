"""进程形态入口:``python -m agent_runner``(经 ``agent-runner.sh`` 供 bash 上层调用)。

把库形态的 agent 函数暴露为命令行,使上层进程(尤其 bash 流水线)能像调用一个
普通 agent 二进制那样驱动本项目——一个自带 watchdog/重试/密钥池的"高可靠 agent"
子进程。

输出通道分工:
  - ``$?``   成败(0 成功 / 1 后端失败 / 2 已确认资源耗尽且无可执行恢复)。
  - stdout   session_id 一行(无则空行)。bash 上层用 ``sid=$(...)`` 取之。
  - stderr   诊断(重试/超时/资源耗尽通告,给人看)。
结果文本不进 stdout——已留存于 ``$OUTPUT_DIR/<log_name>.jsonl``。

用法:
    python -m agent_runner <entry> <prompt> <log_name> [session_id] [-- extra...]
    python -m agent_runner --help

<entry> 选操作(resume/fork 还需 session_id):
    new | resume | fork | agent_with_retry
    (全名亦接受:agent_with_retry_session_new 等)

位置参数之后的全部透传给 agent 作为 extra(如 --model 等);开头的 ``--`` 分隔符
会被吞掉(便于显式标记透传区)。
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

用法:
  agent-runner <entry> <prompt> <log_name> [session_id] [-- extra...]

entry(取一):
  new / agent_with_retry_session_new        全新会话(== 别名 agent_with_retry)
  resume / agent_with_retry_session_resume  在已有 session 上续接
  fork / agent_with_retry_session_fork      从已有 session 分叉独立会话

resume/fork 需 session_id。其后的参数透传给 agent(如 --model x);开头的
'--' 分隔符会被吞掉。

输出通道:$? = 成败(0 成功 / 1 均失败),stdout = session_id 一行(供
``sid=$(...)`` 捕获),stderr = 诊断。配置经环境变量(OUTPUT_DIR、AGENT_BACKEND、
SANDBOX、KEY_POOL_CONFIG 等),与库形态一致。
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

    fn_name, takes_sid = _ENTRIES[entry]
    fn = getattr(_engine, fn_name)
    rest = list(argv[1:])

    if takes_sid:
        if len(rest) < 3:
            sys.stderr.write(
                f"agent-runner: entry '{entry}' needs <prompt> <log_name> <session_id>.\n"
            )
            return 2
        prompt, log_name, sid = rest[0], rest[1], rest[2]
        extra = rest[3:]
    else:
        if len(rest) < 2:
            sys.stderr.write(
                f"agent-runner: entry '{entry}' needs <prompt> <log_name>.\n"
            )
            return 2
        prompt, log_name = rest[0], rest[1]
        extra = rest[2:]

    # 吞掉 extra 开头的一个 '--' 分隔符(便于显式标记透传区,如
    # `new p l -- --model x`)。非开头的 '--' 保留(那是真正的 agent 参数)。
    if extra and extra[0] == "--":
        extra = extra[1:]

    if takes_sid:
        res = fn(prompt, log_name, sid, *extra)
    else:
        res = fn(prompt, log_name, *extra)

    # 进程形态的输出通道分工:$? = 成败(0/1/2),stdout = session_id 一行
    # (无则空行),stderr = 诊断(由 engine 内部写)。结果文本不进 stdout——
    # 它已全量留存于 <prefix>.jsonl,库形态则经 Result.text 给到调用方。
    sid = getattr(res, "session_id", "") or ""
    sys.stdout.write(sid + "\n")
    sys.stdout.flush()
    return int(res)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
