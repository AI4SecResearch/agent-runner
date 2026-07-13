"""Manual agent/provider/key selection.

``use`` computes shell ``export`` lines for a chosen (agent, provider, key)
and prints them to stdout (for ``eval "$(llm-provider-manager use ...)"``).
It also persists them to ``active.env.sh`` so that new shells can
``source`` the file to restore the last selection.

It is **agent-scoped**: only the selected agent's env vars are exported.
The agent's own ``exports_for`` decides what to emit — the orchestration
layer here never hardcodes ``ANTHROPIC_*`` or ``LLM_KEY_*``.

Resolution order:
  * agent    — ``--agent`` flag → config ``default.agent`` → registry default
  * provider — positional arg → config ``default.provider``
  * key      — positional arg → config ``default.key`` → provider's defaultKey
  * model    — ``--model`` flag → the key's first available model
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import agents as agents_mod
from . import env_contract as ec
from .schema import Config, Provider, Key

# Path to the persisted env file (sourced by the shell hook).
ACTIVE_ENV_VAR = "LLM_PROVIDER_ACTIVE_ENV"
DEFAULT_ACTIVE_ENV = "~/.config/llm-provider-manager/active.env.sh"


def active_env_path() -> str:
    """Resolve the active.env.sh path (env var override > default)."""
    return os.path.expanduser(
        os.environ.get(ACTIVE_ENV_VAR, DEFAULT_ACTIVE_ENV)
    )


@dataclass
class UsePlan:
    exports: dict[str, str]
    agent_id: str | None
    provider_id: str | None
    key_id: str | None
    default_model: str | None
    blocked: bool
    block_reason: str = ""
    skipped_providers: list[str] = field(default_factory=list)


def _resolve_model(provider: Provider, key: Key, model: str | None) -> str:
    """The single model id to feed the agent (validated against the key)."""
    models = provider.models_for_key(key.id)
    if not models:
        raise ValueError(
            f"provider '{provider.id}' key '{key.id}' has no models"
        )
    if model is not None:
        if model not in {m.id for m in models}:
            raise ValueError(
                f"model '{model}' not available under provider "
                f"'{provider.id}' key '{key.id}'"
            )
        return model
    return models[0].id


def build_use_plan(
    config: Config,
    *,
    agent_id: str | None = None,
    provider_id: str | None = None,
    key_id: str | None = None,
    model: str | None = None,
) -> UsePlan:
    """Compute the exports for ``use``.

    ``agent_id`` defaults to config ``default.agent`` (or the registry's
    default agent — ``claude``). ``provider_id`` / ``key_id`` default to
    config ``default``; ``model`` to the key's first model.
    """
    # ── resolve agent ──
    if agent_id is None:
        if config.default is not None and config.default.agent is not None:
            agent_id = config.default.agent
        else:
            agent_id = agents_mod.default_agent_id()
    agent = agents_mod.get_agent(agent_id)

    # ── resolve provider/key ──
    if provider_id is None:
        if config.default is not None:
            provider_id = config.default.provider
            if key_id is None:
                key_id = config.default.key
        else:
            # No default configured — fall back to the first provider.
            provider_id = config.providers[0].id

    provider = config.provider_by_id(provider_id)
    kid = key_id or provider.default_key_id()
    key = provider.key_by_id(kid)
    selected = _resolve_model(provider, key, model)

    # ── delegate to the agent ──
    result = agent.exports_for(config, provider, key, selected)

    return UsePlan(
        exports=result.exports,
        agent_id=agent.id,
        provider_id=provider.id,
        key_id=key.id,
        default_model=result.default_model,
        blocked=result.blocked,
        block_reason=result.block_reason,
        skipped_providers=result.skipped_providers,
    )


def render_exports(plan: UsePlan) -> str:
    """Render the plan as shell ``export`` lines (for eval / source)."""
    lines = [ec.sh_export(k, v) for k, v in plan.exports.items()]
    return "\n".join(lines) + ("\n" if lines else "")


def write_active_env(plan: UsePlan, path: str | None = None) -> str:
    """Persist the plan's exports to active.env.sh (0600).

    Returns the path written. The file is sourceable by the shell hook
    so that new shells restore the last selection.
    """
    p = Path(path or active_env_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render_exports(plan), encoding="utf-8")
    os.chmod(p, 0o600)
    return str(p)


# ── shell hook ──────────────────────────────────────────────────────

HOOK_MARKER_BEGIN = "# >>> llm-provider-manager >>>"
HOOK_MARKER_END = "# <<< llm-provider-manager <<<"

# Absolute path to the lpm wrapper, injected into the hook template below.
# The hook invokes lpm by this path rather than the bare `lpm` name so it
# keeps working when ~/.local/bin is absent from PATH (non-interactive
# shells, scripts, cron, sub-shells with a reset PATH, …). `lpm()` is still
# defined by name for interactive typing; only its internal forwarding (and
# the rc-time init call) use the absolute path.
LPM_BIN = "$HOME/.local/bin/lpm"

HOOK_TEMPLATE = """\
{begin}
# restore last selection (or initialise from config 'default')
export LLM_PROVIDER_ACTIVE_ENV="$HOME/.config/llm-provider-manager/active.env.sh"
[ -f "$LLM_PROVIDER_ACTIVE_ENV" ] || eval "$({bin} use 2>/dev/null)"
source "$LLM_PROVIDER_ACTIVE_ENV" 2>/dev/null
# lpm: intercepts 'use' to source active.env.sh after running; all else passes through
lpm() {{
    if [ "$1" = "use" ]; then
        command {bin} use "${{@:2}}" && source "$LLM_PROVIDER_ACTIVE_ENV"
    else
        command {bin} "$@"
    fi
}}
{end}
"""


def build_hook_block() -> str:
    return HOOK_TEMPLATE.format(begin=HOOK_MARKER_BEGIN, end=HOOK_MARKER_END, bin=LPM_BIN)


def init_shell_hook(rc_path: str) -> bool:
    """Install or refresh the hook block in a shell rc file.

    Re-runs are idempotent AND self-updating: an existing block (text between
    the markers, inclusive) is removed and re-written with the current
    template, so bumping the hook (e.g. a new LPM_BIN path) propagates on the
    next ``lpm init-shell-hook`` / install instead of leaving stale blocks
    behind. Returns True if the file was modified, False if the block was
    already up to date.
    """
    rc = Path(rc_path).expanduser()
    block = build_hook_block()
    content = rc.read_text(encoding="utf-8") if rc.exists() else ""

    # Replace an existing block (markers inclusive) with the current template.
    if HOOK_MARKER_BEGIN in content and HOOK_MARKER_END in content:
        pre, _, rest = content.partition(HOOK_MARKER_BEGIN)
        _, _, post = rest.partition(HOOK_MARKER_END)
        new_content = pre.rstrip("\n") + "\n\n" + block
        if post.strip():
            new_content += "\n" + post.lstrip("\n")
        new_content = new_content.rstrip("\n") + "\n"
        if new_content == content:
            return False
        rc.parent.mkdir(parents=True, exist_ok=True)
        rc.write_text(new_content, encoding="utf-8")
        return True

    # No existing block — append a fresh one.
    new_content = content
    if new_content and not new_content.endswith("\n"):
        new_content += "\n"
    new_content += "\n" + block
    rc.parent.mkdir(parents=True, exist_ok=True)
    rc.write_text(new_content, encoding="utf-8")
    return True
