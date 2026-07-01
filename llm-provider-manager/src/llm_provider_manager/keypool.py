"""Runtime key-pool rotation + error classification over a lpm Config.

This is the runtime counterpart to lpm's interactive ``use``: instead of a human
picking one provider+key, the batch agent-runner rotates across the whole pool
and reacts to errors. Ported from agent-runner/runner.py, adapted to lpm's typed
Config so it gains, for free:

  * the protocol map — base_url is resolved per active agent via
    ``Agent.base_url_for(provider)`` (anthropic for claude, openai for opencode);
  * ``agentBlacklist`` — keys blacklisted for the active agent are skipped;
  * ``is_usable`` — providers with no baseURL for the active agent are skipped.

State file (``current_index`` / ``disabled{idx:expiry}`` / ``success_count``) is
caller-supplied (agent-runner points it at ``$DATA_DIR/key-pool-state.json``) —
runtime state stays in the EXECUTION context, never in lpm's user-config dir.

Concurrency: LOCK_SH for reads, LOCK_EX for read-modify-write via fcntl.flock.
Disabled keys auto-expire after ``Settings.disable_ttl_hours``; clock skew purges
all.

Library entry point only — ``dispatch(argv)`` is consumed by agent-runner's
adapter; it is NOT wired into the ``lpm`` CLI (the runtime ops are
machine-consumed, not user-typed). ``python -m llm_provider_manager.keypool …``
works for dev/debug via the ``__main__`` guard.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time

from . import agents as agents_mod
from . import config as config_mod
from . import providers as providers_mod
from .schema import Config, Provider

DEBUG = bool(os.environ.get("LPM_KEYPOOL_DEBUG"))


def _dbg(msg: str) -> None:
    if DEBUG:
        print(f"[lpm.keypool] {msg}", file=sys.stderr)


def _resolve_models(provider: Provider, key_id: str):
    """Resolve (primary, downgrade) model ids for a key.

    Precedence: key-level override → provider-level override → convention
    (models[0]=primary, models[1]=downgrade; single-element list → both same).
    Returns (None, None) when the key declares no models, so the caller leaves
    the backend default in place rather than clobbering it with an empty value.
    """
    models = provider.models_for_key(key_id)
    if not models:
        return None, None
    ids = [m.id for m in models]
    key = provider.key_by_id(key_id)
    primary = key.primary_model or provider.primary_model or ids[0]
    downgrade = key.downgrade_model or provider.downgrade_model
    if downgrade is None:
        downgrade = ids[1] if len(ids) >= 2 else ids[0]
    return primary, downgrade


class KeyPool:
    def __init__(self, config_path, state_path, *, agent_id):
        self.config_path = config_path
        self.state_path = state_path
        self.agent_id = agent_id
        self._config: Config | None = None
        self._agent = agents_mod.get_agent(agent_id)

    @property
    def config(self) -> Config:
        if self._config is None:
            self._config = config_mod.load(self.config_path)  # silent (no warnings)
        return self._config

    def _entries(self):
        """Flatten ALL providers' keys into (key_value, provider_id, key_id).

        Skips providers not usable for the active agent (no matching baseURL
        protocol) and keys whose agentBlacklist contains the active agent.
        Enforced here so rotate / available_size / disable all stay consistent.
        Order is stable: providers in config order, keys in declared order.
        """
        out = []
        for p in self.config.providers:
            if not self._agent.is_usable(p):
                continue
            for k in p.keys:
                if k.is_blacklisted_for(self.agent_id):
                    continue
                out.append((k.key, p.id, k.id))
        return out

    def _keys(self):
        return [e[0] for e in self._entries()]

    def _entry_at(self, idx):
        """Return the (key_value, provider_id, key_id) entry at idx, or None."""
        entries = self._entries()
        if not entries:
            return None
        return entries[idx % len(entries)]

    @staticmethod
    def _label(entry):
        """Compact 'provider/key_id' label for a pool entry (for debug logs)."""
        if entry is None:
            return "?"
        _, pid, kid = entry
        return f"{pid}/{kid}"

    def _current_entry(self):
        entries = self._entries()
        if not entries:
            return None
        if not os.path.exists(self.state_path):
            return entries[0]
        data = self._read_state()
        idx = data.get("current_index", 0) % len(entries)
        return entries[idx]

    def _provider_for_current(self):
        """Return the Provider object for the current key index, or None."""
        e = self._current_entry()
        return self.config.provider_by_id(e[1]) if e else None

    def _apply_line(self, key_value):
        """Print the JSON line the key-pool wrappers consume, or an empty line.

        ``key_value`` is the selected key (returned by init/rotate/on_success);
        the provider + key_id are resolved from the current pool index so the
        base_url/models match the key. Empty arg (or no current entry) → empty
        line (preserves the ``[ -n "$key" ]`` signal for runner.sh).
        """
        if not key_value:
            print("")
            return
        entry = self._current_entry()
        if entry is None:
            print("")
            return
        _, pid, kid = entry
        provider = self.config.provider_by_id(pid)
        base_url = self._agent.base_url_for(provider) or ""
        primary, downgrade = _resolve_models(provider, kid)
        print(json.dumps({
            "key": key_value,
            "base_url": base_url,
            "primary_model": primary or "",
            "downgrade_model": downgrade or "",
        }))

    # ── State file access (LOCK_SH / LOCK_EX) ──────────────────────
    # Copied verbatim from agent-runner/runner.py — the f.truncate() flush
    # trick is load-bearing for correctness under flock. Do not simplify.

    def _read_state(self):
        """Read state under shared lock."""
        with open(self.state_path) as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                return json.load(f)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def _modify_state(self, fn):
        """Read-modify-write under exclusive lock. Returns fn result."""
        with open(self.state_path, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                data = json.load(f)
                result = fn(data)
                f.seek(0)
                json.dump(data, f)
                # json.dump() 只填缓冲、不触发 write()：内容真正进内核要等 with
                # 结束的 close()，而那已在 finally 解锁之后。故紧跟 truncate()——
                # 它发 ftruncate 前会先 flush 写缓冲，把 write() 收进 flock 临界区内。
                f.truncate()
                return result
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def _init_state(self):
        """Ensure state file is initialized. LOCK_EX serializes concurrent inits."""
        fd = os.open(self.state_path, os.O_CREAT | os.O_RDWR, 0o644)
        with os.fdopen(fd, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                if f.read().strip():
                    return
                json.dump({"current_index": 0, "disabled": {}, "success_count": 0}, f)
                f.truncate()
                _dbg(f"init: created state file {self.state_path}")
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    # ── Disabled key helpers ────────────────────────────────────────

    @staticmethod
    def _parse_expiry(v):
        """Parse expiry value (str or number) to epoch seconds."""
        if isinstance(v, (int, float)):
            return v
        from datetime import datetime, timezone, timedelta
        return datetime.strptime(v, "%Y-%m-%d %H:%M").replace(
            tzinfo=timezone(timedelta(hours=8))
        ).timestamp()

    @staticmethod
    def _format_expiry(epoch):
        """Format epoch seconds to 'YYYY-MM-DD HH:MM' (CST/UTC+8)."""
        from datetime import datetime, timezone, timedelta
        cst = timezone(timedelta(hours=8))
        return datetime.fromtimestamp(epoch, tz=cst).strftime("%Y-%m-%d %H:%M")

    def _active_disabled(self, data):
        """Return set of currently-disabled key indices (read-only filter)."""
        disabled_map = data.get("disabled", {})
        if not disabled_map:
            return set()
        now = time.time()
        return {int(k) for k, v in disabled_map.items() if self._parse_expiry(v) > now}

    def _purge_expired(self, data):
        """Remove expired entries from disabled map. Modifies data in place."""
        disabled_map = data.get("disabled", {})
        if not disabled_map:
            return
        now = time.time()
        expired = [k for k, v in disabled_map.items() if self._parse_expiry(v) <= now]
        for k in expired:
            del disabled_map[k]

    # ── Public API ──────────────────────────────────────────────────

    def current_key(self):
        """Get current key from config using index from state."""
        keys = self._keys()
        if not keys:
            return ""
        if not os.path.exists(self.state_path):
            return keys[0]
        data = self._read_state()
        idx = data.get("current_index", 0)
        return keys[idx % len(keys)]

    def init(self):
        """Initialize state file from config, return current key."""
        keys = self._keys()
        if not keys:
            return ""
        self._init_state()
        idx = self._read_state().get("current_index", 0)
        key = self.current_key()
        _dbg(f"init: [{idx}] ({self._label(self._entry_at(idx))}) key={key[:8]}...")
        return key

    def rotate(self):
        """Atomically advance to next non-disabled key, return new key."""
        keys = self._keys()
        if not keys:
            return ""

        def _rotate(data):
            self._purge_expired(data)
            disabled = self._active_disabled(data)
            if len(disabled) >= len(keys):
                _dbg(f"rotate: all {len(keys)} keys disabled")
                return ""
            cur = data.get("current_index", 0)
            new_idx = (cur + 1) % len(keys)
            while new_idx in disabled:
                new_idx = (new_idx + 1) % len(keys)
            data["current_index"] = new_idx
            _dbg(f"rotate: {cur}→{new_idx} ({self._label(self._entry_at(new_idx))}) "
                 f"key={keys[new_idx][:8]}... disabled={disabled}")
            return keys[new_idx]

        return self._modify_state(_rotate)

    def size(self):
        """Return total key count."""
        return len(self._keys())

    def disable(self, key_str):
        """Disable key by value with TTL. Returns the key that was disabled."""
        keys = self._keys()
        try:
            idx = keys.index(key_str)
        except ValueError:
            _dbg(f"disable: key={key_str[:8]}... not found in pool")
            return ""

        ttl = self.config.settings.disable_ttl_hours * 3600

        def _disable(data):
            self._purge_expired(data)
            expiry = time.time() + ttl
            data.setdefault("disabled", {})[str(idx)] = self._format_expiry(expiry)
            _dbg(f"disable: [{idx}] ({self._label(self._entry_at(idx))}) "
                 f"key={key_str[:8]}... until={self._format_expiry(expiry)}")
            return key_str

        return self._modify_state(_disable)

    def available_size(self):
        """Return number of non-disabled keys."""
        total = len(self._keys())
        if not os.path.exists(self.state_path):
            return total
        data = self._read_state()
        active = total - len(self._active_disabled(data))
        _dbg(f"available_size: {active}/{total}")
        return active

    def on_success(self):
        """Check proactive rotation. Returns new key if rotated, else empty string."""
        rotate_every = self.config.settings.rotate_every

        def _on_success(data):
            self._purge_expired(data)
            count = data.get("success_count", 0) + 1
            if count < rotate_every:
                data["success_count"] = count
                _dbg(f"on_success: count={count}/{rotate_every}")
                return ""
            data["success_count"] = 0
            keys = self._keys()
            disabled = self._active_disabled(data)
            if len(disabled) >= len(keys):
                return ""
            cur = data.get("current_index", 0)
            new_idx = (cur + 1) % len(keys)
            while new_idx in disabled:
                new_idx = (new_idx + 1) % len(keys)
            data["current_index"] = new_idx
            _dbg(f"on_success: count={count}/{rotate_every}, rotating {cur}→{new_idx} "
                 f"({self._label(self._entry_at(new_idx))}) key={keys[new_idx][:8]}...")
            return keys[new_idx]

        return self._modify_state(_on_success)


def classify_error(payload_text, config_path, state_path, *, agent_id):
    """Classify an error payload via the active provider module.

    The provider is the active key's provider (resolved from keypool state);
    interpretation is delegated to the providers package. Returns an atom
    strategy string (e.g. "disable,rotate", "downgrade").
    """
    provider_id = None
    handling: dict[str, str] = {}
    if os.path.exists(config_path):
        pool = KeyPool(config_path, state_path, agent_id=agent_id)
        p = pool._provider_for_current()
        if p is not None:
            provider_id = p.id
            handling = p.error_handling
    return providers_mod.classify(provider_id, payload_text, handling)


def react(payload_text, config_path, state_path, *, agent_id):
    """Decide ONE recovery step for the given error (reactive retry).

    The reactive counterpart to the old pre-planned ``retry_plan``: instead of
    laying out the whole retry sequence up front, the runner calls this after
    each failure and applies just the returned step, then re-classifies the
    next error. Returns either an atom strategy string (e.g. ``"disable,rotate"``,
    ``"downgrade"``) or ``"stop"`` when no recovery is possible.

    Stop rules (conservative, avoids infinite loops):
      * pool exhausted (available=0) AND strategy needs rotate → no key to move to.
      * no key pool AND strategy needs rotate → strip rotate; if the remainder
        has no actionable atoms either → stop.
      * strategy carries no actionable atom at all (e.g. bare ``disable`` with
        an empty pool) → stop.
    """
    action = classify_error(payload_text, config_path, state_path, agent_id=agent_id)

    has_pool = os.path.exists(config_path)
    available = 0
    if has_pool:
        pool = KeyPool(config_path, state_path, agent_id=agent_id)
        available = pool.available_size()

    # No key pool → disable/rotate are meaningless (nothing to disable, nothing
    # to rotate to); only downgrade (same key, smaller model) can help.
    if not has_pool:
        action = ",".join(a for a in action.split(",") if a == "downgrade")
    # Pool present but exhausted → can't rotate either; downgrade still possible.
    elif "rotate" in action and available == 0:
        action = ",".join(a for a in action.split(",") if a != "rotate")

    actionable = any(a in ("rotate", "downgrade") for a in action.split(",") if a)
    if not actionable:
        _dbg(f"react: action={action} available={available} → stop")
        return "stop"

    _dbg(f"react: action={action} available={available}")
    return action


def _resolve_agent(agent_id, config_path):
    """agent flag → config default.agent → registry default."""
    if agent_id:
        return agent_id
    if os.path.exists(config_path):
        try:
            cfg = config_mod.load(config_path)
            if cfg.default and cfg.default.agent:
                return cfg.default.agent
        except Exception:
            pass
    return agents_mod.default_agent_id()


def dispatch(argv) -> int:
    """Shared argparse entry — consumed by agent-runner's adapter and by
    ``python -m llm_provider_manager.keypool``. Not wired into the ``lpm`` CLI."""
    parser = argparse.ArgumentParser(prog="lpm-keypool", add_help=True)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--config", required=True)
        p.add_argument("--state", required=True)
        p.add_argument("--agent", default=None,
                       help="agent id (default: config default.agent or registry default)")

    for name in ("init", "rotate", "size", "available-size", "on-success"):
        common(sub.add_parser(name))
    p = sub.add_parser("disable"); common(p); p.add_argument("--key", required=True)
    p = sub.add_parser("classify"); common(p); p.add_argument("--text", required=True)
    p = sub.add_parser("react"); common(p); p.add_argument("--text", required=True)

    args = parser.parse_args(argv)
    agent_id = _resolve_agent(args.agent, args.config)
    has_config = os.path.exists(args.config)
    if not has_config:
        _dbg(f"no config at {args.config}; {args.command} is a no-op")

    def pool():
        return KeyPool(args.config, args.state, agent_id=agent_id)

    if args.command == "init":
        if not has_config:
            return 0
        kp = pool(); kp._apply_line(kp.init())
    elif args.command == "rotate":
        if not has_config:
            print(""); return 0
        kp = pool(); kp._apply_line(kp.rotate())
    elif args.command == "size":
        print(0 if not has_config else pool().size())
    elif args.command == "available-size":
        print(0 if not has_config else pool().available_size())
    elif args.command == "on-success":
        if not has_config:
            return 0
        kp = pool(); kp._apply_line(kp.on_success())
    elif args.command == "disable":
        if not (has_config and os.path.exists(args.state)):
            return 0
        ok = pool().disable(args.key)
        return 0 if ok else 1
    elif args.command == "classify":
        text = sys.stdin.read() if args.text == "-" else args.text
        print(classify_error(text, args.config, args.state, agent_id=agent_id))
    elif args.command == "react":
        text = sys.stdin.read() if args.text == "-" else args.text
        print(react(text, args.config, args.state, agent_id=agent_id))
    return 0


if __name__ == "__main__":
    sys.exit(dispatch(sys.argv[1:]))
