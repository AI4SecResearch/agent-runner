"""Generic fallback provider.

Maps the extracted error code to a recovery action via the provider's
error_handling config (code -> action, "_default" fallback). Used when no
provider-specific module is registered (or as the base behavior).
"""

DEFAULT_ACTION = "rotate_then_downgrade"


def classify(signals, error_handling):
    code = signals.get("code")
    action = error_handling.get(code, error_handling.get("_default", DEFAULT_ACTION))
    disable = "true" if "rotate" in action else "false"
    return f"{action}:{disable}"
