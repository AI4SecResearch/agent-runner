"""OpenCode backend — Python counterpart of ``backends/opencode.sh``.

Owns everything OpenCode-specific: the ``opencode`` binary, the ``run``
subcommand, the positional prompt, the ``--format json`` output, how the jsonl
log is parsed, and the flag vocabulary (``--model``/``-s``/``--fork``). The
engine never references ``opencode`` directly.

Env vars (agent-agnostic; the key pool overrides per provider):
  SANDBOX            "1" → --dangerously-skip-permissions
  PRIMARY_MODEL      Primary model id, provider-prefixed (default bailian/glm-5.2)
  DOWNGRADE_MODEL    Downgrade-tier model id (default bailian/glm-5.1)

Auth: OpenCode reads the API key from the env var configured in
~/.config/opencode/opencode.json (default Z_AI_API_KEY). Endpoint/protocol
live in opencode.json too, so ``base_url_env_var`` returns empty (the key
pool's per-provider base_url is unused for opencode).
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

from ..landlock import wrap as _landlock_wrap
from ._jsonl import ensure_parent as _ensure_parent
from ._jsonl import first_matching, iter_lines, read_jsonl

# Defaults live in the backend so generic code stays agent-agnostic (mirrors
# backends/opencode.sh's `:-bailian/glm-5.2` / `:-bailian/glm-5.1`). Resolved
# inside model_args at call time (NOT at import) so both backends can be
# loaded in one process without clobbering each other's defaults.
_DEFAULT_PRIMARY = "bailian/glm-5.2"
_DEFAULT_DOWNGRADE = "bailian/glm-5.1"


class OpencodeBackend:
    """OpenCode — ``run`` subcommand, ``--format json`` event stream."""

    agent_id = "opencode"

    # ── invoke (mirrors agent_backend_invoke) ─────────────────────────────
    def invoke(self, prompt: str, prefix: str, argv: list[str]):
        """Start ``opencode run <prompt> --format json``; open <prefix>.err for
        stderr; return the ``Popen`` without waiting. The watchdog streams
        stdout to <prefix>.jsonl on a reader thread, then polls/waits/kills."""
        err_path = f"{prefix}.err"
        _ensure_parent(err_path)  # defensive: callers normally create OUTPUT_DIR
        self._err = open(err_path, "w")  # kept open until proc finishes
        from ..platform import PLATFORM
        cmd = _landlock_wrap(["opencode", "run", prompt, "--format", "json", *argv])
        return subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=self._err, text=True,
            **PLATFORM.new_session_kwargs(),
        )

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
            if hasattr(self, "_err") and not self._err.closed:
                self._err.close()

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
        # Non-sandbox emits nothing — OpenCode's permission model is in
        # opencode.json, not a CLI flag.
        if os.environ.get("SANDBOX") == "1":
            return ["--dangerously-skip-permissions"]
        return []

    def model_args(self, tier: str) -> list[str]:
        if tier == "primary":
            m = os.environ.get("PRIMARY_MODEL") or _DEFAULT_PRIMARY
        elif tier == "downgrade":
            m = os.environ.get("DOWNGRADE_MODEL") or _DEFAULT_DOWNGRADE
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
        return os.environ.get("OPENCODE_AUTH_ENV_VAR", "Z_AI_API_KEY")

    def base_url_env_var(self) -> str:
        # Empty ⇒ key pool skips base_url export: OpenCode routes by the
        # provider prefix in the model id, endpoint/protocol live in opencode.json.
        return ""


# ── module-local helpers ───────────────────────────────────────────────────
import json

# _landlock_wrap is imported at the top (from ..landlock import wrap as _landlock_wrap).


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
