"""agent-runner —— 可靠的 agent 执行层(Python 实现)。

提供 watchdog 超时检测、带模型降级的反应式重试、跨 provider 密钥池轮换、
会话复用(new / resume / fork)。同一套能力有两种调用形态:

  - 库形态:``from agent_runner import agent_with_retry``,函数返回 ``Result``。
  - 进程形态:``python -m agent_runner`` 或经 ``agent-runner.sh`` 包装,供上层
    (尤其 bash 流水线)把本项目当作一个"高可靠 agent"子进程驱动。

公开 API(签名一致:首参 prompt、次参 log_name、其后可变 extra):

    agent_with_retry_session_new(prompt, log_name, *extra)          全新会话
    agent_with_retry_session_resume(prompt, log_name, sid, *extra)  续接同一会话
    agent_with_retry_session_fork(prompt, log_name, sid, *extra)    分叉独立会话
    agent_with_retry(prompt, log_name, *extra)                      = _new 别名

返回 ``Result``:``rc``(0 成功 / 1 均失败)、``session_id``(成功尝试记录的会话 id,
供后续 resume/fork 续接)、``text``(成功尝试的结果文本)。``int(Result)``/
``bool(Result)`` 保留退出码习惯。

**多线程**:`Runner` 类封装一条独立编排链(持自己的 ``Config``/backend/``KeyPool``);
模块级函数委托一个 thread-local 默认 ``Runner``,故老调用方零改动即可跨线程并发使用。
多 Agent 微调/显式隔离场景用 ``Runner(config_overrides={...})`` —— 各实例配置全隔离
(优先级:config_overrides > AR_ env > TOML > 默认)。keypool 返回纯 ``KeyContext``(不写
``os.environ``),经引擎透传给 ``backend.invoke(key_ctx=...)`` 构造隔离的子进程 env 快照。

进程形态的输出通道分工:``$?`` = 成败(0/1/2)、stdout = session_id 一行(供
``sid=$(agent-runner.sh ...)`` 捕获)、stderr = 诊断(重试/超时/耗尽通告)。结果
文本不进 stdout——已全量留存于 ``$OUTPUT_DIR/<log_name>.jsonl``。

内部函数(``_agent_once*`` / ``_agent_retry_loop`` / 模块级状态)留在
``engine.py``,不经 ``__init__`` 导出——这是 Python 独有的可见性边界(进程形态
无对应物,所有符号按名可见,靠 ``_`` 前缀约定)。

用法(纯 sys.path 分发,无需 pip 安装):

    import sys, os
    sys.path.insert(0, "/path/to/agent-runner-py")
    os.environ["AR_RUN_DIR"] = "/var/run/mytask"
    os.environ["AR_BACKEND"] = "claude-code"      # 或 "opencode";默认 claude-code
    from agent_runner import agent_with_retry
    res = agent_with_retry("总结这份文档", "summary")
    if res:
        print("session:", res.session_id)

后端/密钥池/认证全部经环境变量配置(``AR_RUN_DIR`` / ``AR_BACKEND`` /
``AR_SKIP_PERMISSIONS`` / ``AR_KEY_POOL_CONFIG`` / ``AR_LPM_SRC`` / ``AR_STALL_TIMEOUT`` /
``AR_TOTAL_TIMEOUT`` 等)。优先级:实例 ``config_overrides`` > ``AR_`` env > TOML > 默认。
"""

from .engine import (
    Result,
    Runner,
    agent_with_retry,
    agent_with_retry_session_fork,
    agent_with_retry_session_new,
    agent_with_retry_session_resume,
)

__all__ = [
    "Result",
    "Runner",
    "agent_with_retry",
    "agent_with_retry_session_new",
    "agent_with_retry_session_resume",
    "agent_with_retry_session_fork",
]
