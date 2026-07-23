"""Key-pool adapter for the Python engine — reuses vendored lpm directly.

This is the Python counterpart of ``runner.sh``'s ``key_pool_*`` wrappers, but
with the CLI round-trip removed: lpm's ``keypool.py`` already exposes a
library-level ``KeyPool`` class (with ``init``/``rotate``/``on_success``/
``disable``/``available_size`` methods returning ``(key_value, provider_id,
key_id)`` entries) and module-level ``react``/``classify_error`` functions,
which we import directly.

**Concurrency model (multi-threaded).** This wrapper mutates NO process-level
state: it never writes ``os.environ`` and never calls ``config.clear_cache()``.
Instead, ``init``/``rotate``/``on_success`` resolve the active pool entry to a
pure ``KeyContext`` struct (key + base_url + provider-constrained models) and
return it. The engine threads that ``KeyContext`` to the backend, whose
``invoke`` maps ``key``/``base_url`` to its own env-var names and builds an
isolated controlled ``Popen(env=...)`` snapshot — so each agent
subprocess gets its own key, with zero cross-thread env races. The wrapper
tracks ``self._current_key_ctx`` (instance-level) so ``disable`` can find the
active key value without reading the process environment.

The wrapper holds NO backend reference (decoupled — the backend owns env-var-
name mapping) and NO module-level singleton state (each ``Runner`` constructs
its own ``KeyPool``). lpm loading is lazy (``_ensure_lpm``) and lock-guarded;
``lpm_src`` is process-level (``sys.path`` is process-global).
"""

from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass

# ── 定位 lpm 的 src/(lazy,锁守) ────────────────────────────────────────
# 三段式查找:1. AR_LPM_SRC(经 config 层) 2. 仓库内 subtree 的
# llm-provider-manager/src(默认)3. ~/.local/share/llm-provider-manager/src。
# lpm_src 是进程级(sys.path 进程全局);首次调用决定,后续实例仅在未设时采纳。
_HERE = os.path.dirname(os.path.abspath(__file__))

_LPM_LOCK = threading.Lock()
_LpmKeyPool = None  # set by _ensure_lpm
_lpm_react = None
_lpm_classify_error = None
_lpm_ready = False


def _ensure_lpm(cfg) -> None:
    """Lazy-locate lpm 的 src/ 并 import 其 keypool 模块。幂等 + 锁守。

    在 ``KeyPool.__init__`` 首次触发(读实例 config 的 ``lpm_src``)。进程级:
    ``sys.path`` 是进程全局,首次设置后生效;``_lpm_ready`` 守卫避免重复。
    """
    global _LpmKeyPool, _lpm_react, _lpm_classify_error, _lpm_ready
    if _lpm_ready:
        return
    with _LPM_LOCK:
        if _lpm_ready:
            return
        lpm_src = cfg.get("lpm_src", "")
        for _cand in (
            lpm_src,
            os.path.join(_HERE, "..", "llm-provider-manager", "src"),
            os.path.expanduser("~/.local/share/llm-provider-manager/src"),
        ):
            if _cand and os.path.isdir(_cand) and _cand not in sys.path:
                sys.path.insert(0, _cand)
                break
        try:
            from llm_provider_manager.keypool import (  # type: ignore
                KeyPool as _KP,
                react as _r,
                classify_error as _ce,
            )
        except ImportError as _e:  # pragma: no cover
            sys.stderr.write(
                f"agent_runner.keypool: llm_provider_manager not found ({_e}).\n"
                "  Expected the vendored llm-provider-manager/src; or set AR_LPM_SRC=<lpm>/src;\n"
                "  or install lpm (install.sh -> ~/.local/share/llm-provider-manager).\n"
            )
            raise
        _LpmKeyPool = _KP
        _lpm_react = _r
        _lpm_classify_error = _ce
        _lpm_ready = True


@dataclass
class KeyContext:
    """A resolved key context (raw values) for one pool entry. Pure data.

    Carries the resolved API key, base_url, and provider-constrained models.
    The backend maps ``key``/``base_url`` to its own env-var names and builds
    the subprocess env (extra env vars); the engine threads ``primary_model``/
    ``downgrade_model`` to ``backend.model_args`` as ``resolved_model`` (bypassing
    the config layer's env round-trip). Empty fields = no value to apply.
    """
    key: str = ""
    base_url: str = ""
    primary_model: str = ""
    downgrade_model: str = ""
    # for debugging / disable() value lookup
    provider_id: str = ""
    key_id: str = ""


@dataclass(frozen=True)
class _VerifiedProviderSnapshot:
    file_descriptor: int
    sha256: str


class KeyPool:
    """Thin wrapper over lpm's ``KeyPool`` resolving entries to ``KeyContext``.

    Holds no backend reference (decoupled) and mutates no ``os.environ`` (each
    subprocess gets fresh extra env vars via ``Popen env=``). Tracks
    ``self._current_key_ctx`` (instance-level) so ``disable`` can find the
    active key value without reading the process environment.
    """

    def __init__(
        self,
        config_path: str,
        state_path: str,
        agent_id: str,
        config,
        *,
        provider_snapshot: _VerifiedProviderSnapshot | None = None,
    ):
        _ensure_lpm(config)
        self._config_fd = (
            None
            if provider_snapshot is None
            else provider_snapshot.file_descriptor
        )
        self._config_sha256 = (
            None if provider_snapshot is None else provider_snapshot.sha256
        )
        self._kp = _LpmKeyPool(
            config_path,
            state_path,
            agent_id=agent_id,
            config_fd=self._config_fd,
            expected_config_sha256=self._config_sha256,
        )
        self._config = config
        self._current_key_ctx: KeyContext | None = None
        self._has_config = self._config_fd is not None or os.path.isfile(
            config_path
        )

    # ── config-presence guard (mirrors runner.py dispatch's has_config) ──
    # lpm's KeyPool raises on a missing config; runner.py's dispatch short-
    # circuits to no-ops/zeros. We reproduce that here so a missing config
    # (the no-key-pool case) behaves identically to the bash path.
    def _noop_guard(self) -> bool:
        """True when there's no config → the op should be a no-op."""
        return not self._has_config

    # ── resolve an entry to a KeyContext (pure, no env writes) ───────────
    def _resolve_entry(self, entry) -> KeyContext:
        """``(key_value, provider_id, key_id)`` → ``KeyContext``(纯,无副作用)。

        仅做解析:从 entry 取 provider(经 lpm)、解析 base_url(经 lpm agent 的
        ``base_url_for``)、按 AR_* 调用方意愿 + provider 供应边界选出模型。
        不写 env、不碰 config 缓存——应用动作由 backend 在 Popen env= 完成。
        """
        if not entry:
            return KeyContext()
        key_value, pid, kid = entry
        provider = self._kp.config.provider_by_id(pid)
        if provider is None:
            # provider 查不到(配置漂移)→ 只带 key,base_url/模型留空
            return KeyContext(key=key_value, provider_id=pid, key_id=kid)

        base_url = self._kp._agent.base_url_for(provider) or ""

        # 模型选择:AR_* 表调用方意愿(经 config),provider 约束供应边界。
        ar_primary = self._config.get("primary_model", "")
        ar_downgrade = self._config.get("downgrade_model", "")
        provider_primary, provider_downgrade = _resolve_models_external(provider, kid)
        available = {m.id for m in provider.models_for_key(kid)}

        chosen_primary = _choose_model(ar_primary, provider_primary, available)
        chosen_downgrade = _choose_model(ar_downgrade, provider_downgrade, available)
        chosen_primary = _model_for_agent(
            agent_id=self._kp.agent_id,
            provider=provider,
            key_id=kid,
            model_id=chosen_primary,
        )
        chosen_downgrade = _model_for_agent(
            agent_id=self._kp.agent_id,
            provider=provider,
            key_id=kid,
            model_id=chosen_downgrade,
        )

        return KeyContext(
            key=key_value,
            base_url=base_url,
            primary_model=chosen_primary,
            downgrade_model=chosen_downgrade,
            provider_id=pid,
            key_id=kid,
        )

    # ── the subcommands runner.sh wraps — each returns a KeyContext ──────
    def init(self) -> KeyContext:
        """Initialize state, resolve the current entry → KeyContext. Sets current."""
        if self._noop_guard():
            return KeyContext()
        ctx = self._resolve_entry(self._kp.init())
        self._current_key_ctx = ctx
        return ctx

    def rotate(self) -> KeyContext:
        """Advance to next non-disabled key → KeyContext. Sets current."""
        if self._noop_guard():
            return KeyContext()
        ctx = self._resolve_entry(self._kp.rotate())
        self._current_key_ctx = ctx
        return ctx

    def on_success(self) -> KeyContext:
        """Proactive rotation check → KeyContext (the now-current entry).

        If lpm rotated (success_count reached rotate_every), resolves & sets
        the new current. Otherwise returns the unchanged current. Either way
        returns the now-current KeyContext.
        """
        if self._noop_guard():
            return KeyContext()
        entry = self._kp.on_success()
        if entry:  # proactively rotated
            ctx = self._resolve_entry(entry)
            self._current_key_ctx = ctx
            return ctx
        return self._current_key_ctx or KeyContext()

    def disable(self) -> None:
        """Disable the currently-applied key (value tracked in instance)."""
        if self._current_key_ctx is None or not self._current_key_ctx.key:
            return
        if not (self._has_config and os.path.isfile(self._kp.state_path)):
            return
        self._kp.disable(self._current_key_ctx.key)

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
            config_fd=self._config_fd,
            expected_config_sha256=self._config_sha256,
        )

    def classify(self, payload_text: str) -> str:
        return _lpm_classify_error(
            payload_text,
            self._kp.config_path,
            self._kp.state_path,
            agent_id=self._kp.agent_id,
            config_fd=self._config_fd,
            expected_config_sha256=self._config_sha256,
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


def _choose_model(wanted: str, provider_default, available: set[str]) -> str:
    """模型选择:调用方意愿 wanted 若在 provider 可用列表 → 用它;否则回落
    provider_default;都没有 → 空串。"""
    if wanted and wanted in available:
        return wanted
    if provider_default:
        return provider_default
    return ""


def _model_for_agent(
    *,
    agent_id: str,
    provider,
    key_id: str,
    model_id: str,
) -> str:
    if not model_id or agent_id != "opencode":
        return model_id
    from llm_provider_manager.agents.opencode import (  # type: ignore
        opencode_entry_id_for,
    )

    entry_id = opencode_entry_id_for(provider, key_id)
    return f"{entry_id}/{model_id}"
