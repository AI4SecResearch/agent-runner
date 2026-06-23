"""Shared preliminary parsing for the provider layer.

extract_signals() turns an agent's structured error payload into normalized
signals ({message, code, status}) that provider modules interpret. This is the
"preliminary parse" step common to all providers.
"""
import re

# [NNN] (HTTP status) or [NNNN] (upstream codes such as zhipu's 1305).
_CODE_RE = re.compile(r"\[(\d{3,4})\]")


def extract_signals(payload):
    """Derive normalized signals from the agent's error payload.

    Uses payload["code"] if the agent surfaced an upstream code; otherwise
    falls back to a [NNN/NNNN] match in the message (e.g. claude-code renders
    the provider error inline in its result text).
    """
    message = (payload.get("message") or "").strip()
    code = payload.get("code")
    if not code:
        m = _CODE_RE.search(message[:500])
        code = m.group(1) if m else None
    return {"message": message, "code": code, "status": payload.get("status")}
