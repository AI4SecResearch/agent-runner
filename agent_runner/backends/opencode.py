"""OpenCode backend — Python counterpart of ``backends/opencode.sh``.

Owns everything OpenCode-specific: the ``opencode`` binary, the ``run``
subcommand, the positional prompt, the ``--format json`` output, how the jsonl
log is parsed, and the flag vocabulary (``--model``/``-s``/``--fork``). The
engine never references ``opencode`` directly.

Env vars (agent-agnostic; the key pool overrides per provider):
  AR_SANDBOX          → --dangerously-skip-permissions
  AR_PRIMARY_MODEL    模型 id(provider 前缀形式,如 bailian/glm-5.2;经 config 层)
  AR_DOWNGRADE_MODEL  降级模型 id

Auth: OpenCode reads the API key from the env var configured in
~/.config/opencode/opencode.json (default Z_AI_API_KEY). Endpoint/protocol
live in opencode.json too, so ``base_url_env_var`` returns empty (the key
pool's per-provider base_url is unused for opencode).
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

from ._jsonl import ensure_parent as _ensure_parent
from ._jsonl import first_matching, iter_lines, read_jsonl


# 模型名不在此写死——经 config 层取(AR_PRIMARY_MODEL/AR_DOWNGRADE_MODEL)。
class OpencodeBackend:
    """OpenCode — ``run`` subcommand, ``--format json`` event stream."""

    agent_id = "opencode"

    def __init__(self, config=None):
        # Per-Runner Config (multi-thread isolation). None → module default.
        if config is None:
            from .. import config as _cfg_mod
            config = _cfg_mod._default()
        self._config = config

    # ── invoke (mirrors agent_backend_invoke) ─────────────────────────────
    def invoke(self, prompt: str, prefix: str, argv: list[str], key_ctx=None):
        """Start ``opencode run <prompt> --format json``; open <prefix>.err for
        stderr; build an isolated subprocess env (extra env vars from
        ``key_ctx``: key → the opencode-auth env var) and pass ``env=`` to
        ``Popen`` so the agent subprocess gets its own key snapshot. Returns the
        ``Popen`` without waiting. The err handle is attached to the proc
        (``proc._ar_err``), not the instance — concurrent invocations never
        clobber each other. ``key_ctx=None`` → inherits ``os.environ`` as-is."""
        err_path = f"{prefix}.err"
        _ensure_parent(err_path)  # defensive: callers normally create AR_RUN_DIR
        err = open(err_path, "w")  # attached to proc; stream closes it
        from ..platform import PLATFORM
        cmd = ["opencode", "run", prompt, "--format", "json", *argv]
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
        # base_url_env_var() is "" for opencode (routes by provider prefix) → skip
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
                    jl.flush()  # 终态事件一落地,is_complete 即可见
        finally:
            err = getattr(proc, "_ar_err", None)
            if err is not None and not err.closed:
                err.close()

    # ── 成功运行的结果文本 ────────────────────────────────────────────────
    def result_body(self, prefix: str) -> str:
        """所有 text 事件的 .part.text 顺序拼接;无则空串。"""
        parts = []
        for obj in read_jsonl(f"{prefix}.jsonl"):
            if obj.get("type") == "text":
                part = obj.get("part")
                t = part.get("text") if isinstance(part, dict) else None
                if t is not None:
                    parts.append(str(t))
        return "".join(parts)

    # ── 看门狗早退判定 ───────────────────────────────────────────────────
    def is_complete(self, prefix: str) -> bool:
        jsonl = f"{prefix}.jsonl"
        # jq -e 'select(.type == "error" or (.type == "step_finish" and
        #         (.part.reason == "stop" or .part.reason == null)))'
        for line in iter_lines(jsonl):
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if _is_terminal(obj):
                return True
        return False

    # ── success/failure (mirrors agent_backend_result_ok) ────────────────
    def result_ok(self, prefix: str) -> bool:
        jsonl = f"{prefix}.jsonl"
        if not os.path.exists(jsonl) or os.path.getsize(jsonl) == 0:
            return False
        objs = read_jsonl(jsonl)
        has_terminal = any(
            obj.get("type") == "step_finish"
            and (_reason(obj) in ("stop", None))
            for obj in objs
        )
        no_error = all(obj.get("type") != "error" for obj in objs)
        return has_terminal and no_error

    # ── error payload (mirrors agent_backend_result_text) ─────────────────
    def result_text(self, prefix: str) -> str:
        jsonl = f"{prefix}.jsonl"
        # jq -c 'select(.type == "error") | {message: .error.data.message,
        #   code: (try (.error.data.responseBody | fromjson | .error.code) catch null),
        #   status: .error.data.statusCode}' | head -1
        obj = first_matching(jsonl, lambda o: o.get("type") == "error")
        if obj is None:
            return ""
        data = _dig(obj, "error", "data") or {}
        message = data.get("message")
        code: Any = None
        rb = data.get("responseBody")
        if isinstance(rb, str):
            try:
                parsed = json.loads(rb)
                code = _dig(parsed, "error", "code")
            except json.JSONDecodeError:
                code = None
        status = data.get("statusCode")
        return json_compact({
            "message": message if message is not None else "",
            "code": code,
            "status": status,
        })

    # ── session id (mirrors agent_backend_session_id) ─────────────────────
    def session_id(self, prefix: str) -> str:
        jsonl = f"{prefix}.jsonl"
        # jq -r 'select(.sessionID != null) | .sessionID' | head -1
        obj = first_matching(jsonl, lambda o: o.get("sessionID") is not None)
        return str(obj["sessionID"]) if obj else ""

    # ── flag fragments ────────────────────────────────────────────────────
    def perm_args(self) -> list[str]:
        # 非 sandbox 不输出——OpenCode 的权限模型在 opencode.json 里,非 CLI flag。
        if self._config.get("sandbox", False):
            return ["--dangerously-skip-permissions"]
        return []

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
        return ["-s", sid] if sid else []

    def fork_args(self, sid: str) -> list[str]:
        r = self.resume_args(sid)
        return r + ["--fork"] if r else []

    # ── env-var names ─────────────────────────────────────────────────────
    def api_key_env_var(self) -> str:
        return self._config.get("opencode_auth_env_var", "Z_AI_API_KEY")

    def base_url_env_var(self) -> str:
        # Empty ⇒ key pool skips base_url export: OpenCode routes by the
        # provider prefix in the model id, endpoint/protocol live in opencode.json.
        return ""


# ── module-local helpers ───────────────────────────────────────────────────
import json


def _reason(obj: dict) -> Any:
    """``.part.reason`` (may be absent → None, matching jq's null)."""
    part = obj.get("part")
    if isinstance(part, dict):
        return part.get("reason")
    return None


def _is_terminal(obj: dict) -> bool:
    """Mirror of the is_complete jq predicate (one event)."""
    t = obj.get("type")
    if t == "error":
        return True
    if t == "step_finish":
        return _reason(obj) in ("stop", None)
    return False


def _dig(obj: Any, *keys: str) -> Any:
    """Nested-dict traversal with None fallback (mirrors jq's null on missing)."""
    cur: Any = obj
    for k in keys:
        if isinstance(cur, dict):
            cur = cur.get(k)
        else:
            return None
    return cur


def json_compact(obj: dict) -> str:
    """紧凑 JSON(无分隔符后空格),对齐 jq -c 的输出。"""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
