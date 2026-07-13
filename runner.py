#!/usr/bin/env python3
"""agent-runner key-pool adapter — delegates to llm-provider-manager (lpm).

lpm is vendored into agent-runner at ``llm-provider-manager/`` (git subtree), so
the default is the in-tree copy. ``$LPM_SRC`` overrides it (e.g. point at a dev
checkout of lpm); the default install path
(``~/.local/share/llm-provider-manager`` — where install.sh clones lpm) is a
last-resort fallback.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))

for _cand in (os.environ.get("LPM_SRC"),                                      # explicit override
              os.path.join(_HERE, "llm-provider-manager", "src"),              # vendored (subtree) — default
              os.path.expanduser("~/.local/share/llm-provider-manager/src")):  # install fallback
    if _cand and os.path.isdir(_cand):
        if _cand not in sys.path:
            sys.path.insert(0, _cand)
        break

try:
    from llm_provider_manager.keypool import dispatch
except ImportError:
    sys.stderr.write(
        "runner.py: llm_provider_manager not found.\n"
        "  Expected the vendored llm-provider-manager/src; or set LPM_SRC=<lpm>/src;\n"
        "  or install lpm (install.sh → ~/.local/share/llm-provider-manager).\n"
    )
    sys.exit(1)

# agent-runner backend name → lpm agent id. Unknown backend falls back to the
# lpm registry default (claude) with a stderr warning.
_BACKEND_TO_AGENT = {"claude-code": "claude", "opencode": "opencode"}


def main(argv):
    backend = os.environ.get("AGENT_BACKEND", "claude-code")
    agent = _BACKEND_TO_AGENT.get(backend, "claude")
    if backend not in _BACKEND_TO_AGENT:
        sys.stderr.write(
            f"runner.py: unknown AGENT_BACKEND '{backend}', defaulting agent='{agent}'\n"
        )
    # Inject --agent right after the subcommand name; argparse accepts
    # interspersed optionals, so this parses cleanly alongside --config/--state.
    return dispatch([argv[0], "--agent", agent, *argv[1:]])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
