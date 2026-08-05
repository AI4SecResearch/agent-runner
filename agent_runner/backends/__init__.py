"""Agent backends — the Python counterpart of ``backends/*.sh``.

Each backend implements the 11-op contract consumed by ``engine.py`` (the same
ops ``runner.sh`` calls on its bash backends), but with ``jq`` replaced by the
``json`` stdlib and ``tee | jq`` pipelines replaced by a streaming
``subprocess.Popen`` reader. ``$AGENT_BACKEND`` selects the active one, default
``claude-code`` — same source as the bash side.

The registry mirrors ``runner.py``'s ``_BACKEND_TO_AGENT`` map + the bash
``backends/<name>.sh`` filename convention. Adding a backend is a new module
here + one ``REGISTRY`` entry; the engine never references a concrete agent.
"""

from __future__ import annotations

from typing import Protocol

from .claude_code import ClaudeCodeBackend
from .opencode import OpencodeBackend


class Backend(Protocol):
    """The 11-op contract (mirrors ``agent_backend_*`` in ``backends/*.sh``).

    ``invoke`` writes ``<prefix>.jsonl`` + ``<prefix>.err`` and returns the
    ``Popen`` (NOT waited on); the watchdog streams stdout to ``<prefix>.jsonl``
    via ``stream``, polls, and either waits or kills. ``prefix`` is a full path
    (the engine constructs it as ``$OUTPUT_DIR/$log_name``).

    Each backend is instantiated per-``Runner`` with that Runner's ``Config``
    instance (``__init__(self, config=None)``), so multi-threaded callers each
    get an isolated backend with its own config view. Backends hold NO shared
    mutable state — per-call handles (e.g. the err file) are attached to the
    returned ``Popen`` object (``proc._ar_err``), never to instance attributes.
    """

    # the lpm agent id this backend maps to (claude-code → "claude", etc.)
    agent_id: str

    def __init__(self, config=None):
        """Accept the owning Runner's ``Config`` (None → module default)."""
        ...

    def invoke(self, prompt: str, prefix: str, argv: list[str], key_ctx=None):
        """Start one agent step. Opens <prefix>.err for the agent's stderr,
        builds an isolated subprocess env (extra env vars from ``key_ctx``:
        key/base_url → this backend's env-var names) and passes ``env=`` to
        ``Popen`` so each agent subprocess gets its own key snapshot with zero
        cross-thread env races. Returns the ``Popen`` WITHOUT waiting (the
        watchdog streams stdout to <prefix>.jsonl via ``stream``, polls, and
        either waits or kills).

        ``key_ctx=None`` (no key pool) → ``Popen`` inherits ``os.environ`` as-is
        (current behavior). Mirrors bash's ``_agent_once … &`` (backgrounded) —
        the watchdog owns the process's lifecycle so it can kill on timeout.
        Success is NOT judged from the returncode (see ``result_ok``)."""
        ...

    def stream(self, proc, prefix: str) -> None:
        """把 ``proc.stdout`` 逐行写入 <prefix>.jsonl(结果行一落地,
        ``is_complete`` 即可见)。**不**打印到 stdout——stdout 留给进程形态的
        session_id 行,结果文本由 ``result_body`` 按需从日志读回。在 reader
        线程上跑,阻塞至 stdout EOF。finally 关闭 ``proc._ar_err``(由 invoke
        挂上的 per-call 句柄,非实例属性——并发安全)。"""
        ...

    def is_complete(self, prefix: str) -> bool:
        """agent 是否已写出终态结果(看门狗早退依据)。一旦为真即视为本次运
        行完成,不等进程自身退出。"""
        ...

    def result_body(self, prefix: str) -> str:
        """成功运行的结果文本(claude 取 result 事件的 .result;opencode 取所有
        text 事件的 .part.text 拼接)。供编排层填入 ``Result.text``——与
        session_id 同源(都从成功日志读回),单一真相、不依赖流式 sink。失败或
        无结果时返回空串。"""
        ...

    def result_ok(self, prefix: str) -> bool:
        """Did the run succeed? (mirrors ``agent_backend_result_ok``)"""
        ...

    def result_text(self, prefix: str) -> str:
        """The error payload (JSON string) fed to the provider layer.

        Mirrors ``agent_backend_result_text`` — produces a JSON object with
        ``{message, code?, status?}``; the provider module parses it."""
        ...

    def session_id(self, prefix: str) -> str:
        """The session id the agent recorded (empty = none/unsupported)."""
        ...

    def perm_args(self) -> list[str]:
        """Permission flag fragment (e.g. --dangerously-skip-permissions)."""
        ...

    def model_args(self, tier: str, resolved_model: str = "",
                   *, provider_id: str = "") -> list[str]:
        """Model flag fragment; tier is ``primary``/``downgrade``/a bare id.

        ``resolved_model`` (the keypool's resolved model id, from ``KeyContext``)
        takes priority over the config layer — this is the multi-thread path
        that bypasses the ``AR_*`` env round-trip. Empty → fall back to config
        (the startup-time ``AR_PRIMARY_MODEL``/``AR_DOWNGRADE_MODEL`` env or config file).

        ``provider_id`` (the keypool's resolved provider id, keyword-only) lets a
        backend qualify the model id for agents that require a provider prefix
        (e.g. opencode's ``provider/model``). Backends whose ``--model`` takes a
        bare id ignore it."""
        ...

    def resume_args(self, sid: str) -> list[str]:
        """Resume flags (empty list ⇒ backend opts out ⇒ degrade to new session)."""
        ...

    def fork_args(self, sid: str) -> list[str]:
        """All flags to FORK from a session (resume + fork). Empty list when
        ``sid`` is empty ⇒ degrade, same convention as ``resume_args``."""
        ...

    def api_key_env_var(self) -> str:
        """The env var this agent reads for its API key (the extra_env key)."""
        ...

    def base_url_env_var(self) -> str:
        """The env var this agent reads for its base_url (empty ⇒ skip extra_env)."""
        ...


# REGISTRY maps names to CLASSES (not singletons): each Runner instantiates its
# own backend with its own Config, so multi-threaded callers are isolated.
REGISTRY: dict[str, type] = {
    "claude-code": ClaudeCodeBackend,
    "opencode": OpencodeBackend,
}


def get_backend(name: str | None) -> Backend:
    """Look up a backend by ``$AGENT_BACKEND`` value; return a default-config
    INSTANCE (backward-compat for module-level / external callers). The Runner
    constructs its own per-instance backend via ``REGISTRY[name](config=...)``."""
    if not name or name not in REGISTRY:
        import sys

        default = "claude-code"
        if name and name not in REGISTRY:
            sys.stderr.write(
                f"agent_runner.backends: unknown AGENT_BACKEND '{name}', "
                f"defaulting to '{default}'\n"
            )
        return REGISTRY[default]()
    return REGISTRY[name]()


def known_backends() -> list[str]:
    return list(REGISTRY)
