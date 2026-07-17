"""多线程并发测试 —— 验证各线程 key/模型/err 句柄隔离,无跨线程 env 竞态。

核心保证(多线程改造的目标):
  ① keypool 返回纯 ``KeyContext``(不写 ``os.environ``),经引擎透传给
     ``backend.invoke(key_ctx=...)``,后者构造隔离的 ``Popen(env={**os.environ,
     **extra_env})`` —— 每线程 agent 子进程各拿各的 key 快照。
  ② backend 的 per-call 句柄(err 文件)挂在 ``proc._ar_err`` 上,非实例属性
     —— 并发 invoke 不互相 clobber。
  ③ 模块级公开函数委托 thread-local 默认 ``Runner``,每线程各自独立实例。
  ④ ``Runner(config_overrides=...)`` 各自隔离配置(多 Agent 微调)。

这里用 ``ThreadBackend``(继承真 ``ClaudeCodeBackend``,复用其 ``_build_env``
/``result_ok``/``is_complete`` 等真实解析,只把 invoke 换成"捕获 env + 不真起子进程")
+ ``ThreadFakeKP``(按线程名返回不同 key)来端到端验证 ①②。
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import agent_runner.engine as eng  # noqa: E402
from agent_runner.backends.claude_code import ClaudeCodeBackend  # noqa: E402
from agent_runner.backends._jsonl import ensure_parent  # noqa: E402
from agent_runner.keypool import KeyContext  # noqa: E402


def _result(text="ok", is_error=False):
    return {"type": "result", "result": text, "is_error": is_error}


class _FakeProc:
    """已退出进程:poll() 返回 0(看门狗见进程已退出,直走 wait/return)。"""
    returncode = 0
    pid = -1
    stdout = None
    _ar_err = None  # invoke 挂上的 per-call err 句柄

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


class ThreadBackend(ClaudeCodeBackend):
    """ClaudeCodeBackend 但 invoke 捕获 subprocess env + 挂 per-call err,不真起子进程。

    复用父类的 ``_build_env``(真实 subprocess-env 构造)、``result_ok``/``is_complete``
    /``session_id``/``result_body``/``result_text``/``model_args`` 等真实实现 —— 故本
    测试端到端验证了 ① env 隔离 与 ② err 脱钩 的真实路径。
    """

    def __init__(self, config=None):
        super().__init__(config=config)
        self.records: list[tuple] = []  # (thread_name, env, err_handle, proc)
        self._lock = threading.Lock()

    def invoke(self, prompt, prefix, argv, key_ctx=None):
        env = self._build_env(key_ctx)  # 真实 subprocess env: {**os.environ, **extra_env} or None
        err_path = f"{prefix}.err"
        ensure_parent(err_path)
        err = open(err_path, "w")  # per-call, 挂到 proc(非实例属性)
        with open(f"{prefix}.jsonl", "w") as f:
            f.write(json.dumps(_result("ok")) + "\n")
        proc = _FakeProc()
        proc._ar_err = err
        with self._lock:
            self.records.append((threading.current_thread().name, env, err, proc))
        return proc

    def stream(self, proc, prefix):
        # 镜像真 stream 的 finally:关 per-call err(挂在 proc 上,非实例属性)
        err = getattr(proc, "_ar_err", None)
        if err is not None and not err.closed:
            err.close()


class ThreadFakeKP:
    """按线程名返回不同 key 的假密钥池 —— 模拟多线程各拿各的 key。

    无共享可变状态(``current_thread().name`` 线程本地),天然线程安全。
    """

    @staticmethod
    def _name():
        return threading.current_thread().name

    def init(self) -> KeyContext:
        return KeyContext(key=f"key-{self._name()}",
                          primary_model=f"m-{self._name()}",
                          base_url=f"https://{self._name()}.test")

    def rotate(self) -> KeyContext:
        return KeyContext(key=f"key-{self._name()}-r")

    def on_success(self) -> KeyContext:
        return KeyContext()

    def disable(self) -> None:
        pass

    def available_size(self) -> int:
        return 2

    def react(self, t) -> str:
        return "stop"

    def classify(self, t) -> str:
        return "rotate"


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("AR_RUN_DIR", str(tmp_path))
    monkeypatch.setenv("AR_BACKEND", "claude-code")
    monkeypatch.setenv("AR_STALL_TIMEOUT", "5")
    monkeypatch.setenv("AR_TOTAL_TIMEOUT", "0")
    from agent_runner import config as _cfg
    _cfg._reset_default()
    eng._reset_default_runner()
    yield
    _cfg._reset_default()
    eng._reset_default_runner()


# ── ①② 并发:各线程 key/env 隔离、err 句柄不互相 clobber ──────────────────

def test_concurrent_threads_isolated_keys_and_env(monkeypatch):
    N = 8
    backend = ThreadBackend()
    kp = ThreadFakeKP()
    monkeypatch.setattr(eng.Runner, "_get_backend", lambda self: backend)
    monkeypatch.setattr(eng.Runner, "_ensure_keypool", lambda self: kp)

    results = {}
    errors = []

    def worker(i):
        try:
            res = eng.agent_with_retry(f"prompt-{i}", f"log{i}")
            results[i] = res
        except Exception as e:  # noqa: BLE001
            errors.append((i, repr(e)))

    threads = [threading.Thread(target=worker, args=(i,), name=f"T{i}") for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"threads raised: {errors}"
    assert all(results[i].rc == 0 for i in range(N)), {i: results[i].rc for i in range(N)}

    # 每线程恰好一次 invoke
    assert len(backend.records) == N
    by_thread = {name: (env, err, proc) for (name, env, err, proc) in backend.records}

    for i in range(N):
        name = f"T{i}"
        assert name in by_thread, f"missing thread {name} in {set(by_thread)}"
        env, err, proc = by_thread[name]
        # ① env 隔离:每线程的 agent 子进程 env 里是自己的 key,无跨线程串 key
        assert env is not None, f"{name}: subprocess env should be built (key present)"
        assert env["ANTHROPIC_AUTH_TOKEN"] == f"key-{name}", \
            f"{name}: key leaked/incorrect -> {env.get('ANTHROPIC_AUTH_TOKEN')}"
        assert env["ANTHROPIC_BASE_URL"] == f"https://{name}.test"
        # ② err 脱钩:句柄挂在 proc(per-call),非 backend 实例属性;各线程独立对象
        assert proc._ar_err is err
        assert err.closed, f"{name}: stream 应已关闭 per-call err 句柄"

    # err 句柄是 N 个独立对象(无单例 clobber)
    errs = [err for (_n, _e, err, _p) in backend.records]
    assert len({id(e) for e in errs}) == N, "err handles must be distinct objects"
    assert len({id(p) for (_n, _e, _er, p) in backend.records}) == N, "procs distinct"


# ── ③ 模块级函数委托 thread-local 默认 Runner(每线程独立实例) ─────────────

def test_module_level_default_runner_is_thread_local():
    seen = {}
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()  # 让两线程几乎同时取默认 Runner
        # 持对象引用(非 id())——id() 在 joined 线程间不稳定:线程退出后其
        # thread-local ref 释放,对象被 GC,id 可能被复用,造成假等。
        seen[threading.current_thread().name] = eng._default_runner()

    t1 = threading.Thread(target=worker, name="A")
    t2 = threading.Thread(target=worker, name="B")
    t1.start(); t2.start()
    t1.join(); t2.join()

    # 每线程各得一个独立 Runner 实例(identity 比较,对象经 seen 持活故稳定)
    assert seen["A"] is not seen["B"], "each thread must get its own default Runner instance"
    # 同线程内复取一致
    assert eng._default_runner() is eng._default_runner()


# ── ④ Runner(config_overrides=...) 各自隔离配置(多 Agent 微调) ─────────

def test_runners_with_config_overrides_isolate_config():
    r1 = eng.Runner(config_overrides={"backend": "opencode", "primary_model": "glm-4.7",
                                      "stall_timeout": 600})
    r2 = eng.Runner(config_overrides={"backend": "claude-code", "primary_model": "glm-5.1",
                                      "stall_timeout": 50})
    assert r1._config.get("backend") == "opencode"
    assert r2._config.get("backend") == "claude-code"
    assert r1._config.get("primary_model") == "glm-4.7"
    assert r2._config.get("primary_model") == "glm-5.1"
    assert r1._config.get("stall_timeout") == 600
    assert r2._config.get("stall_timeout") == 50
    # 互不污染
    assert r1._config is not r2._config


def test_runner_config_overrides_beat_env(monkeypatch):
    """Runner config_overrides 优先于 AR_ env(进程级),实现同进程内多 Agent 微调。"""
    monkeypatch.setenv("AR_PRIMARY_MODEL", "env-model")
    r = eng.Runner(config_overrides={"primary_model": "instance-model"})
    assert r._config.get("primary_model") == "instance-model"
    # 模块级默认 Runner(无 config_overrides)仍吃 env
    eng._reset_default_runner()
    assert eng._default_runner()._config.get("primary_model") == "env-model"
