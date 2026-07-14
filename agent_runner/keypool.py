"""Key-pool adapter for the Python engine — reuses vendored lpm directly.

This is the Python counterpart of ``runner.sh``'s ``key_pool_*`` wrappers +
``runner.py`` CLI adapter, but with the CLI round-trip removed: lpm's
``keypool.py`` already exposes a library-level ``KeyPool`` class (with
``init``/``rotate``/``on_success``/``disable``/``available_size`` methods that
return ``(key_value, provider_id, key_id)`` entries, not JSON lines) and
module-level ``react``/``classify_error`` functions. We import those directly,
so there is no ``python3 runner.py react --text - …`` subprocess, no argparse,
no JSON-on-stdout round-trip per classification.

The ``_apply_entry`` mirrors ``runner.sh``'s ``_kp_apply``: each field is
written to the environment only when non-empty, so an omitted base_url/model
keeps the backend default; a changed API-key env-var name first unsets the
previous one (no leak across providers).
"""

from __future__ import annotations

import os
import sys

# ── 定位 lpm 的 src/(三段式查找)───────────────────────────────────────
# 1. $LPM_SRC(显式覆盖,如指向一份 dev checkout 的上游 lpm)
# 2. 仓库内 subtree 的 llm-provider-manager/src(默认)——以 git subtree 形式
#    引入,可经 `git subtree pull/push` 与上游同步,单一来源、不复制。
# 3. ~/.local/share/llm-provider-manager/src(install.sh 兜底)
_HERE = os.path.dirname(os.path.abspath(__file__))
for _cand in (
    os.environ.get("LPM_SRC"),
    os.path.join(_HERE, "..", "llm-provider-manager", "src"),
    os.path.expanduser("~/.local/share/llm-provider-manager/src"),
):
    if _cand and os.path.isdir(_cand) and _cand not in sys.path:
        sys.path.insert(0, _cand)
        break

try:
    from llm_provider_manager.keypool import (  # type: ignore
        KeyPool as _LpmKeyPool,
        react as _lpm_react,
        classify_error as _lpm_classify_error,
    )
except ImportError as _e:  # pragma: no cover - exercised via the import error path
    sys.stderr.write(
        f"agent_runner.keypool: llm_provider_manager not found ({_e}).\n"
        "  Expected the vendored llm-provider-manager/src; or set LPM_SRC=<lpm>/src;\n"
        "  or install lpm (install.sh → ~/.local/share/llm-provider-manager).\n"
    )
    raise


class KeyPool:
    """Thin wrapper over lpm's ``KeyPool`` that writes env vars on apply.

    Holds the ``_current_env_var`` mirror of ``runner.sh``'s
    ``$_kp_current_env_var`` so a changed API-key env-var name unsets the
    previous one rather than leaking it into the next invocation.
    """

    def __init__(self, config_path: str, state_path: str, agent_id: str, backend):
        self._kp = _LpmKeyPool(config_path, state_path, agent_id=agent_id)
        self._backend = backend
        self._current_env_var: str | None = None
        self._has_config = os.path.isfile(config_path)

    # ── config-presence guard (mirrors runner.py dispatch's has_config) ──
    # lpm's KeyPool raises on a missing config; runner.py's dispatch short-
    # circuits to no-ops/zeros. We reproduce that here so a missing config
    # (the no-key-pool case) behaves identically to the bash path.
    def _noop_guard(self) -> bool:
        """True when there's no config → the op should be a no-op."""
        return not self._has_config

    # ── apply an entry to the environment (mirror of _kp_apply) ──────────
    def _apply_entry(self, entry) -> None:
        if not entry:
            return
        key_value, pid, kid = entry

        # key → API key env var (backend-declared); unset a previously-exported
        # var if its name changed.
        api_key_var = self._backend.api_key_env_var()
        if api_key_var:
            if (
                self._current_env_var
                and self._current_env_var != api_key_var
                and self._current_env_var in os.environ
            ):
                del os.environ[self._current_env_var]
            os.environ[api_key_var] = key_value
            self._current_env_var = api_key_var

        # Resolve the provider from the entry's provider_id — NOT by re-reading
        # state (which would be a TOCTOU window where a concurrent rotate could
        # pair this key with another provider's base_url/models). lpm's
        # _apply_line does the same: derive provider from the resolved entry.
        provider = self._kp.config.provider_by_id(pid)
        if provider is None:
            return

        # base_url → base_url env var (backend-declared; empty for backends
        # that route via their own config). Exported only when both the var
        # name and the value are non-empty.
        base_url_var = self._backend.base_url_env_var()
        if base_url_var:
            base_url = self._kp._agent.base_url_for(provider) or ""
            if base_url:
                os.environ[base_url_var] = base_url

        # models → agent-agnostic PRIMARY_MODEL / DOWNGRADE_MODEL. Exported
        # only when the provider specifies them, so an omitted field keeps the
        # backend default.
        primary, downgrade = _resolve_models_external(provider, kid)
        if primary:
            os.environ["PRIMARY_MODEL"] = primary
        if downgrade:
            os.environ["DOWNGRADE_MODEL"] = downgrade

    # ── the subcommands runner.sh wraps ──────────────────────────────────
    def init(self) -> None:
        if self._noop_guard():
            return
        self._apply_entry(self._kp.init())

    def rotate(self) -> None:
        if self._noop_guard():
            return
        self._apply_entry(self._kp.rotate())

    def on_success(self) -> None:
        if self._noop_guard():
            return
        self._apply_entry(self._kp.on_success())

    def disable(self) -> None:
        """Disable the currently-applied key (value tracked in env)."""
        if not self._current_env_var:
            return
        if not (self._has_config and os.path.isfile(self._kp.state_path)):
            return
        key_value = os.environ.get(self._current_env_var, "")
        if key_value:
            self._kp.disable(key_value)

    def available_size(self) -> int:
        if self._noop_guard():
            return 0
        return self._kp.available_size()

    def react(self, payload_text: str) -> str:
        return _lpm_react(
            payload_text,
            self._kp.config_path,
            self._kp.state_path,
            agent_id=self._kp.agent_id,
        )

    def classify(self, payload_text: str) -> str:
        return _lpm_classify_error(
            payload_text,
            self._kp.config_path,
            self._kp.state_path,
            agent_id=self._kp.agent_id,
        )


def _resolve_models_external(provider, key_id: str):
    """Resolve (primary, downgrade) model ids for a key — lpm's helper, exposed.

    Mirrors ``llm_provider_manager.keypool._resolve_models``: precedence is
    key-level override → provider-level override → convention (models[0] =
    primary, models[1] = downgrade; single-element list → both same). Returns
    (None, None) when the key declares no models, so the caller keeps the
    backend default rather than clobbering it.
    """
    from llm_provider_manager.keypool import _resolve_models  # type: ignore

    return _resolve_models(provider, key_id)
