"""Agent backends — pluggable per-agent env contract + config rendering.

Each agent is a self-contained module describing:
  * preferred_protocols — which provider baseURL protocols it can consume
    (ordered; e.g. opencode prefers openai, falls back to anthropic).
  * base_url_for(provider) / is_usable(provider) — protocol selection.
  * exports_for(config, provider, key, model) — env vars to export for `use`.
  * render_config(config, out_path, existing) — render the agent's static
    config file (Claude settings.json / opencode opencode.json).

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
