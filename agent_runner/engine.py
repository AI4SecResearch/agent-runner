"""通用 agent 编排。

分层设计(每层叠加一项能力),贯穿以下不变量:
  * disable 决策的分水岭:session_{new,resume,fork} 的主试用 ``_with_check``
    (不自行 disable);disable/rotate/downgrade 由 ``_retry_loop`` 在循环顶部
    经 ``react`` 统一决策。
  * 反应式重试:每次失败重新经 ``react`` 分类;换 key 后遇到不同的错误码会得到
    相称的策略。
  * 续接 vs 重跑:优先从主试日志取 session_id 走续接分支(--resume <sid> +
    提示词"继续");无 session 时按主试原样重跑(重发 $prompt + $session_args)
    ——分叉场景重新 fork 自源会话,绝不裸 resume 共享上下文。
  * 退出码:``_with_check``→0/1,``_retry_loop`` 与三个公开入口 + 别名→0/1。
  * 不信任进程退出码:成功与否一律由 ``result_ok`` 读日志判定。

编排状态为模块级(对应一组模块变量,无 Engine 类、不做多实例隔离)。后端由
``$AGENT_BACKEND`` 选定,在各公开入口按需懒解析,故环境变量改动在下次调用即
生效,无需 configure()、无 import 顺序依赖。
"""

from __future__ import annotations

import os
import signal
import time
from dataclasses import dataclass

from .backends import get_backend
from .keypool import KeyPool


# ── structured run outcome ────────────────────────────────────────────────
@dataclass
class Result:
    """The outcome of one public agent invocation.

    Fields:
      rc:          0 = success, 1 = all retries failed, 2 = quota exhausted
                   (no key pool) — same semantics as the bash exit codes.
      session_id:  the session id the agent recorded for the successful attempt
                   (empty when none / unsupported / the run failed). Library
                   callers thread this into a subsequent ``_resume``/``_fork``.
      text:        the agent's result text (claude ``.result`` / opencode
                   ``.part.text``) from the successful attempt. Captured only so
                   library callers can read it without re-parsing the jsonl;
                   the process mode does NOT print it to stdout.

    ``__int__``/``__bool__`` keep exit-code idioms working (``int(res)`` for the
    CLI; ``if res:`` for success)."""
    rc: int
    session_id: str = ""
    text: str = ""

    def __int__(self) -> int:
        return self.rc

    def __bool__(self) -> bool:
        return self.rc == 0

# ── module-level state (mirror of runner.sh's shell variables) ────────────
_backend = None
_backend_name: str | None = None
_kp: KeyPool | None = None
_kp_current_env_var: str | None = None  # tracked inside KeyPool too; kept for parity


def _get_backend():
    """Resolve the active backend from ``$AGENT_BACKEND`` (default claude-code),
    lazily. Re-resolves on each call so an env change between calls switches
    backends — same source as bash's common.sh, no configure() needed."""
    global _backend, _backend_name
    name = os.environ.get("AGENT_BACKEND", "claude-code")
    if _backend is None or name != _backend_name:
        _backend = get_backend(name)
        _backend_name = name
    return _backend


def _kp_config() -> str:
    # Default: <DATA_DIR>/providers.jsonc (or <OUTPUT_DIR>/...). Resolved
    # relative to the caller's runtime root (OUTPUT_DIR/DATA_DIR), NOT this
    # package's location — the standalone project makes no assumption about
    # where it's checked out. Callers normally set KEY_POOL_CONFIG explicitly.
    base = os.environ.get("DATA_DIR") or os.environ.get("OUTPUT_DIR") or "."
    return os.environ.get(
        "KEY_POOL_CONFIG",
        os.environ.get("LLM_PROVIDER_CONFIG", os.path.join(base, "providers.jsonc")),
    )


def _kp_state() -> str:
    base = os.environ.get("DATA_DIR") or os.environ.get("OUTPUT_DIR") or "."
    return os.path.join(base, "key-pool-state.json")


def _ensure_keypool() -> KeyPool:
    """Construct the keypool on first need (mirrors key_pool_init)."""
    global _kp
    if _kp is None:
        backend = _get_backend()
        _kp = KeyPool(_kp_config(), _kp_state(), agent_id=backend.agent_id, backend=backend)
    return _kp


# ── result check & error classification ────────────────────────────────────

def _check_agent_result(log_name: str) -> bool:
    """0=ok / 1=fail — mirrors runner.sh's _check_agent_result."""
    output = os.environ["OUTPUT_DIR"]
    return _get_backend().result_ok(f"{output}/{log_name}")


def classify_agent_error(log_name: str) -> str:
    """Output an atom strategy string (e.g. "disable,rotate", "downgrade")."""
    output = os.environ["OUTPUT_DIR"]
    text = _get_backend().result_text(f"{output}/{log_name}")
    return _ensure_keypool().classify(text)


# ── process execution (pure orchestration, generic) ───────────────────────

def _agent_once(prompt: str, log_name: str, extra: list[str]):
    """Assemble argv (perm + primary-model unless caller gave --model + pass-
    through) → backend.invoke. Guarantees exactly one --model (caller overrides
    primary). Returns the ``Popen`` (NOT waited on) — the watchdog owns the
    lifecycle. Success is never judged from the returncode (see result_ok)."""
    output = os.environ["OUTPUT_DIR"]
    prefix = f"{output}/{log_name}"
    backend = _get_backend()

    argv = list(backend.perm_args())
    has_model = "--model" in extra
    if not has_model:
        argv += backend.model_args("primary")
    argv += list(extra)

    return backend.invoke(prompt, prefix, argv)


def _agent_once_with_watchdog(prompt: str, log_name: str, extra: list[str]) -> tuple[int, int]:
    """后台启动 agent,reader 线程把 stdout 流式写入 jsonl,主线程轮询早退/
    超时,正常则等待、超时则杀。返回 (进程退出码, 看门狗状态):看门狗状态
    0=正常结束、1=超时被杀。

    reader 线程只把 stdout 逐行写入 <prefix>.jsonl(结果行一落地,is_complete
    即可见);**不**打印到 stdout——stdout 留给进程形态的 session_id 行,结果
    文本由 ``result_body`` 按需从日志读回。主线程轮询 ``is_complete`` / 文件
    增长 / 总超时,超时则杀整个进程组(先杀子进程再杀父)。agent 写出结果行即
    早退,不等进程自身收尾。
    """
    import threading

    output = os.environ["OUTPUT_DIR"]
    prefix = f"{output}/{log_name}"
    jsonl_path = f"{output}/{log_name}.jsonl"
    stall_timeout = int(os.environ.get("AGENT_STALL_TIMEOUT", "300"))
    max_timeout = int(os.environ.get("AGENT_TIMEOUT", "0"))
    backend = _get_backend()

    proc = _agent_once(prompt, log_name, extra)

    # reader 线程:stdout → jsonl(阻塞至 stdout EOF,即进程退出——正常或被杀)。
    reader = threading.Thread(target=backend.stream, args=(proc, prefix), daemon=True)
    reader.start()

    start = time.time()
    last_size = 0
    last_growth = start
    timed_out = False
    timeout_reason = ""

    while proc.poll() is None:
        now = time.time()
        elapsed = now - start

        # agent emitted its final result → early exit (don't wait for teardown)
        if backend.is_complete(prefix):
            break

        # total hard timeout
        if max_timeout > 0 and elapsed >= max_timeout:
            timed_out = True
            timeout_reason = f"总超时 {int(elapsed)}s >= {max_timeout}s"
            break

        # file growth (stall detection)
        try:
            cur = os.path.getsize(jsonl_path)
        except OSError:
            cur = 0
        if cur > last_size:
            last_size = cur
            last_growth = now

        stall_elapsed = now - last_growth
        if stall_timeout > 0 and stall_elapsed >= stall_timeout:
            timed_out = True
            timeout_reason = f"无进展 {int(stall_elapsed)}s >= {stall_timeout}s"
            break

        time.sleep(10)

    if timed_out:
        # Kill the whole process group (agent + any landlock helper) — the
        # Popen used start_new_session=True so the agent is its own group
        # leader; -SIGKILL the group, then reap.
        _kill_process_group(proc)
        reader.join(timeout=5)
        sys_stderr_write(f"          ⚠️ 超时({timeout_reason}): {log_name}\n")
        return proc.returncode or 0, 1

    # normal: wait for the reader to finish draining stdout
    reader.join()
    return proc.returncode or 0, 0


def _kill_process_group(proc) -> None:
    """Kill the agent's process tree via the platform abstraction (POSIX:
    -KILL the session group; Windows: stub — see platform.py), then reap."""
    from .platform import PLATFORM
    PLATFORM.kill_tree(proc)


# ── 带检查的单次执行 ───────────────────────────────────────────────────────

def _agent_once_with_check(prompt: str, log_name: str, extra: list[str]) -> Result:
    """看门狗 + 业务面结果检查。成功时返回带 session_id 与结果文本的 Result;
    失败(含超时被杀)返回 rc=1 的 Result。不碰密钥池——disable 由调用方决策
    (重试循环在循环顶部统一决策,单次执行不重复 disable)。

    session_id 与结果文本都从成功日志读回(单一真相,与后端是否流式无关)。"""
    output = os.environ["OUTPUT_DIR"]
    backend = _get_backend()
    _rc, wd = _agent_once_with_watchdog(prompt, log_name, extra)
    if wd == 1:
        return Result(1)  # 超时被杀 → 视为可重试失败
    if _check_agent_result(log_name):
        prefix = f"{output}/{log_name}"
        sid = backend.session_id(prefix)
        text = backend.result_body(prefix)
        return Result(0, sid, text)
    return Result(1)


def _agent_once_with_disable(prompt: str, log_name: str, extra: list[str]) -> Result:
    """在 ``_agent_once_with_check`` 之上加 disable 副作用:失败时按错误分类决定
    是否禁用当前 key。返回 rc=0 成功 / 1 失败(可重试)/ 2 额度耗尽且无密钥池
    (放弃)。供无外层重试循环、需自行处理 disable 的单次执行场景使用。"""
    res = _agent_once_with_check(prompt, log_name, extra)
    if res.rc == 0:
        return res
    strategy = classify_agent_error(log_name)
    if "disable" in f",{strategy},".split(","):
        if os.path.isfile(_kp_config()):
            _ensure_keypool().disable()
        else:
            sys_stderr_write(f"          ⚠️ 额度耗尽且无 key pool: {log_name}\n")
            return Result(2)
    return Result(1)


# ── 重试编排(反应式:每次失败决定一步) ────────────────────────────────────

def _agent_retry_loop(prompt: str, base_log: str, session_args: list[str],
                      extra: list[str]) -> Result:
    """共享的反应式重试循环。前置:调用方已 ``key_pool_init`` 并跑完一次失败的
    主试(日志 base_log)。``session_args`` 是主试所用的会话 flag(""=全新 /
    --resume S=续接 / --resume S --fork-session=分叉),仅在重跑分支按原样复用。

    续接策略:优先从主试日志取 session_id 走续接分支("继续" + --resume <sid>);
    无 session 时按主试原样重跑(fork 场景重新 fork 自源会话,绝不裸 resume
    共享上下文,避免污染其它 fork)。成功的那次重试的 Result(含 session_id 与
    结果文本)向上返回。
    """
    output = os.environ["OUTPUT_DIR"]
    backend = _get_backend()
    kp = _ensure_keypool()

    cont_sid = backend.session_id(f"{output}/{base_log}")
    cont_resume = backend.resume_args(cont_sid)

    n = kp.available_size()
    max_attempts = n + 2  # 给降级档留余量
    if max_attempts <= 0:
        max_attempts = 2

    attempt = 0
    cur_log = base_log  # 最近一次失败的日志(react 读它)

    while attempt < max_attempts:
        text = backend.result_text(f"{output}/{cur_log}")
        step = kp.react(text)
        if step == "stop":
            sys_stderr_write(f"          ⚠️ 资源耗尽（无可用 key/模型）: {cur_log}\n")
            return Result(1)

        # 执行策略里的原子
        atoms = f",{step},".split(",")
        if "disable" in atoms:
            kp.disable()
        if "rotate" in atoms:
            kp.rotate()
        model = "downgrade" if "downgrade" in atoms else "primary"

        attempt += 1
        name = f"{base_log}-r{attempt}"
        model_args = backend.model_args(model)
        sys_stderr_write(
            f"          ⚠️ 重试 {attempt}/{max_attempts} ({model} / {step}): {base_log}\n"
        )

        if cont_resume:
            # 续接:在主试已记录的 session 上"继续"(不再 fork、不重发 session_args)
            res = _agent_once_with_check("继续", name, cont_resume + model_args)
        else:
            # 无 session 可接续 → 按主试原样重跑
            # (new 重发 $prompt;resume 重接 S;fork 重新 fork 自 S)
            res = _agent_once_with_check(prompt, name, list(session_args) + model_args)

        if res.rc == 0:
            if model != "downgrade":
                kp.on_success()
            return res  # 带这次成功重试的 session_id + 结果文本
        # 失败 → 下次循环 react 读这次重试的日志
        cur_log = name

    sys_stderr_write(f"          ⚠️ 重试次数达上限 ({max_attempts}): {base_log}\n")
    return Result(1)


# ── 公开入口(new / resume / fork)────────────────────────────────────────

def agent_with_retry_session_new(prompt: str, log_name: str, *extra: str) -> Result:
    """全新会话运行。返回 Result(rc=0 成功 / 1=均失败)。"""
    extra_list = list(extra)
    _ensure_keypool().init()
    res = _agent_once_with_check(prompt, log_name, extra_list)
    if res.rc == 0:
        _ensure_keypool().on_success()
        return res
    return _agent_retry_loop(prompt, log_name, [], extra_list)


def agent_with_retry_session_resume(prompt: str, log_name: str, sid: str, *extra: str) -> Result:
    """在 session_id 上续接(resume:同一会话,积累上下文)。返回 Result(rc=0/1)。"""
    extra_list = list(extra)
    backend = _get_backend()
    _ensure_keypool().init()
    session_args = backend.resume_args(sid)
    res = _agent_once_with_check(prompt, log_name, session_args + extra_list)
    if res.rc == 0:
        _ensure_keypool().on_success()
        return res
    return _agent_retry_loop(prompt, log_name, session_args, extra_list)


def agent_with_retry_session_fork(prompt: str, log_name: str, sid: str, *extra: str) -> Result:
    """从 session_id 分叉(fork:拷贝一份独立会话再跑)。返回 Result(rc=0/1)。"""
    extra_list = list(extra)
    backend = _get_backend()
    _ensure_keypool().init()
    session_args = backend.fork_args(sid)
    res = _agent_once_with_check(prompt, log_name, session_args + extra_list)
    if res.rc == 0:
        _ensure_keypool().on_success()
        return res
    return _agent_retry_loop(prompt, log_name, session_args, extra_list)


def agent_once_session_resume(prompt: str, log_name: str, sid: str, *extra: str) -> Result:
    """单次续接(无重试循环)。运行一次,后端支持则续接已有 session(否则退化为
    全新会话)。失败时按错误分类自行 disable 当前 key(单次入口自带 disable,
    没有外层循环替它决策)。返回 Result(rc=0 成功 / 1 可重试失败 / 2 额度耗尽
    且无密钥池,放弃)。"""
    extra_list = list(extra)
    backend = _get_backend()
    _ensure_keypool().init()
    resume_args = backend.resume_args(sid)
    return _agent_once_with_disable(prompt, log_name, resume_args + extra_list)


def agent_with_retry(prompt: str, log_name: str, *extra: str) -> Result:
    """``agent_with_retry_session_new`` 的兼容别名。"""
    return agent_with_retry_session_new(prompt, log_name, *extra)


# ── stderr 写入助手(模块内自用)──────────────────────────────────────────

def sys_stderr_write(s: str) -> None:
    """写一行到 stderr(调用方自带尾换行)。"""
    import sys
    sys.stderr.write(s)
    sys.stderr.flush()
