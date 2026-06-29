"""LLM provider manager.

Manage multiple LLM service providers and switch which LLM (provider + key)
a given agent uses in the current shell.

Two pluggable axes (mirrors agent-runner's backends/ + providers/ design):
  * agents/    — per-agent env contract + config rendering (claude, opencode)
  * providers/ — per-provider error classification with built-in defaults

The ``use`` command is agent-scoped: it exports only the selected agent's
env vars for a chosen (provider, key, model).
"""

__version__ = "0.2.0"
