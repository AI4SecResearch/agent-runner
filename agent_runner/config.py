"""配置访问层:统一 TOML 配置文件 + ``AR_`` 前缀环境变量覆盖。

所有配置项走同一套机制——都可写进 TOML、都可用 ``AR_`` 前缀环境变量覆盖、都有硬编码兜底
默认。优先级:**``AR_`` env > TOML > 硬编码默认**。

配置项全部在下方 ``SPECS`` 表中声明(键名 / 类型 / 默认值 / 可选 TOML 嵌套路径)。
解析逻辑完全通用——遍历 SPECS、按声明取值/转类型。**加一个配置项 = SPECS 加一行**,
不碰任何其它代码。

配置文件查找顺序(取第一个存在的):
  1. ``$AR_CONFIG_FILE``(显式指定)
  2. ``./agent-runner.toml``(当前工作目录)
  3. ``~/.config/agent-runner/config.toml``(XDG 风格用户级)

找不到任何文件时,配置为空——所有项回落到硬编码默认(或被 env 覆盖)。即**无 TOML 也能跑**。

TOML 键用下划线小写(如 ``primary_model``);对应的环境变量是 ``AR_`` + 大写键
(如 ``AR_PRIMARY_MODEL``)。访问器 ``get("primary_model")`` 自动按优先级解析。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

_CACHE: dict | None = None


# ── 配置项声明表(单一真相源) ─────────────────────────────────────────────
@dataclass
class Spec:
    """一个配置项的声明。

    key:        规范键名(如 "stall_timeout")。env 变量 = AR_ + key 大写。
    type:       值类型(str / int / bool)。env 字符串按此转类型;TOML 已是正确类型。
    default:    硬编码兜底默认(None = 无默认)。
    toml_path:  嵌套 TOML 路径(如 "timeouts.stall");None = 顶层 key。
    """
    key: str
    type: type = str
    default: Any = None
    toml_path: str | None = None


SPECS: list[Spec] = [
    Spec("backend",                default="claude-code"),
    Spec("primary_model"),
    Spec("downgrade_model"),
    Spec("key_pool_config"),
    Spec("keypool_state"),         # 运行时派生(key_pool_config 同目录),表里占位
    Spec("run_dir"),
    Spec("sandbox",        type=bool, default=False),
    Spec("stall_timeout",  type=int,  default=300, toml_path="timeouts.stall"),
    Spec("total_timeout",  type=int,  default=0,   toml_path="timeouts.total"),
    Spec("opencode_auth_env_var",   default="Z_AI_API_KEY"),
    Spec("lpm_src"),
]

# 规范键集合(供快速查找)
_KEYS = {s.key for s in SPECS}


# ── TOML 加载(查找 + 解析) ───────────────────────────────────────────────

def _candidate_paths() -> list[Path]:
    """配置文件候选路径(按优先级)。"""
    out = []
    explicit = os.environ.get("AR_CONFIG_FILE")
    if explicit:
        out.append(Path(explicit))
    out.append(Path.cwd() / "agent-runner.toml")
    out.append(Path.home() / ".config" / "agent-runner" / "config.toml")
    return out


def _load_toml() -> dict:
    """加载配置文件(取第一个存在的);找不到返回空 dict,不报错。"""
    for p in _candidate_paths():
        try:
            if p.is_file():
                with open(p, "rb") as f:
                    return tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):
            continue
    return {}


def _dig(d: Any, path: str) -> Any:
    """按 dotted path 导航嵌套 dict(如 "timeouts.stall")。"""
    for part in path.split("."):
        if not isinstance(d, dict):
            return None
        d = d.get(part)
    return d


# ── 通用解析(遍历 SPECS,零特判) ─────────────────────────────────────────

def _coerce(val: str, type_: type) -> Any:
    """env 字符串按声明类型转。"""
    if type_ is bool:
        return val.lower() in ("1", "true", "yes", "on")
    if type_ is int:
        try:
            return int(val)
        except ValueError:
            return val
    return val


def _resolved() -> dict:
    """全量解析(AR_ env > TOML > 默认),含 keypool_state 派生。"""
    global _CACHE
    if _CACHE is not None:
        return _CACHE

    toml = _load_toml()
    merged: dict = {}

    for spec in SPECS:
        # 1. 硬编码默认
        val = spec.default
        # 2. TOML 覆盖(按 toml_path 嵌套取值,否则顶层 key)
        tv = _dig(toml, spec.toml_path) if spec.toml_path else toml.get(spec.key)
        if tv is not None:
            val = tv
        # 3. AR_ env 覆盖(env 总是字符串,按 spec.type 转)
        ev = os.environ.get("AR_" + spec.key.upper())
        if ev is not None:
            val = _coerce(ev, spec.type)
        merged[spec.key] = val

    # keypool_state 派生:未显式指定时,默认 = key_pool_config 同目录下 key-pool-state.json
    if not merged.get("keypool_state"):
        kpc = merged.get("key_pool_config")
        if kpc:
            merged["keypool_state"] = str(
                Path(kpc).expanduser().parent / "key-pool-state.json"
            )

    _CACHE = merged
    return merged


# ── 公开 API ──────────────────────────────────────────────────────────────

def get(key: str, default=None):
    """取一个配置项(经 AR_ env > TOML > 默认 解析)。无值返回 default。"""
    val = _resolved().get(key, default)
    return val if val not in (None, "") else default


def clear_cache() -> None:
    """清除缓存(测试用:改了 env/TOML 后重新解析)。"""
    global _CACHE
    _CACHE = None
