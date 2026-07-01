"""Agent backends — pluggable per-agent env contract + config rendering.

Each agent is a self-contained module describing:
  * preferred_protocols — which provider baseURL protocols it can consume
    (ordered; e.g. opencode prefers openai, falls back to anthropic).
  * base_url_for(provider) / is_usable(provider) — protocol selection.
  * exports_for(config, provider, key, model) — env vars to export for `use`.
  * render_config(config, out_path, existing) — render the agent's static
    config file (Claude settings.json / opencode opencode.json).
  * probe(env, config) / probe_config_file(path) / config_probe_paths(cwd) —
    the reverse of exports_for/render_config: for ``status``, read back what
    is *actually live* in the shell env and in project/user config files
    (which may override env, e.g. an ``--inline``-rendered config).

The registry is the single dispatch point; cli/use never hardcode agent ids.
Mirrors agent-runner's backends/<name>.sh pattern: adding a new agent is a
new module + one REGISTRY entry, no changes to the orchestration layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # annotations-only; avoids circular import at runtime
    from ..schema import Config, Key, Provider
    from ..use import UsePlan


@dataclass
class ExportResult:
    """Outcome of an agent computing its env exports for a (provider, key)."""

    exports: dict[str, str]
    blocked: bool = False
    block_reason: str = ""
    skipped_providers: list[str] = field(default_factory=list)
    default_model: str | None = None


@dataclass
class AgentStatus:
    """What ``status`` reverse-resolved for one agent in the current shell/dir.

    Three layers of truth, priority high → low:
      * ``config_overrides`` — literal values baked into a project/user agent
        config file (the ``--inline`` render output). These **override** env.
      * ``env_values`` — the values the current process actually has exported
        (inherited from the parent shell; what ``lpm use`` set).
      * ``drift`` — env values that disagree with ``active.env.sh``'s record.

    The ``provider_id`` / ``key_id`` / ``model`` fields hold the **effective**
    reverse-resolution (config-file literal wins over env), and
    ``effective_source`` says which layer it came from.
    """

    agent_id: str
    configured: bool = False
    provider_id: str | None = None
    key_id: str | None = None
    model: str | None = None
    effective_source: str = "none"           # "config-file:<path>" | "env" | "none"
    env_values: dict[str, str | None] = field(default_factory=dict)
    secret_vars: tuple[str, ...] = ()        # contract vars holding API keys (redact on display)
    config_overrides: dict[str, str] = field(default_factory=dict)
    override_source: str | None = None       # path of the file that overrode
    drift: dict[str, str] = field(default_factory=dict)   # {var: active_env_value}
    note: str = ""


class Agent(Protocol):
    """The contract every agent backend implements."""

    id: str
    preferred_protocols: tuple[str, ...]
    default_config_path: str
    config_path_env_var: str

    def base_url_for(self, provider: "Provider") -> str | None: ...
    def is_usable(self, provider: "Provider") -> bool: ...
    def config_path_for_dir(self, dir_path: str) -> str: ...
    def exports_for(
        self,
        config: "Config",
        provider: "Provider",
        key: "Key",
        model: str,
    ) -> ExportResult: ...
    def render_config(
        self,
        config: "Config",
        out_path: str,
        selection: "UsePlan | None" = None,
    ) -> tuple[dict, list[str]]: ...
    # ── status: reverse-resolve what's actually live in the shell/dir ──
    def probe(
        self,
        env: dict[str, str],
        config: "Config",
    ) -> AgentStatus: ...
    def probe_config_file(self, path: str) -> dict[str, str]: ...
    def config_probe_paths(self, cwd: str) -> list[str]: ...


def _build_registry() -> dict[str, Agent]:
    from .claude import ClaudeAgent
    from .opencode import OpencodeAgent

    return {"claude": ClaudeAgent(), "opencode": OpencodeAgent()}


REGISTRY: dict[str, Agent] = _build_registry()


def get_agent(agent_id: str) -> Agent:
    if agent_id not in REGISTRY:
        raise ValueError(
            f"unknown agent '{agent_id}'; known: {list(REGISTRY)}"
        )
    return REGISTRY[agent_id]


def known_agent_ids() -> list[str]:
    return list(REGISTRY)


def default_agent_id() -> str:
    # First registered; "claude" by convention.
    return next(iter(REGISTRY))
