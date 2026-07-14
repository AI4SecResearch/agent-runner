"""Claude Code backend — Python counterpart of ``backends/claude-code.sh``.

Owns everything Claude-Code-specific: the ``claude`` binary, the ``-p`` prompt
flag, the ``stream-json`` output format, how the jsonl log is parsed, and the
flag vocabulary (``--model``/``--resume``/``--fork-session``/``--permission-mode``).
The engine never references ``claude`` directly — same isolation as the bash
side.

Env vars (agent-agnostic; the key pool overrides per provider):
  SANDBOX             "1" → --dangerously-skip-permissions
  PRIMARY_MODEL       Primary model id (default glm-5-turbo; key pool may override)
  DOWNGRADE_MODEL      Downgrade-tier model id (default glm-4.7)
  ANTHROPIC_BASE_URL  base_url (read by the `claude` binary)
"""

from __future__ import annotations

import os
import subprocess

from ..landlock import wrap as _landlock_wrap
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


# Defaults live in the backend so generic code stays agent-agnostic (mirrors
# backends/claude-code.sh's `:-glm-5-turbo` / `:-glm-4.7`). Resolved inside
# model_args at call time (NOT at import) so that loading both backends into
# the same process doesn't let one clobber the other's defaults in env.
_DEFAULT_PRIMARY = "glm-5-turbo"
_DEFAULT_DOWNGRADE = "glm-4.7"


class ClaudeCodeBackend:
    """Claude Code — anthropic-protocol only, ``stream-json`` output."""

    agent_id = "claude"

    # ── invoke (mirrors agent_backend_invoke) ─────────────────────────────
    def invoke(self, prompt: str, prefix: str, argv: list[str]):
        """Start ``claude -p <prompt> --output-format stream-json --verbose``;
        open <prefix>.err for stderr; return the ``Popen`` without waiting.

        The watchdog calls ``stream(proc, prefix)`` on a reader thread (which
        writes <prefix>.jsonl and prints the result text), then polls/waits/
        kills. Backgrounding is the load-bearing bit — without it the watchdog
        cannot kill a stalled agent.
        """
        err_path = f"{prefix}.err"
        # Ensure the output directory exists (defensive — callers normally
        # create OUTPUT_DIR, but a missing parent shouldn't crash the run).
        _ensure_parent(err_path)
        # Open in a new session so the watchdog can kill the whole process
        # group (claude + any landlock helper) on timeout — mirrors bash's
        # ``pkill -P <job_pid>`` then ``kill <job_pid>`` ordering. The
        # platform kwarg abstracts POSIX (start_new_session) vs Windows.
        self._err = open(err_path, "w")  # kept open until proc finishes
        from ..platform import PLATFORM
        cmd = _landlock_wrap([
            "claude", "-p", prompt,
            "--output-format", "stream-json", "--verbose",
            *argv,
        ])
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
                    jl.flush()  # 结果行一落地,is_complete 即可见
        finally:
            if hasattr(self, "_err") and not self._err.closed:
                self._err.close()

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
        if os.environ.get("SANDBOX") == "1":
            return ["--dangerously-skip-permissions"]
        return ["--permission-mode", "acceptEdits"]

    def model_args(self, tier: str) -> list[str]:
        if tier == "primary":
            m = os.environ.get("PRIMARY_MODEL") or _DEFAULT_PRIMARY
        elif tier == "downgrade":
            m = os.environ.get("DOWNGRADE_MODEL") or _DEFAULT_DOWNGRADE
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
