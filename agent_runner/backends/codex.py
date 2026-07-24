"""Codex backend — Python counterpart of ``backends/codex.sh``.

Owns everything Codex-specific: the Codex Python SDK driver
(``_codex_runner.py``), the ``run`` subcommand, the ``thread.started`` /
``turn.completed`` compat events, how the jsonl log is parsed, and the flag
vocabulary (``--model``/``--resume``/``--fork-session``/``--sandbox``). The
engine never references ``codex`` directly — same isolation as the other
backends.

Unlike claude-code/opencode (which stream the agent binary's stdout straight
into ``<prefix>.jsonl``), Codex is driven through the Python SDK in a child
process that writes ``<prefix>.jsonl``/``<prefix>.out``/``<prefix>.err``
**itself** (see ``_codex_runner.py``). So ``invoke`` spawns that runner as a
subprocess and ``stream`` only drains its (empty) stdout + closes the per-call
err handle — it must NOT open ``<prefix>.jsonl`` (that would truncate the file
the runner is writing).

Codex's own configuration — web search, network access, the ``codex`` binary,
auth — is the USER's responsibility (codex is expected installed & configured
for non-interactive use). agent-runner only owns its own contract: prompt,
session(resume/fork), model, the event log, and the sandbox mode (bridging
the generic ``AR_SANDBOX`` bool to the Codex SDK ``Sandbox``).

Env vars:
  AR_SANDBOX           bool → Codex sandbox 模式(True=danger-full-access,
                        False=workspace-write,默认;复用通用 sandbox 配置)
  AR_PRIMARY_MODEL     模型 id(经 config 层;keypool 解析覆盖)
  AR_DOWNGRADE_MODEL    降级模型 id

Auth: Codex reads its own credentials (``codex`` binary's config /
``OPENAI_API_KEY``), NOT the Anthropic key pool — hence
``api_key_env_var``/``base_url_env_var`` both return ``""`` and this backend
opts out of key injection. ``agent_id="codex"`` is unknown to vendored lpm
(which only knows claude/opencode); the keypool wrapper tolerates that by
degrading to a no-pool backend (mirrors ``backends/codex.sh``'s
``key_pool_*`` early returns).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from ._jsonl import ensure_parent as _ensure_parent
from ._jsonl import first_matching, read_jsonl

# ── the standalone SDK driver (spawned as a subprocess, same as the bash
#    side's ``python3 codex_runner.py run …``) ──────────────────────────────
_RUNNER_PY = os.path.join(os.path.dirname(__file__), "_codex_runner.py")


# 模型名不在此写死——经 config 层取(AR_PRIMARY_MODEL/AR_DOWNGRADE_MODEL)。
class CodexBackend:
    """Codex — Python-SDK driver subprocess, file-based event contract."""

    agent_id = "codex"

    def __init__(self, config=None):
        # Per-Runner Config (multi-thread isolation). None → module default.
        if config is None:
            from .. import config as _cfg_mod
            config = _cfg_mod._default()
        self._config = config

    # ── invoke (mirrors agent_backend_invoke) ─────────────────────────────
    def invoke(self, prompt: str, prefix: str, argv: list[str], key_ctx=None):
        """Spawn ``python3 _codex_runner.py run --prompt … --output-dir …
        --log-name … --project-root … <argv>``; open <prefix>.err for stderr;
        pass an isolated subprocess env only when a key_ctx carries vars this
        backend reads (it reads none — see ``api_key_env_var``), otherwise the
        child inherits ``os.environ`` so Codex picks up its own auth AND its
        own configuration. Returns the ``Popen`` without waiting. The err
        handle is attached to the proc (``proc._ar_err``), not the instance
        — concurrent invocations never clobber each other.

        ``argv`` carries ``perm_args``(``--sandbox``)+ ``model_args``
        (``--model``)+ caller pass-through(``--resume``/``--fork-session``…).
        No other Codex-side knobs are injected here — the user configures
        codex itself (web search, network access, binary, auth; see module
        docstring)."""
        err_path = f"{prefix}.err"
        _ensure_parent(err_path)  # defensive: callers normally create AR_RUN_DIR
        err = open(err_path, "w")  # attached to proc; stream closes it
        from ..platform import PLATFORM
        cmd = [
            sys.executable, _RUNNER_PY, "run",
            "--prompt", prompt,
            "--output-dir", os.path.dirname(prefix) or ".",
            "--log-name", os.path.basename(prefix),
            "--project-root", str(Path.cwd()),
            *argv,
        ]
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=err, text=True,
            env=self._build_env(key_ctx),
            **PLATFORM.new_session_kwargs(),
        )
        proc._ar_err = err  # per-call; stream's finally closes it
        return proc

    def _build_env(self, key_ctx):
        """``{**os.environ, **extra_env}`` from key_ctx, or None (inherit env).

        Codex reads no key/base_url env var from the pool(api_key_env_var/
        base_url_env_var both ``""``)→ always None → the child inherits
        ``os.environ`` and uses Codex's own auth."""
        if key_ctx is None:
            return None
        extra_env = {}
        key_var = self.api_key_env_var()
        if key_var and key_ctx.key:
            extra_env[key_var] = key_ctx.key
        url_var = self.base_url_env_var()
        if url_var and key_ctx.base_url:
            extra_env[url_var] = key_ctx.base_url
        if not extra_env:
            return None
        return {**os.environ, **extra_env}

    # ── stdout 流式写入 jsonl ─────────────────────────────────────────────
    def stream(self, proc, prefix: str) -> None:
        """_codex_runner.py writes ``<prefix>.jsonl``/``.out`` itself(文件契
        约);stdout 不用——只 drain 它(空,阻塞到子进程退出,让 reader 线程
        活到看门狗 join)并 finally 关 per-call err 句柄。**不** open
        ``<prefix>.jsonl``——那会截断 runner 正在写的文件。"""
        try:
            if proc.stdout is not None:
                for _ in proc.stdout:  # drain(对 codex 为空);EOF 即进程退出
                    pass
        finally:
            err = getattr(proc, "_ar_err", None)
            if err is not None and not err.closed:
                err.close()

    # ── 成功运行的结果文本 ────────────────────────────────────────────────
    def result_body(self, prefix: str) -> str:
        """_codex_runner.py 写入 <prefix>.out 的最终回答文本;无则空串。"""
        out_path = f"{prefix}.out"
        if not os.path.exists(out_path):
            return ""
        try:
            with open(out_path, encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""

    # ── 看门狗早退判定 ───────────────────────────────────────────────────
    def is_complete(self, prefix: str) -> bool:
        jsonl = f"{prefix}.jsonl"
        for obj in read_jsonl(jsonl):
            if obj.get("type") in ("turn.completed", "turn/completed"):
                return True
        return False

    # ── success/failure (mirrors agent_backend_result_ok) ────────────────
    def result_ok(self, prefix: str) -> bool:
        jsonl = f"{prefix}.jsonl"
        if not os.path.exists(jsonl) or os.path.getsize(jsonl) == 0:
            return False
        # jq -se 'any(.[]; (.type=="turn.completed" or .type=="turn/completed")
        #   and ((.status // .turn.status // "completed")=="completed"))'
        for obj in read_jsonl(jsonl):
            if obj.get("type") not in ("turn.completed", "turn/completed"):
                continue
            status = obj.get("status")
            if status is None:
                turn = obj.get("turn")
                status = turn.get("status") if isinstance(turn, dict) else None
            if (status or "completed") == "completed":
                return True
        return False

    # ── error payload (mirrors agent_backend_result_text) ─────────────────
    def result_text(self, prefix: str) -> str:
        err_path = f"{prefix}.err"
        if os.path.exists(err_path) and os.path.getsize(err_path) > 0:
            with open(err_path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            tail = "".join(lines[-40:])
            return json_compact({"message": tail})
        return json_compact({"message": "Codex request failed"})

    # ── session id (mirrors agent_backend_session_id) ─────────────────────
    def session_id(self, prefix: str) -> str:
        jsonl = f"{prefix}.jsonl"
        # jq -r 'select(.type == "thread.started") | .thread_id' | head -1
        obj = first_matching(jsonl, lambda o: o.get("type") == "thread.started")
        tid = obj.get("thread_id") if obj else None
        return str(tid) if tid else ""

    # ── flag fragments ────────────────────────────────────────────────────
    def perm_args(self) -> list[str]:
        # 复用通用 sandbox(bool):True=danger-full-access(无限制),
        # False=workspace-write(默认,与 bash 一致)。_codex_runner.py 再把
        # 模式串映射成 Codex SDK 的 Sandbox 对象。
        mode = "danger-full-access" if self._config.get("sandbox", False) else "workspace-write"
        return ["--sandbox", mode]

    def model_args(self, tier: str, resolved_model: str = "") -> list[str]:
        # resolved_model (keypool-resolved) 优先;空 → 回落 config。
        if resolved_model:
            m = resolved_model
        elif tier == "primary":
            m = self._config.get("primary_model", "")
        elif tier == "downgrade":
            m = self._config.get("downgrade_model", "")
        else:
            m = tier
        return ["--model", m] if m else []

    def resume_args(self, sid: str) -> list[str]:
        return ["--resume", sid] if sid else []

    def fork_args(self, sid: str) -> list[str]:
        r = self.resume_args(sid)
        return r + ["--fork-session"] if r else []

    # ── env-var names ─────────────────────────────────────────────────────
    def api_key_env_var(self) -> str:
        # 空 ⇒ 跳过密钥注入:codex 用自身 auth(codex 二进制配置 / OPENAI_API_KEY)
        return ""

    def base_url_env_var(self) -> str:
        return ""


# ── module-local helpers ───────────────────────────────────────────────────
import json


def json_compact(obj: dict) -> str:
    """紧凑 JSON(无分隔符后空格),对齐 jq -c 的输出。"""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
