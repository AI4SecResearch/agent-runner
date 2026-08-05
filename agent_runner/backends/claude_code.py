"""Claude Code backend — Python counterpart of ``backends/claude-code.sh``.

Owns everything Claude-Code-specific: the ``claude`` binary, the ``-p`` prompt
flag, the ``stream-json`` output format, how the jsonl log is parsed, and the
flag vocabulary (``--model``/``--resume``/``--fork-session``/``--permission-mode``).
The engine never references ``claude`` directly — same isolation as the bash
side.

Env vars (agent-agnostic; the key pool overrides per provider):
  AR_SKIP_PERMISSIONS  → --dangerously-skip-permissions
  AR_PRIMARY_MODEL     模型 id(经 config 层;keypool 按供应校验覆盖)
  AR_DOWNGRADE_MODEL   降级模型 id
  ANTHROPIC_BASE_URL   base_url(由 `claude` 二进制读;keypool 写)
"""

from __future__ import annotations

import os
import subprocess

from ._jsonl import ensure_parent as _ensure_parent
from ._jsonl import first_matching, iter_lines, read_jsonl

# ── env vars Claude Code reads natively (reuse lpm's constants when lpm is
#    importable; fall back to literals so the backend works even if lpm isn't
#    on sys.path yet — the engine wires lpm in before any key-pool op, but the
#    backend's api_key/base_url env-var names are needed at invoke time).
try:
    from llm_provider_manager.agents.claude import (  # type: ignore
        ANTHROPIC_AUTH_TOKEN_VAR as _AUTH_VAR,
        ANTHROPIC_BASE_URL_VAR as _BASE_URL_VAR,
    )
except Exception:  # pragma: no cover - lpm not yet importable at module load
    _AUTH_VAR = "ANTHROPIC_AUTH_TOKEN"
    _BASE_URL_VAR = "ANTHROPIC_BASE_URL"


# 模型名不在此写死——经 config 层取(AR_PRIMARY_MODEL/AR_DOWNGRADE_MODEL,配置文件或 env),
# keypool 按供应校验覆盖。无 config 值则 model_args 返回空(由调用方/agent 处理)。


class ClaudeCodeBackend:
    """Claude Code — anthropic-protocol only, ``stream-json`` output."""

    agent_id = "claude"

    def __init__(self, config=None):
        # Per-Runner Config (multi-thread isolation). None → module default
        # (back-compat / process mode). Resolved once at construction.
        if config is None:
            from .. import config as _cfg_mod
            config = _cfg_mod._default()
        self._config = config

    # ── invoke (mirrors agent_backend_invoke) ─────────────────────────────
    def invoke(self, prompt: str, prefix: str, argv: list[str], key_ctx=None):
        """Start ``claude -p <prompt> --output-format stream-json --verbose``;
        open <prefix>.err for stderr; build an isolated subprocess env (extra env
        vars from ``key_ctx``: key → ANTHROPIC_AUTH_TOKEN, base_url →
        ANTHROPIC_BASE_URL) and pass ``env=`` to ``Popen`` so the agent subprocess
        gets its own key snapshot with zero cross-thread env races. Returns the
        ``Popen`` without waiting. The err handle is attached to the proc
        (``proc._ar_err``), not stored on the instance — so concurrent invocations
        never clobber each other's err handle. ``key_ctx=None`` → inherits
        ``os.environ`` as-is.
        """
        err_path = f"{prefix}.err"
        # Ensure the output directory exists (defensive — callers normally
        # create AR_RUN_DIR, but a missing parent shouldn't crash the run).
        _ensure_parent(err_path)
        err = open(err_path, "w")  # attached to proc; stream closes it
        from ..platform import PLATFORM
        cmd = [
            "claude", "-p", prompt,
            "--output-format", "stream-json", "--verbose",
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
        """``{**os.environ, **extra_env}`` from key_ctx, or None (inherit env)."""
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
        jsonl_path = f"{prefix}.jsonl"
        try:
            with open(jsonl_path, "w") as jl:
                assert proc.stdout is not None
                for line in proc.stdout:
                    jl.write(line)
                    jl.flush()  # 结果行一落地,is_complete 即可见
        finally:
            # per-call err handle (attached by invoke), NOT an instance attr —
            # concurrent invocations each close their own.
            err = getattr(proc, "_ar_err", None)
            if err is not None and not err.closed:
                err.close()

    # ── 成功运行的结果文本 ────────────────────────────────────────────────
    def result_body(self, prefix: str) -> str:
        """成功 result 事件的 .result(取首个);无则空串。"""
        obj = first_matching(f"{prefix}.jsonl", lambda o: o.get("type") == "result")
        if obj is None:
            return ""
        r = obj.get("result")
        return str(r) if r is not None else ""

    # ── 看门狗早退判定 ───────────────────────────────────────────────────
    def is_complete(self, prefix: str) -> bool:
        jsonl = f"{prefix}.jsonl"
        # bash uses `grep -q '"type":"result"'` — a raw substring scan, no
        # json parse (fast path). We match that to stay byte-equivalent on
        # the early-exit trigger.
        for line in iter_lines(jsonl):
            if '"type":"result"' in line or '"type": "result"' in line:
                return True
        return False

    # ── success/failure (mirrors agent_backend_result_ok) ────────────────
    def result_ok(self, prefix: str) -> bool:
        jsonl = f"{prefix}.jsonl"
        if not os.path.exists(jsonl) or os.path.getsize(jsonl) == 0:
            return False
        # jq -se 'any(.[]; .type=="result" and ((.is_error // false) | not))'
        return any(
            obj.get("type") == "result" and not (obj.get("is_error") or False)
            for obj in read_jsonl(jsonl)
        )

    # ── error payload (mirrors agent_backend_result_text) ─────────────────
    def result_text(self, prefix: str) -> str:
        jsonl = f"{prefix}.jsonl"
        # jq -c 'select(.type=="result") | {message: .result}' | head -1
        obj = first_matching(jsonl, lambda o: o.get("type") == "result")
        if obj is None:
            return ""
        # Compact JSON, matching `jq -c`. message may be missing if result is null.
        msg = obj.get("result")
        return json_compact({"message": msg if msg is not None else ""})

    # ── session id (mirrors agent_backend_session_id) ─────────────────────
    def session_id(self, prefix: str) -> str:
        jsonl = f"{prefix}.jsonl"
        # jq -r 'select(.session_id != null) | .session_id' | head -1
        obj = first_matching(jsonl, lambda o: o.get("session_id") is not None)
        return str(obj["session_id"]) if obj else ""

    # ── flag fragments ────────────────────────────────────────────────────
    def perm_args(self) -> list[str]:
        if self._config.get("skip_permissions", False):
            return ["--dangerously-skip-permissions"]
        return ["--permission-mode", "acceptEdits"]

    def model_args(self, tier: str, resolved_model: str = "",
                   *, provider_id: str = "") -> list[str]:
        # resolved_model (keypool-resolved, multi-thread path) 优先于 config 层,
        # 绕过 AR_* env 回环。空 resolved_model → 回落 config(启动期 AR_* env / 配置文件)。
        # provider_id 对 claude 无意义——其 --model 接受裸 id(由
        # ANTHROPIC_BASE_URL 路由到对应 provider),故忽略。
        if resolved_model:
            m = resolved_model
        elif tier == "primary":
            m = self._config.get("primary_model", "")
        elif tier == "downgrade":
            m = self._config.get("downgrade_model", "")
        else:
            m = tier  # bare model id
        return ["--model", m] if m else []

    def resume_args(self, sid: str) -> list[str]:
        return ["--resume", sid] if sid else []

    def fork_args(self, sid: str) -> list[str]:
        r = self.resume_args(sid)
        return r + ["--fork-session"] if r else []

    # ── env-var names ─────────────────────────────────────────────────────
    def api_key_env_var(self) -> str:
        return _AUTH_VAR

    def base_url_env_var(self) -> str:
        return _BASE_URL_VAR


# ── module-local helpers ───────────────────────────────────────────────────
import json as _json


def json_compact(obj: dict) -> str:
    """紧凑 JSON(无分隔符后空格),对齐 jq -c 的输出。"""
    return _json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
