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

import math
from dataclasses import InitVar, dataclass, field
from typing import Any, Literal

from .providers import ATOMS

ProviderType = Literal["symmetric", "asymmetric"]
Protocol = str  # "anthropic" | "openai" | other

KNOWN_PROTOCOLS = ("anthropic", "openai")

# Recovery action vocabulary is owned by providers/__init__.py. The schema
# validates errorHandling against that canonical output contract.


class StrictConfigError(ValueError):
    """Structured strict-schema error that never stores rejected values."""

    __slots__ = ("code", "path")

    def __init__(self, *, code: str, path: str) -> None:
        self.code = code
        self.path = path
        super().__init__(f"{path}: {code}")


def _known_agents() -> list[str]:
    """Known agent ids — deferred import to avoid a schema ↔ agents cycle.

    The agents package imports schema only under TYPE_CHECKING, but to keep
    the dependency direction unambiguous we look the registry up at call time
    (parse time), never at module import time.
    """
    from . import agents  # noqa: deferred — breaks the import cycle

    return agents.known_agent_ids()


def _strict_fail(code: str, path: str) -> None:
    raise StrictConfigError(code=code, path=path)


def _schema_fail(
    *,
    strict_path: str | None,
    code: str,
    compatibility_message: str,
) -> None:
    if strict_path is not None:
        _strict_fail(code, strict_path)
    raise ValueError(compatibility_message)


def _strict_object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _strict_fail("expected_object", path)
    return value


def _strict_only_keys(
    value: dict[str, Any],
    allowed: set[str],
    path: str,
) -> None:
    if any(not isinstance(key, str) for key in value):
        _strict_fail("unknown_fields", path)
    if set(value) - allowed:
        _strict_fail("unknown_fields", path)


def _strict_text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _strict_fail("expected_non_empty_text", path)
    return value


def _strict_optional_text(value: Any, path: str) -> str | None:
    if value is None:
        return None
    return _strict_text(value, path)


def _strict_non_empty_list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list) or not value:
        _strict_fail("expected_non_empty_list", path)
    return value


def _strict_unique(values: list[str], path: str) -> None:
    if len(set(values)) != len(values):
        _strict_fail("duplicate_id", path)


@dataclass
class Model:
    id: str
    display_name: str
    context: int
    output: int

    @classmethod
    def from_dict(
        cls,
        document: dict,
        *,
        strict: bool = False,
        path: str = "model",
    ) -> Model:
        if strict:
            model = _strict_object(document, path)
            _strict_only_keys(
                model,
                {"id", "displayName", "context", "output"},
                path,
            )
            _strict_text(model.get("id"), f"{path}.id")
            _strict_optional_text(
                model.get("displayName"),
                f"{path}.displayName",
            )
            for field_name in ("context", "output"):
                amount = model.get(field_name)
                if type(amount) is not int or amount <= 0:
                    _strict_fail(
                        "expected_positive_integer",
                        f"{path}.{field_name}",
                    )
        return cls(
            id=document["id"],
            display_name=document.get("displayName", document["id"]),
            context=int(document["context"]),
            output=int(document["output"]),
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
    def from_dict(
        cls,
        document: dict,
        provider_type: ProviderType,
        *,
        strict: bool = False,
        path: str = "key",
    ) -> Key:
        if strict:
            key_document = _strict_object(document, path)
            _strict_only_keys(
                key_document,
                {
                    "id",
                    "key",
                    "agentBlacklist",
                    "models",
                    "primaryModel",
                    "downgradeModel",
                },
                path,
            )
            _strict_text(key_document.get("id"), f"{path}.id")
            _strict_text(key_document.get("key"), f"{path}.key")
            _strict_optional_text(
                key_document.get("primaryModel"),
                f"{path}.primaryModel",
            )
            _strict_optional_text(
                key_document.get("downgradeModel"),
                f"{path}.downgradeModel",
            )

        raw_models = document.get("models")
        models = (
            [
                Model.from_dict(
                    model,
                    strict=strict,
                    path=f"{path}.models[{index}]",
                )
                for index, model in enumerate(raw_models)
            ]
            if raw_models is not None
            else None
        )
        if provider_type == "asymmetric" and not models:
            _schema_fail(
                strict_path=f"{path}.models" if strict else None,
                code="invalid_model_location",
                compatibility_message=(
                    f"asymmetric provider key '{document['id']}' "
                    "must declare its own 'models'"
                ),
            )
        if provider_type == "symmetric" and models is not None:
            _schema_fail(
                strict_path=f"{path}.models" if strict else None,
                code="invalid_model_location",
                compatibility_message=(
                    f"symmetric provider key '{document['id']}' "
                    "must not declare 'models' (declared at provider level)"
                ),
            )
        if strict and models is not None:
            _strict_unique(
                [model.id for model in models],
                f"{path}.models Model id",
            )

        raw_blacklist = document.get("agentBlacklist") or []
        if not isinstance(raw_blacklist, list) or not all(
            isinstance(agent, str)
            for agent in raw_blacklist
        ):
            _schema_fail(
                strict_path=(
                    f"{path}.agentBlacklist"
                    if strict
                    else None
                ),
                code="expected_string_list",
                compatibility_message=(
                    f"key '{document['id']}' agentBlacklist must be "
                    "a list of agent names"
                ),
            )
        known_agents = _known_agents()
        bad_agents = [
            agent
            for agent in raw_blacklist
            if agent not in known_agents
        ]
        if bad_agents:
            _schema_fail(
                strict_path=(
                    f"{path}.agentBlacklist"
                    if strict
                    else None
                ),
                code="invalid_agent",
                compatibility_message=(
                    f"key '{document['id']}' agentBlacklist has unknown "
                    f"agents {bad_agents}; known: {known_agents}"
                ),
            )
        return cls(
            id=document["id"],
            key=document["key"],
            models=models,
            agent_blacklist=tuple(raw_blacklist),
            primary_model=document.get("primaryModel"),
            downgrade_model=document.get("downgradeModel"),
        )


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
    _strict_path: InitVar[str | None] = None

    def __post_init__(self, _strict_path: str | None) -> None:
        if self.type == "symmetric" and not self.models:
            _schema_fail(
                strict_path=(
                    f"{_strict_path}.models"
                    if _strict_path is not None
                    else None
                ),
                code="invalid_model_location",
                compatibility_message=(
                    f"symmetric provider '{self.id}' must declare "
                    "provider-level 'models'"
                ),
            )
        if self.type == "asymmetric" and self.models is not None:
            _schema_fail(
                strict_path=(
                    f"{_strict_path}.models"
                    if _strict_path is not None
                    else None
                ),
                code="invalid_model_location",
                compatibility_message=(
                    f"asymmetric provider '{self.id}' must not declare "
                    "provider-level 'models'"
                ),
            )
        if self.type == "asymmetric":
            for field_name, model_id in (
                ("primaryModel", self.primary_model),
                ("downgradeModel", self.downgrade_model),
            ):
                if model_id is not None:
                    _schema_fail(
                        strict_path=(
                            f"{_strict_path}.{field_name}"
                            if _strict_path is not None
                            else None
                        ),
                        code="invalid_model_location",
                        compatibility_message=(
                            f"asymmetric provider '{self.id}' must not "
                            f"declare provider-level '{field_name}'"
                        ),
                    )

        key_ids = [key.id for key in self.keys]
        if len(set(key_ids)) != len(key_ids):
            _schema_fail(
                strict_path=(
                    f"{_strict_path} Key id"
                    if _strict_path is not None
                    else None
                ),
                code="duplicate_id",
                compatibility_message=(
                    f"provider '{self.id}' has duplicate key ids"
                ),
            )
        if self.default_key and self.default_key not in key_ids:
            _schema_fail(
                strict_path=(
                    f"{_strict_path}.defaultKey"
                    if _strict_path is not None
                    else None
                ),
                code="invalid_reference",
                compatibility_message=(
                    f"provider '{self.id}' defaultKey "
                    f"'{self.default_key}' not among keys"
                ),
            )
        if not self.base_urls:
            _schema_fail(
                strict_path=(
                    f"{_strict_path}.baseURLs"
                    if _strict_path is not None
                    else None
                ),
                code="expected_non_empty_object",
                compatibility_message=(
                    f"provider '{self.id}' must declare at least one "
                    "baseURLs entry"
                ),
            )

        # optional model-tier overrides must reference real models in scope.
        # Provider-level only meaningful for symmetric (where models live); the
        # per-key check covers both shapes via models_for_key().
        if self.type == "symmetric":
            valid = {model.id for model in self.models or []}
            self._validate_model_references(
                valid,
                self.primary_model,
                self.downgrade_model,
                path=_strict_path,
            )
        for index, key in enumerate(self.keys):
            valid = {
                model.id
                for model in self.models_for_key(key.id)
            }
            key_path = (
                f"{_strict_path}.keys[{index}]"
                if _strict_path is not None
                else None
            )
            self._validate_model_references(
                valid,
                key.primary_model,
                key.downgrade_model,
                path=key_path,
            )

    def _validate_model_references(
        self,
        valid: set[str],
        primary: str | None,
        downgrade: str | None,
        *,
        path: str | None,
    ) -> None:
        for field_name, model_id in (
            ("primaryModel", primary),
            ("downgradeModel", downgrade),
        ):
            if model_id is not None and model_id not in valid:
                _schema_fail(
                    strict_path=(
                        f"{path}.{field_name}"
                        if path is not None
                        else None
                    ),
                    code="invalid_reference",
                    compatibility_message=(
                        f"provider '{self.id}' {field_name} "
                        f"'{model_id}' not among its models "
                        f"{sorted(valid)}"
                    ),
                )

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
    def from_dict(
        cls,
        document: dict,
        *,
        strict: bool = False,
        path: str = "provider",
    ) -> Provider:
        if strict:
            provider_document = _strict_object(document, path)
            _strict_only_keys(
                provider_document,
                {
                    "id",
                    "type",
                    "displayName",
                    "defaultKey",
                    "baseURLs",
                    "keys",
                    "models",
                    "primaryModel",
                    "downgradeModel",
                    "errorHandling",
                },
                path,
            )
            _strict_text(provider_document.get("id"), f"{path}.id")
            _strict_optional_text(
                provider_document.get("displayName"),
                f"{path}.displayName",
            )
            _strict_optional_text(
                provider_document.get("defaultKey"),
                f"{path}.defaultKey",
            )
            _strict_optional_text(
                provider_document.get("primaryModel"),
                f"{path}.primaryModel",
            )
            _strict_optional_text(
                provider_document.get("downgradeModel"),
                f"{path}.downgradeModel",
            )

        provider_type = document.get("type")
        if provider_type not in ("symmetric", "asymmetric"):
            _schema_fail(
                strict_path=f"{path}.type" if strict else None,
                code="invalid_provider_type",
                compatibility_message=(
                    f"provider '{document.get('id')}' type must be "
                    "symmetric|asymmetric"
                ),
            )

        base_urls = document.get("baseURLs")
        if not isinstance(base_urls, dict) or not base_urls:
            _schema_fail(
                strict_path=f"{path}.baseURLs" if strict else None,
                code="expected_non_empty_object",
                compatibility_message=(
                    f"provider '{document.get('id')}' baseURLs must be "
                    "a non-empty object"
                ),
            )
        if strict:
            for index, (protocol, url) in enumerate(base_urls.items()):
                _strict_text(
                    protocol,
                    f"{path}.baseURLs key[{index}]",
                )
                _strict_text(
                    url,
                    f"{path}.baseURLs value[{index}]",
                )

        raw_keys = document.get("keys", [])
        if strict:
            raw_keys = _strict_non_empty_list(
                raw_keys,
                f"{path}.keys",
            )
        keys = [
            Key.from_dict(
                key,
                provider_type,
                strict=strict,
                path=f"{path}.keys[{index}]",
            )
            for index, key in enumerate(raw_keys)
        ]
        if not keys:
            _schema_fail(
                strict_path=f"{path}.keys" if strict else None,
                code="expected_non_empty_list",
                compatibility_message=(
                    f"provider '{document.get('id')}' must declare "
                    "at least one key"
                ),
            )

        models = None
        raw_models = document.get("models")
        if provider_type == "symmetric":
            if strict:
                raw_models = _strict_non_empty_list(
                    raw_models,
                    f"{path}.models",
                )
            elif not raw_models:
                raise ValueError(
                    f"symmetric provider '{document['id']}' needs 'models'"
                )
            models = [
                Model.from_dict(
                    model,
                    strict=strict,
                    path=f"{path}.models[{index}]",
                )
                for index, model in enumerate(raw_models)
            ]
        elif strict and "models" in document:
            _strict_fail("invalid_model_location", f"{path}.models")

        if strict:
            _strict_unique(
                [key.id for key in keys],
                f"{path} Key id",
            )
            if models is not None:
                _strict_unique(
                    [model.id for model in models],
                    f"{path}.models Model id",
                )

        error_handling = document.get("errorHandling") or {}
        if not isinstance(error_handling, dict):
            _schema_fail(
                strict_path=(
                    f"{path}.errorHandling"
                    if strict
                    else None
                ),
                code="expected_object",
                compatibility_message=(
                    f"provider '{document.get('id')}' errorHandling "
                    "must be an object"
                ),
            )
        if strict:
            _validate_error_handling(
                error_handling,
                path=f"{path}.errorHandling",
            )

        return cls(
            id=document["id"],
            type=provider_type,
            display_name=document.get("displayName", document["id"]),
            base_urls=dict(base_urls),
            keys=keys,
            models=models,
            default_key=document.get("defaultKey"),
            error_handling=dict(error_handling),
            primary_model=document.get("primaryModel"),
            downgrade_model=document.get("downgradeModel"),
            _strict_path=path if strict else None,
        )


def _validate_error_handling(
    handling: dict[Any, Any],
    *,
    path: str,
) -> None:
    for error_code, action_text in handling.items():
        _strict_text(error_code, f"{path} error code")
        action_text = _strict_text(action_text, path)
        actions = tuple(action_text.split(","))
        if (
            not actions
            or any(action not in ATOMS for action in actions)
            or len(set(actions)) != len(actions)
            or tuple(sorted(actions, key=ATOMS.index)) != actions
        ):
            _strict_fail("invalid_error_handling", path)


@dataclass
class Settings:
    rotate_every: int = 5
    disable_ttl_hours: float = 5.0

    @classmethod
    def from_dict(
        cls,
        document: dict | None,
        *,
        strict: bool = False,
    ) -> Settings:
        if strict and document is not None:
            settings = _strict_object(document, "settings")
            _strict_only_keys(
                settings,
                {"rotateEvery", "disableTtlHours"},
                "settings",
            )
            rotate_every = settings.get("rotateEvery", 5)
            if type(rotate_every) is not int or rotate_every < 0:
                _strict_fail(
                    "expected_non_negative_integer",
                    "settings.rotateEvery",
                )
            disable_ttl = settings.get("disableTtlHours", 5)
            if (
                isinstance(disable_ttl, bool)
                or not isinstance(disable_ttl, (int, float))
                or disable_ttl <= 0
            ):
                _strict_fail(
                    "expected_positive_number",
                    "settings.disableTtlHours",
                )
            try:
                finite = math.isfinite(float(disable_ttl))
            except (OverflowError, ValueError):
                finite = False
            if not finite:
                _strict_fail(
                    "expected_positive_number",
                    "settings.disableTtlHours",
                )
        document = document or {}
        return cls(
            rotate_every=int(document.get("rotateEvery", 5)),
            disable_ttl_hours=float(
                document.get("disableTtlHours", 5.0)
            ),
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
    def from_dict(
        cls,
        document: dict,
        *,
        strict: bool = False,
    ) -> Config:
        if strict:
            root = _strict_object(document, "$")
            _strict_only_keys(
                root,
                {"settings", "default", "providers"},
                "$",
            )

        raw_providers = document.get("providers")
        if not isinstance(raw_providers, list) or not raw_providers:
            if strict:
                _strict_fail("expected_non_empty_list", "providers")
            raise ValueError("providers must be a non-empty array")
        providers = [
            Provider.from_dict(
                provider,
                strict=strict,
                path=f"providers[{index}]",
            )
            for index, provider in enumerate(raw_providers)
        ]
        provider_ids = [provider.id for provider in providers]
        if len(set(provider_ids)) != len(provider_ids):
            if strict:
                _strict_fail("duplicate_id", "Provider id")
            raise ValueError(f"duplicate provider ids: {provider_ids}")

        default = None
        raw_default = document.get("default")
        if raw_default is not None:
            if strict:
                default_document = _strict_object(
                    raw_default,
                    "default",
                )
                _strict_only_keys(
                    default_document,
                    {"provider", "key", "agent"},
                    "default",
                )
                _strict_text(
                    default_document.get("provider"),
                    "default.provider",
                )
                _strict_optional_text(
                    default_document.get("key"),
                    "default.key",
                )
                _strict_optional_text(
                    default_document.get("agent"),
                    "default.agent",
                )
            elif (
                not isinstance(raw_default, dict)
                or "provider" not in raw_default
            ):
                raise ValueError(
                    "default must be an object with 'provider' "
                    "(and optional 'key'/'agent')"
                )

            provider_id = raw_default["provider"]
            if provider_id not in provider_ids:
                if strict:
                    _strict_fail(
                        "invalid_reference",
                        "default.provider",
                    )
                raise ValueError(
                    f"default.provider '{provider_id}' not among providers"
                )
            provider = next(
                item
                for item in providers
                if item.id == provider_id
            )
            key_id = raw_default.get("key")
            if (
                key_id is not None
                and key_id not in [key.id for key in provider.keys]
            ):
                if strict:
                    _strict_fail("invalid_reference", "default.key")
                raise ValueError(
                    f"default.key '{key_id}' not among provider "
                    f"'{provider_id}' keys"
                )
            agent_id = raw_default.get("agent")
            if agent_id is not None and agent_id not in _known_agents():
                if strict:
                    _strict_fail("invalid_agent", "default.agent")
                raise ValueError(
                    f"default.agent '{agent_id}' not a registered agent; "
                    f"known: {_known_agents()}"
                )
            default = Default(
                provider=provider_id,
                key=key_id,
                agent=agent_id,
            )

        return cls(
            settings=Settings.from_dict(
                document.get("settings"),
                strict=strict,
            ),
            providers=providers,
            default=default,
        )
