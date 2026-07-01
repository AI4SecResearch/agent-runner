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

from . import AgentStatus, ExportResult

if TYPE_CHECKING:
    from ..schema import Config, Key, Provider
    from ..use import UsePlan

# ── env vars Claude Code reads natively ───────────────────────────
ANTHROPIC_BASE_URL_VAR = "ANTHROPIC_BASE_URL"
ANTHROPIC_AUTH_TOKEN_VAR = "ANTHROPIC_AUTH_TOKEN"  # Bearer auth (3rd-party anthropic-compat)
ANTHROPIC_OPUS_MODEL_VAR = "ANTHROPIC_DEFAULT_OPUS_MODEL"
ANTHROPIC_SONNET_MODEL_VAR = "ANTHROPIC_DEFAULT_SONNET_MODEL"

# The full env contract this agent owns (for status probing). A single
# selected model fills both opus+sonnet slots, so the pair is symmetric.
ANTHROPIC_CONTRACT_VARS = (
    ANTHROPIC_BASE_URL_VAR,
    ANTHROPIC_AUTH_TOKEN_VAR,
    ANTHROPIC_OPUS_MODEL_VAR,
    ANTHROPIC_SONNET_MODEL_VAR,
)

# Default user-level output path (where `lpm agent --inline claude` writes).
DEFAULT_USER_SETTINGS_LOCAL = "~/.claude/settings.local.json"
DEFAULT_USER_SETTINGS = "~/.claude/settings.json"

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

    # ── status: reverse-resolve what's actually live ────────────────

    def config_probe_paths(self, cwd: str) -> list[str]:
        """Candidate settings files, highest precedence first.

        Mirrors Claude Code's own merge order (project local > project >
        user local > user). Only paths that exist are returned by the
        caller; here we just enumerate the search list. ``cwd``-relative
        entries are joined verbatim (caller resolves/exists-checks).
        """
        import os
        return [
            os.path.join(cwd, ".claude", "settings.local.json"),
            os.path.join(cwd, ".claude", "settings.json"),
            os.path.expanduser(DEFAULT_USER_SETTINGS_LOCAL),
            os.path.expanduser(DEFAULT_USER_SETTINGS),
        ]

    def probe_config_file(self, path: str) -> dict[str, str]:
        """Literal ANTHROPIC_* overrides baked into a settings.json file.

        Returns ``{var: value}`` for every ANTHROPIC_* key found in the
        file's top-level ``env`` block. An ``--inline`` render bakes these
        in; a ``--template`` render leaves them absent (so overrides={}),
        which is correct — template mode relies on env at runtime.
        """
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        env_block = data.get("env") if isinstance(data, dict) else None
        if not isinstance(env_block, dict):
            return {}
        return {
            k: v for k, v in env_block.items()
            if k in ANTHROPIC_CONTRACT_VARS and isinstance(v, str)
        }

    def probe(
        self,
        env: dict[str, str],
        config: "Config",
    ) -> AgentStatus:
        """Reverse-resolve the live ANTHROPIC_* env vars to provider/key/model.

        Reads only the env layer here; the orchestration layer (``status``)
        merges config-file overrides on top afterwards. Provider is matched
        by baseURL, key by its secret value — both must match a configured
        provider/key, else ``unknown``.
        """
        env_values = {v: env.get(v) for v in ANTHROPIC_CONTRACT_VARS}
        token = env_values[ANTHROPIC_AUTH_TOKEN_VAR]
        base_url = env_values[ANTHROPIC_BASE_URL_VAR]
        model = env_values[ANTHROPIC_OPUS_MODEL_VAR]  # opus==sonnet by contract

        configured = bool(token or base_url or model)
        provider_id: str | None = None
        key_id: str | None = None

        if base_url:
            for p in config.providers:
                if p.base_urls.get("anthropic") == base_url:
                    provider_id = p.id
                    break
        if provider_id and token:
            provider = config.provider_by_id(provider_id)
            for k in provider.keys:
                if k.key == token:
                    key_id = k.id
                    break

        note = ""
        if not configured:
            note = "no ANTHROPIC_* env vars set (run `lpm use` or source active.env.sh)"
        elif provider_id is None and base_url:
            note = f"baseURL {base_url!r} matches no configured provider"

        return AgentStatus(
            agent_id=self.id,
            configured=configured,
            provider_id=provider_id,
            key_id=key_id,
            model=model,
            effective_source="env" if configured else "none",
            env_values=env_values,
            secret_vars=(ANTHROPIC_AUTH_TOKEN_VAR,),
            note=note,
        )
