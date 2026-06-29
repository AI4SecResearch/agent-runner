"""Generic shell-quoting helpers — no agent-specific content.

Everything that names a specific agent (``ANTHROPIC_*`` vars, ``LLM_KEY_*``
naming, npm mappings, protocol preference) lives in the agents package
(``agents/claude.py`` / ``agents/opencode.py``). This module holds only the
shell-quoting utilities shared across all agents.
"""

from __future__ import annotations

import shlex


def sh_export(name: str, value: str) -> str:
    """A single ``export NAME='value'`` line, safely single-quoted."""
    # single-quote; inner single quotes escaped with the standard '\'' trick
    escaped = value.replace("'", "'\"'\"'")
    return f"export {name}='{escaped}'"


def sh_exports(exports: dict[str, str]) -> str:
    return "\n".join(sh_export(k, v) for k, v in exports.items())


def shlex_join_args(args: list[str]) -> str:
    return shlex.join(args)
