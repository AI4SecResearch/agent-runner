"""Claude Code agent backend.

Env contract: Claude Code reads the standard ``ANTHROPIC_*`` variables
natively from the process environment — they are supplied at runtime by
``use``, never baked into ``settings.json``.

  * preferred_protocols = ("anthropic",) — Claude only speaks Anthropic.
  * exports_for fills BASE_URL / AUTH_TOKEN / OPUS_MODEL / SONNET_MODEL;
    a single selected model fills both opus and sonnet slots.
  * render_config writes a fresh settings.json from
    DEFAULT_SETTINGS_TEMPLATE (sensible env + permissions defaults).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from . import ExportResult

if TYPE_CHECKING:
    from ..schema import Config, Key, Provider
    from ..use import UsePlan

# ── env vars Claude Code reads natively ───────────────────────────
ANTHROPIC_BASE_URL_VAR = "ANTHROPIC_BASE_URL"
ANTHROPIC_AUTH_TOKEN_VAR = "ANTHROPIC_AUTH_TOKEN"  # Bearer auth (3rd-party anthropic-compat)
ANTHROPIC_OPUS_MODEL_VAR = "ANTHROPIC_DEFAULT_OPUS_MODEL"
ANTHROPIC_SONNET_MODEL_VAR = "ANTHROPIC_DEFAULT_SONNET_MODEL"

# Built-in template for fresh settings.json generation. ANTHROPIC_* are
# never present here — they come from `use` at runtime.
DEFAULT_SETTINGS_TEMPLATE: dict = {
    "env": {
        "API_TIMEOUT_MS": "3000000",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
    },
    "permissions": {
        "allow": [
            "Bash(curl *)",
            "Bash(grep *)",
            "Bash(wget *)",
            "Bash(git diff *)",
            "Bash(git log *)",
            "Bash(git show *)",
            "Bash(git status *)",
            "Read",
        ]
    },
}


class ClaudeAgent:
    """Claude Code — anthropic-protocol only."""

    id = "claude"
    preferred_protocols = ("anthropic",)
    default_config_path = "~/.claude/settings.local.json"
    config_path_env_var = "LLM_PROVIDER_CLAUDE_OUT"

    def base_url_for(self, provider: "Provider") -> str | None:
        return provider.base_urls.get("anthropic")

    def is_usable(self, provider: "Provider") -> bool:
        return "anthropic" in provider.base_urls

    def config_path_for_dir(self, dir_path: str) -> str:
        """When -o is a directory, preserve Claude Code's .claude/ structure."""
        import os
        return os.path.join(dir_path, ".claude", "settings.local.json")

    def exports_for(
        self,
        config: "Config",
        provider: "Provider",
        key: "Key",
        model: str,
    ) -> ExportResult:
        if not self.is_usable(provider):
            return ExportResult(
                {},
                blocked=True,
                block_reason=(
                    f"provider '{provider.id}' has no anthropic baseURL"
                ),
            )
        if key.is_blacklisted_for("claude"):
            return ExportResult(
                {},
                blocked=True,
                block_reason=(
                    f"key '{key.id}' is blacklisted for claude"
                ),
            )
        return ExportResult({
            ANTHROPIC_BASE_URL_VAR: provider.base_urls["anthropic"],
            ANTHROPIC_AUTH_TOKEN_VAR: key.key,
            ANTHROPIC_OPUS_MODEL_VAR: model,
            ANTHROPIC_SONNET_MODEL_VAR: model,
        })

    # ── config rendering ───────────────────────────────────────────

    def render_config(
        self,
        config: "Config",
        out_path: str,
        selection: "UsePlan | None" = None,
    ) -> tuple[dict, list[str]]:
        """Write settings.local.json from DEFAULT_SETTINGS_TEMPLATE.

        With ``selection`` (inline mode): merges the resolved ANTHROPIC_*
        values into the ``env`` block — a self-contained config that needs
        no ``lpm use`` at runtime.

        Without ``selection`` (template mode): writes the template as-is
        (no ANTHROPIC_*; they come from ``lpm use`` env vars).

        ``config`` is unused. Returns (rendered_dict, []).
        """
        del config  # unused
        p = Path(out_path)
        rendered = json.loads(json.dumps(DEFAULT_SETTINGS_TEMPLATE))
        if selection is not None and selection.exports:
            env = rendered.setdefault("env", {})
            env.update(selection.exports)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(rendered, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return rendered, []
