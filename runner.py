#!/usr/bin/env python3
"""Agent runner core — key pool, error classification, result checking.

Key pool:
    - Config: api-keys.json (per-provider keys + error handling rules)
    - State:  key-pool-state.json (current_index, disabled map with TTL, success_count)
    - Concurrency: LOCK_SH for reads, LOCK_EX for writes via fcntl.flock
    - Disabled keys auto-expire after DISABLE_TTL (5h); clock skew clears all

Subcommands (called by runner.sh):
    init            Init state file, return current key
    rotate          Advance to next non-disabled key
    size            Total key count from config
    available-size  Non-disabled key count
    disable         Disable a key by value with TTL
    on-success      Increment success counter, rotate at threshold
    check-result    Check JSONL for success/failure
    classify        Extract error code → lookup action + auto-disable flag
    retry-plan      Generate (model, count) retry rounds from available keys
"""

import argparse
import fcntl
import json
import os
import re
import sys
import time

DISABLE_TTL = 5 * 3600  # 5 hours
DEBUG = False


def _dbg(msg):
    if DEBUG:
        print(f"[runner.py] {msg}", file=sys.stderr)


class KeyPool:
    def __init__(self, config_path, state_path):
        self.config_path = config_path
        self.state_path = state_path
        self._config = None

    @property
    def config(self):
        if self._config is None:
            with open(self.config_path) as f:
                self._config = json.load(f)
        return self._config

    def _all_keys(self):
        """Flatten all provider keys into a single list."""
        keys = []
        for provider_cfg in self.config.get("providers", {}).values():
            keys.extend(provider_cfg.get("keys", []))
        return keys

    # ── State file access (LOCK_SH / LOCK_EX) ──────────────────────

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
        keys = self._all_keys()
        if not keys:
            return ""
        if not os.path.exists(self.state_path):
            return keys[0]
        data = self._read_state()
        idx = data.get("current_index", 0)
        return keys[idx % len(keys)]

    def init(self):
        """Initialize state file from config, return current key."""
        keys = self._all_keys()
        if not keys:
            return ""
        self._init_state()
        key = self.current_key()
        _dbg(f"init: key[{self._read_state().get('current_index', 0)}]={key[:8]}...")
        return key

    def rotate(self):
        """Atomically advance to next non-disabled key, return new key."""
        keys = self._all_keys()
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
            _dbg(f"rotate: {cur}→{new_idx} key={keys[new_idx][:8]}... disabled={disabled}")
            return keys[new_idx]

        return self._modify_state(_rotate)

    def size(self):
        """Return total key count."""
        return len(self._all_keys())

    def disable(self, key_str):
        """Disable key by value with TTL. Returns the key that was disabled."""
        keys = self._all_keys()
        try:
            idx = keys.index(key_str)
        except ValueError:
            _dbg(f"disable: key {key_str[:8]}... not found in config")
            return ""

        def _disable(data):
            self._purge_expired(data)
            expiry = time.time() + DISABLE_TTL
            data.setdefault("disabled", {})[str(idx)] = self._format_expiry(expiry)
            _dbg(f"disable: key[{idx}]={key_str[:8]}... until={self._format_expiry(expiry)}")
            return key_str

        return self._modify_state(_disable)

    def available_size(self):
        """Return number of non-disabled keys."""
        total = len(self._all_keys())
        if not os.path.exists(self.state_path):
            return total
        data = self._read_state()
        active = total - len(self._active_disabled(data))
        _dbg(f"available_size: {active}/{total}")
        return active

    def on_success(self):
        """Check proactive rotation. Returns new key if rotated, else empty string."""
        rotate_every = self.config.get("rotate_every", 5)

        def _on_success(data):
            self._purge_expired(data)
            count = data.get("success_count", 0) + 1
            if count < rotate_every:
                data["success_count"] = count
                _dbg(f"on_success: count={count}/{rotate_every}")
                return ""
            data["success_count"] = 0
            # Rotate within the same lock
            disabled = self._active_disabled(data)
            if len(disabled) >= len(self._all_keys()):
                return ""
            keys = self._all_keys()
            cur = data.get("current_index", 0)
            new_idx = (cur + 1) % len(keys)
            while new_idx in disabled:
                new_idx = (new_idx + 1) % len(keys)
            data["current_index"] = new_idx
            _dbg(f"on_success: count={count}/{rotate_every}, rotating {cur}→{new_idx} key={keys[new_idx][:8]}...")
            return keys[new_idx]

        return self._modify_state(_on_success)


def classify_error(result_text, config_path, state_path):
    """Classify an error from the agent's result text and look up action + disable
    flag from the provider config.

    Returns "action:disable_flag" where disable_flag is "true" or "false".
    Action is one of: "rotate_key", "downgrade", "rotate_then_downgrade".
    """
    default_action = "rotate_then_downgrade"

    error_code = None
    if result_text:
        match = re.search(r"\[(\d{4})\]", result_text[:500])
        if match:
            error_code = match.group(1)

    if error_code is None:
        return f"{default_action}:false"

    if not os.path.exists(config_path):
        return f"{default_action}:false"

    with open(config_path) as f:
        config = json.load(f)

    provider = None
    all_keys_flat = []
    for pname, pcfg in config.get("providers", {}).items():
        all_keys_flat.extend([(k, pname) for k in pcfg.get("keys", [])])

    if os.path.exists(state_path):
        with open(state_path) as sf:
            fcntl.flock(sf, fcntl.LOCK_SH)
            try:
                idx = json.load(sf).get("current_index", 0)
            finally:
                fcntl.flock(sf, fcntl.LOCK_UN)
        if idx < len(all_keys_flat):
            current_key, provider = all_keys_flat[idx]

    if provider is None:
        return f"{default_action}:false"

    handling = config["providers"][provider].get("error_handling", {})
    action = handling.get(error_code, handling.get("_default", default_action))

    # Auto-disable key when action involves rotation (quota exhaustion)
    disable_flag = "true" if "rotate" in action else "false"
    _dbg(f"classify: error_code={error_code} provider={provider} action={action} disable={disable_flag}")

    return f"{action}:{disable_flag}"


def retry_plan(action, config_path, state_path):
    """Generate retry plan using current available key count from state.

    Falls back to single-key downgrade when config is absent.
    Returns empty list when all keys are disabled.
    """
    if os.path.exists(config_path):
        pool = KeyPool(config_path, state_path)
        pool_size = pool.available_size()
        if pool_size == 0:
            _dbg(f"retry_plan: action={action} pool_size=0, no available keys")
            return []
    else:
        pool_size = 1
    _dbg(f"retry_plan: action={action} pool_size={pool_size}")
    if action == "rotate_key":
        return [("primary", pool_size)]
    elif action == "downgrade":
        return [("downgrade", pool_size)]
    else:  # rotate_then_downgrade
        return [("primary", pool_size), ("downgrade", pool_size)]


def main():
    parser = argparse.ArgumentParser(description="Agent runner core")
    sub = parser.add_subparsers(dest="command", required=True)

    # init
    p = sub.add_parser("init")
    p.add_argument("--config", required=True)
    p.add_argument("--state", required=True)

    # rotate
    p = sub.add_parser("rotate")
    p.add_argument("--config", required=True)
    p.add_argument("--state", required=True)

    # size
    p = sub.add_parser("size")
    p.add_argument("--config", required=True)
    p.add_argument("--state", required=True)

    # on-success
    p = sub.add_parser("on-success")
    p.add_argument("--config", required=True)
    p.add_argument("--state", required=True)

    # disable
    p = sub.add_parser("disable")
    p.add_argument("--key", required=True)
    p.add_argument("--config", required=True)
    p.add_argument("--state", required=True)

    # available-size
    p = sub.add_parser("available-size")
    p.add_argument("--config", required=True)
    p.add_argument("--state", required=True)

    # classify (reads result text from --text; "-" means stdin)
    p = sub.add_parser("classify")
    p.add_argument("--text", required=True)
    p.add_argument("--config", required=True)
    p.add_argument("--state", required=True)

    # retry-plan
    p = sub.add_parser("retry-plan")
    p.add_argument("--action", required=True)
    p.add_argument("--config", required=True)
    p.add_argument("--state", required=True)

    args = parser.parse_args()

    if args.command == "init":
        if not os.path.exists(args.config):
            sys.exit(0)
        pool = KeyPool(args.config, args.state)
        key = pool.init()
        if key:
            print(key)

    elif args.command == "rotate":
        if not os.path.exists(args.config):
            print("")
            sys.exit(0)
        pool = KeyPool(args.config, args.state)
        key = pool.rotate()
        print(key)

    elif args.command == "size":
        if not os.path.exists(args.config):
            print(0)
            sys.exit(0)
        pool = KeyPool(args.config, args.state)
        print(pool.size())

    elif args.command == "on-success":
        if not os.path.exists(args.config):
            sys.exit(0)
        pool = KeyPool(args.config, args.state)
        key = pool.on_success()
        if key:
            print(key)

    elif args.command == "disable":
        if not os.path.exists(args.config) or not os.path.exists(args.state):
            sys.exit(0)
        pool = KeyPool(args.config, args.state)
        disabled_key = pool.disable(args.key)
        sys.exit(0 if disabled_key else 1)

    elif args.command == "available-size":
        if not os.path.exists(args.config):
            print(0)
            sys.exit(0)
        pool = KeyPool(args.config, args.state)
        print(pool.available_size())

    elif args.command == "classify":
        text = sys.stdin.read() if args.text == "-" else args.text
        action = classify_error(text, args.config, args.state)
        print(action)

    elif args.command == "retry-plan":
        for model, count in retry_plan(args.action, args.config, args.state):
            print(f"{model} {count}")


if __name__ == "__main__":
    main()
