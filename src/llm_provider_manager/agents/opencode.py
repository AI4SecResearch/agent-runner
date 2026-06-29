"""OpenCode agent backend.

Env contract: opencode's ``opencode.json`` references per-provider keys via
``{env:LLM_KEY_<PROVIDER>}`` (symmetric, or asymmetric-with-single-key) /
``{env:LLM_KEY_<PROVIDER>_<KEYID>}`` (asymmetric with multiple keys), and a
top-level ``{env:LLM_DEFAULT_MODEL}``.

  * preferred_protocols = ("openai", "anthropic") — openai preferred,
    anthropic as fallback ("有时也可以接受").
  * exports_for exports LLM_KEY_* for every opencode-usable provider/key
    (blacklist → empty string) plus LLM_DEFAULT_MODEL from the selection.
  * render_config bakes opencode.json: npm/baseURL/models static, apiKey
    and top-level model as {env:} placeholders. openai preferred; an
    anthropic-only provider uses the @ai-sdk/anthropic package. A built-in
    template (DEFAULT_CONFIG_TEMPLATE) provides sensible permission defaults.

Asymmetric providers with a single key are rendered without the key-id
suffix (no need to distinguish — behaves like symmetric). The suffix
``<provider>-<keyid>`` appears only when an asymmetric provider has
multiple keys (each with its own model set).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

from . import ExportResult

if TYPE_CHECKING:
    from ..schema import Config, Key, Model, Provider
    from ..use import UsePlan

# ── opencode env-var naming ───────────────────────────────────────
DEFAULT_MODEL_VAR = "LLM_DEFAULT_MODEL"


def key_var(provider_id: str, key_id: str | None = None) -> str:
    """Env var name holding a key for opencode.

    With key_id   → LLM_KEY_<PROVIDER>_<KEYID>  (multi-key asymmetric)
    Without key_id → LLM_KEY_<PROVIDER>          (symmetric / single-key asymmetric)
    """
    parts = [provider_id] + ([key_id] if key_id else [])
    safe = re.sub(r"[^A-Za-z0-9_]", "_", "_".join(parts)).upper()
    return f"LLM_KEY_{safe}"


def opencode_entry_id(provider_id: str, key_id: str | None = None) -> str:
    """opencode provider entry id (used in ``<entryId>/<modelId>`` refs)."""
    return f"{provider_id}-{key_id}" if key_id else provider_id


def _npm_for_protocol(protocol: str) -> str:
    if protocol == "openai":
        return "@ai-sdk/openai-compatible"
    if protocol == "anthropic":
        return "@ai-sdk/anthropic"
    raise ValueError(
        f"no npm mapping for protocol '{protocol}'; "
        "add it to opencode agent's _npm_for_protocol"
    )


# Built-in template for fresh opencode.json generation. Provider/model
# are baked on top at render time.
DEFAULT_CONFIG_TEMPLATE: dict = {
    "$schema": "https://opencode.ai/config.json",
    "permission": {
        "*": "ask",
        "read": "allow",
        "grep": "allow",
        "glob": "allow",
        "todowrite": "allow",
        "webfetch": "allow",
        "bash": {
            "git diff *": "allow",
            "git log *": "allow",
            "git show *": "allow",
            "git status *": "allow",
            "echo *": "allow",
            "grep *": "allow",
            "head *": "allow",
            "ls *": "allow",
            "cat *": "allow",
        },
    },
}


class OpencodeAgent:
    """OpenCode — openai preferred, anthropic fallback."""

    id = "opencode"
    preferred_protocols = ("openai", "anthropic")
    default_config_path = "~/.config/opencode/opencode.json"
    config_path_env_var = "LLM_PROVIDER_OPENCODE_OUT"

    def base_url_for(self, provider: "Provider") -> str | None:
        for proto in self.preferred_protocols:
            if proto in provider.base_urls:
                return provider.base_urls[proto]
        return None

    def is_usable(self, provider: "Provider") -> bool:
        return self.base_url_for(provider) is not None

    def config_path_for_dir(self, dir_path: str) -> str:
        """When -o is a directory, place the file directly inside it."""
        import os
        return os.path.join(dir_path, os.path.basename(self.default_config_path))

    def _preferred_protocol(self, provider: "Provider") -> str | None:
        for proto in self.preferred_protocols:
            if proto in provider.base_urls:
                return proto
        return None

    @staticmethod
    def _needs_per_key_entries(provider: "Provider") -> bool:
        """Whether to render one entry per key (True) or one per provider (False).

        Symmetric → always False (shared models).
        Asymmetric → always True (per-key model sets; entry id is
        <provider>-<keyid> regardless of key count).
        """
        return provider.type == "asymmetric"

    # ── exports ────────────────────────────────────────────────────

    @staticmethod
    def _key_value(key: "Key") -> str:
        return "" if key.is_blacklisted_for("opencode") else key.key

    def _first_default_model(self, config: "Config") -> str | None:
        """First opencode-usable provider/key's first model (fallback)."""
        for p in config.providers:
            if self.is_usable(p) is False:
                continue
            for k in p.keys:
                if k.is_blacklisted_for("opencode"):
                    continue
                models = p.models_for_key(k.id)
                if models:
                    entry_id = opencode_entry_id(
                        p.id, k.id if self._needs_per_key_entries(p) else None
                    )
                    return f"{entry_id}/{models[0].id}"
        return None

    def exports_for(
        self,
        config: "Config",
        provider: "Provider",
        key: "Key",
        model: str,
    ) -> ExportResult:
        exports: dict[str, str] = {}
        skipped: list[str] = []

        for p in config.providers:
            if self._needs_per_key_entries(p):
                # multi-key asymmetric: per-key vars
                any_healthy = False
                for k in p.keys:
                    v = self._key_value(k)
                    exports[key_var(p.id, k.id)] = v
                    if v:
                        any_healthy = True
                if not any_healthy:
                    skipped.append(p.id)
            else:
                # symmetric, or single-key asymmetric: one key var
                k = key if p.id == provider.id else p.key_by_id(p.default_key_id())
                v = self._key_value(k)
                exports[key_var(p.id)] = v
                if not v:
                    skipped.append(p.id)

        # default model from selection (or fallback if selection blacklisted)
        default_model: str | None = None
        if not key.is_blacklisted_for("opencode") and self.is_usable(provider):
            entry_id = opencode_entry_id(
                provider.id,
                key.id if self._needs_per_key_entries(provider) else None,
            )
            default_model = f"{entry_id}/{model}"
        else:
            default_model = self._first_default_model(config)
        if default_model:
            exports[DEFAULT_MODEL_VAR] = default_model

        return ExportResult(
            exports,
            skipped_providers=skipped,
            default_model=default_model,
        )

    # ── config rendering ───────────────────────────────────────────

    @staticmethod
    def _model_entry(m: "Model") -> dict:
        return {
            "name": m.display_name,
            "limit": {"context": m.context, "output": m.output},
        }

    def _provider_block(self, provider: "Provider", protocol: str,
                        api_key_value: str | None = None) -> dict:
        """symmetric: one entry, all provider models, single env-var key.

        If ``api_key_value`` is given (inline mode), bake it in directly;
        otherwise use an {env:} placeholder.
        """
        api_key = api_key_value if api_key_value is not None \
            else "{env:%s}" % key_var(provider.id)
        block: dict = {
            "npm": _npm_for_protocol(protocol),
            "name": provider.display_name,
            "options": {
                "apiKey": api_key,
                "baseURL": provider.base_urls[protocol],
            },
            "models": {},
        }
        for m in provider.all_models():
            block["models"][m.id] = self._model_entry(m)
        return block

    def _key_block(self, provider: "Provider", key: "Key", protocol: str,
                   api_key_value: str | None = None) -> dict:
        """asymmetric: one entry per key, only that key's models, per-key var.

        If ``api_key_value`` is given (inline mode), bake it in directly;
        otherwise use an {env:} placeholder.
        """
        api_key = api_key_value if api_key_value is not None \
            else "{env:%s}" % key_var(provider.id, key.id)
        block: dict = {
            "npm": _npm_for_protocol(protocol),
            "name": f"{provider.display_name} (key: {key.id})",
            "options": {
                "apiKey": api_key,
                "baseURL": provider.base_urls[protocol],
            },
            "models": {},
        }
        for m in (key.models or []):
            block["models"][m.id] = self._model_entry(m)
        return block

    def render_config(
        self,
        config: "Config",
        out_path: str,
        selection: "UsePlan | None" = None,
    ) -> tuple[dict, list[str]]:
        """Render opencode.json from DEFAULT_CONFIG_TEMPLATE + baked providers.

        Without ``selection`` (template mode): uses {env:} placeholders for
        apiKey and model — values come from ``lpm use`` at runtime.

        With ``selection`` (inline mode): bakes real key values and the real
        default model directly into the config — a self-contained file that
        needs no ``lpm use``. All providers are included with real keys;
        the selected provider/key determines the default model.

        Returns (rendered_dict, skipped_provider_ids).
        """
        p = Path(out_path)
        out = json.loads(json.dumps(DEFAULT_CONFIG_TEMPLATE))
        out.setdefault("$schema", "https://opencode.ai/config.json")

        providers_out: dict[str, dict] = {}
        skipped: list[str] = []
        for prov in config.providers:
            protocol = self._preferred_protocol(prov)
            if protocol is None:
                skipped.append(prov.id)
                continue
            if self._needs_per_key_entries(prov):
                for k in prov.keys:
                    var = key_var(prov.id, k.id)
                    val = selection.exports.get(var) if selection else None
                    providers_out[opencode_entry_id(prov.id, k.id)] = \
                        self._key_block(prov, k, protocol, val)
            else:
                var = key_var(prov.id)
                val = selection.exports.get(var) if selection else None
                providers_out[opencode_entry_id(prov.id)] = \
                    self._provider_block(prov, protocol, val)

        out["provider"] = providers_out
        if skipped:
            out["__skipped_providers__"] = skipped  # informational; stripped below

        if selection and selection.default_model:
            out["model"] = selection.default_model
        else:
            default_model = self._first_default_model(config)
            out["model"] = (
                "{env:%s}" % DEFAULT_MODEL_VAR if default_model else out.get("model")
            )

        skipped = out.pop("__skipped_providers__", [])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(out, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return out, skipped
