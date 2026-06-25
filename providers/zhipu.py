"""Zhipu (zhipu.ai / BigModel) provider.

Owns zhipu's error-code → recovery-action table. The upstream 4-digit code
(e.g. 1305) is surfaced by the agents and resolved by base.extract_signals, so
no provider-specific extraction is needed — only the action mapping.

config[providers][zhipu][error_handling] (optional) overrides these defaults
per deployment; absence falls back to DEFAULT_ERROR_HANDLING below.
"""
from .default import DEFAULT_ACTION

# Zhipu upstream error codes → recovery action (see BigModel error docs).
DEFAULT_ERROR_HANDLING = {
    "1305": "downgrade",             # quota exhausted → fall to downgrade tier
    "1308": "rotate_key",            # rate limited → try another key
    "1310": "rotate_key",
    "_default": "rotate_then_downgrade",
}


def classify(signals, error_handling):
    """Map an extracted error code to 'action:disable'.

    Merges any per-deployment error_handling override over the zhipu defaults,
    then resolves the code (with "_default" / DEFAULT_ACTION fallbacks).
    """
    table = {**DEFAULT_ERROR_HANDLING, **(error_handling or {})}
    code = signals.get("code")
    action = table.get(code, table.get("_default", DEFAULT_ACTION))
    disable = "true" if "rotate" in action else "false"
    return f"{action}:{disable}"
