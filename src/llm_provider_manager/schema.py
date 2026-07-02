"""Typed schema for the provider config (providers.jsonc).

Two provider shapes:
  * symmetric  — `models` at provider level; every key shares them.
  * asymmetric — `models` declared under each key.

A provider may expose multiple protocol `baseURLs`; the presence of a
protocol key determines which agents can consume it:
  * `anthropic` → usable by Claude Code
  * `openai`    → usable by opencode (rendered with @ai-sdk/openai-compatible)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ProviderType = Literal["symmetric", "asymmetric"]
Protocol = str  # "anthropic" | "openai" | other

KNOWN_PROTOCOLS = ("anthropic", "openai")

# Recovery action vocabulary: composable atom strings defined in
# providers/__init__.py (ATOMS = disable/rotate/downgrade). errorHandling values
# in providers.jsonc are comma-joined atom strings, e.g. "disable,rotate".
# The canonical default lives in providers.DEFAULT_ACTION (not here) so
# the vocabulary has a single source next to the classify logic that consumes it.


def _known_agents() -> list[str]:
    """Known agent ids — deferred import to avoid a schema ↔ agents cycle.

    The agents package imports schema only under TYPE_CHECKING, but to keep
    the dependency direction unambiguous we look the registry up at call time
    (parse time), never at module import time.
    """
    from . import agents  # noqa: deferred — breaks the import cycle

    return agents.known_agent_ids()


@dataclass
class Model:
    id: str
    display_name: str
    context: int
    output: int

    @classmethod
    def from_dict(cls, d: dict) -> "Model":
        return cls(
            id=d["id"],
            display_name=d.get("displayName", d["id"]),
            context=int(d["context"]),
            output=int(d["output"]),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "displayName": self.display_name,
            "context": self.context,
            "output": self.output,
        }


@dataclass
class Key:
    id: str
    key: str
    models: list[Model] | None = None  # asymmetric only
    agent_blacklist: tuple[str, ...] = ()  # agents this key must NOT serve
    primary_model: str | None = None    # optional override of models[0]
    downgrade_model: str | None = None  # optional override of models[1]/models[0]

    def model_ids(self) -> list[str]:
        return [m.id for m in (self.models or [])]

    def is_blacklisted_for(self, agent: str) -> bool:
        return agent in self.agent_blacklist

    @classmethod
    def from_dict(cls, d: dict, provider_type: ProviderType) -> "Key":
        raw_models = d.get("models")
        models = [Model.from_dict(m) for m in raw_models] if raw_models is not None else None
        if provider_type == "asymmetric" and not models:
            raise ValueError(
                f"asymmetric provider key '{d['id']}' must declare its own 'models'"
            )
        if provider_type == "symmetric" and models is not None:
            raise ValueError(
                f"symmetric provider key '{d['id']}' must not declare 'models' "
                "(declared at provider level)"
            )
        raw_bl = d.get("agentBlacklist") or []
        if not isinstance(raw_bl, list) or not all(isinstance(x, str) for x in raw_bl):
            raise ValueError(
                f"key '{d['id']}' agentBlacklist must be a list of agent names"
            )
        known = _known_agents()
        bad = [a for a in raw_bl if a not in known]
        if bad:
            raise ValueError(
                f"key '{d['id']}' agentBlacklist has unknown agents {bad}; "
                f"known: {known}"
            )
        return cls(id=d["id"], key=d["key"], models=models,
                   agent_blacklist=tuple(raw_bl),
                   primary_model=d.get("primaryModel"),
                   downgrade_model=d.get("downgradeModel"))


@dataclass
class Provider:
    id: str
    type: ProviderType
    display_name: str
    base_urls: dict[str, str]
    keys: list[Key]
    models: list[Model] | None = None  # symmetric only
    default_key: str | None = None
    error_handling: dict[str, str] = field(default_factory=dict)
    primary_model: str | None = None    # symmetric: optional override of models[0]
    downgrade_model: str | None = None  # symmetric: optional override of models[1]/models[0]

    def __post_init__(self) -> None:
        if self.type == "symmetric" and not self.models:
            raise ValueError(
                f"symmetric provider '{self.id}' must declare provider-level 'models'"
            )
        if self.type == "asymmetric" and self.models is not None:
            raise ValueError(
                f"asymmetric provider '{self.id}' must not declare provider-level 'models'"
            )
        key_ids = [k.id for k in self.keys]
        if len(set(key_ids)) != len(key_ids):
            raise ValueError(f"provider '{self.id}' has duplicate key ids")
        if self.default_key and self.default_key not in key_ids:
            raise ValueError(
                f"provider '{self.id}' defaultKey '{self.default_key}' not among keys"
            )
        if not self.base_urls:
            raise ValueError(f"provider '{self.id}' must declare at least one baseURLs entry")
        # optional model-tier overrides must reference real models in scope.
        # Provider-level only meaningful for symmetric (where models live); the
        # per-key check covers both shapes via models_for_key().
        if self.type == "symmetric":
            valid = {m.id for m in (self.models or [])}
            for label, mid in (("primaryModel", self.primary_model),
                               ("downgradeModel", self.downgrade_model)):
                if mid is not None and mid not in valid:
                    raise ValueError(
                        f"provider '{self.id}' {label} '{mid}' not among its models {sorted(valid)}")
        for k in self.keys:
            valid = {m.id for m in self.models_for_key(k.id)}
            for label, mid in (("primaryModel", k.primary_model),
                               ("downgradeModel", k.downgrade_model)):
                if mid is not None and mid not in valid:
                    raise ValueError(
                        f"provider '{self.id}' key '{k.id}' {label} '{mid}' "
                        f"not among its models {sorted(valid)}")

    def default_key_id(self) -> str:
        if self.default_key:
            return self.default_key
        return self.keys[0].id

    def key_by_id(self, key_id: str) -> Key:
        for k in self.keys:
            if k.id == key_id:
                return k
        raise KeyError(f"provider '{self.id}' has no key '{key_id}'")

    def all_models(self) -> list[Model]:
        """Every model the provider can serve (for any key)."""
        if self.type == "symmetric":
            return list(self.models or [])
        out: list[Model] = []
        for k in self.keys:
            out.extend(k.models or [])
        return out

    def models_for_key(self, key_id: str) -> list[Model]:
        if self.type == "symmetric":
            return list(self.models or [])
        k = self.key_by_id(key_id)
        return list(k.models or [])

    def usable_for(self, protocol: Protocol) -> bool:
        return protocol in self.base_urls

    @classmethod
    def from_dict(cls, d: dict) -> "Provider":
        ptype: ProviderType = d["type"]  # validated below
        if ptype not in ("symmetric", "asymmetric"):
            raise ValueError(f"provider '{d.get('id')}' type must be symmetric|asymmetric")
        base_urls = d["baseURLs"]
        if not isinstance(base_urls, dict) or not base_urls:
            raise ValueError(f"provider '{d.get('id')}' baseURLs must be a non-empty object")
        keys = [Key.from_dict(k, ptype) for k in d.get("keys", [])]
        if not keys:
            raise ValueError(f"provider '{d.get('id')}' must declare at least one key")
        models = None
        if ptype == "symmetric":
            raw = d.get("models")
            if not raw:
                raise ValueError(f"symmetric provider '{d['id']}' needs 'models'")
            models = [Model.from_dict(m) for m in raw]
        eh = d.get("errorHandling") or {}
        if not isinstance(eh, dict):
            raise ValueError(f"provider '{d.get('id')}' errorHandling must be an object")
        return cls(
            id=d["id"],
            type=ptype,
            display_name=d.get("displayName", d["id"]),
            base_urls=dict(base_urls),
            keys=keys,
            models=models,
            default_key=d.get("defaultKey"),
            error_handling=dict(eh),
            primary_model=d.get("primaryModel"),
            downgrade_model=d.get("downgradeModel"),
        )


@dataclass
class Settings:
    rotate_every: int = 5
    disable_ttl_hours: float = 5.0

    @classmethod
    def from_dict(cls, d: dict | None) -> "Settings":
        d = d or {}
        return cls(
            rotate_every=int(d.get("rotateEvery", 5)),
            disable_ttl_hours=float(d.get("disableTtlHours", 5.0)),
        )


@dataclass
class Default:
    """The agent+provider(+key) to use when ``use`` is invoked without args.

    ``agent`` is optional; when None, ``use`` falls back to the agents
    registry's default agent id (``claude`` by registration order).
    """
    provider: str
    key: str | None = None  # None → the provider's defaultKey
    agent: str | None = None

    def resolve_key(self, p: "Provider") -> str:
        if self.key is not None:
            return self.key
        return p.default_key_id()


@dataclass
class Config:
    settings: Settings
    providers: list[Provider]
    default: Default | None = None

    def provider_by_id(self, pid: str) -> Provider:
        for p in self.providers:
            if p.id == pid:
                return p
        raise KeyError(f"no provider '{pid}'")

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        raw = d.get("providers")
        if not isinstance(raw, list) or not raw:
            raise ValueError("providers must be a non-empty array")
        providers = [Provider.from_dict(p) for p in raw]
        ids = [p.id for p in providers]
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate provider ids: {ids}")
        default: Default | None = None
        raw_default = d.get("default")
        if raw_default is not None:
            if not isinstance(raw_default, dict) or "provider" not in raw_default:
                raise ValueError("default must be an object with 'provider' (and optional 'key'/'agent')")
            pid = raw_default["provider"]
            if pid not in ids:
                raise ValueError(f"default.provider '{pid}' not among providers")
            p = next(p for p in providers if p.id == pid)
            dk = raw_default.get("key")
            if dk is not None and dk not in [k.id for k in p.keys]:
                raise ValueError(f"default.key '{dk}' not among provider '{pid}' keys")
            da = raw_default.get("agent")
            if da is not None:
                known_agents = _known_agents()
                if da not in known_agents:
                    raise ValueError(
                        f"default.agent '{da}' not a registered agent; "
                        f"known: {known_agents}"
                    )
            default = Default(provider=pid, key=dk, agent=da)
        return cls(settings=Settings.from_dict(d.get("settings")), providers=providers, default=default)
