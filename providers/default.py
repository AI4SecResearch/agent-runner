"""Generic fallback provider.

Maps the extracted error code to a recovery action using the supplied
error_handling override (code -> action, "_default" fallback), falling back to
DEFAULT_ACTION when neither matches. Unlike provider-specific modules, this
generic fallback ships no built-in code table — it relies entirely on the
override (or the default action). Used when no provider module is registered.
"""

DEFAULT_ACTION = "rotate_then_downgrade"


def classify(signals, error_handling):
    code = signals.get("code")
    action = error_handling.get(code, error_handling.get("_default", DEFAULT_ACTION))
    disable = "true" if "rotate" in action else "false"
    return f"{action}:{disable}"
