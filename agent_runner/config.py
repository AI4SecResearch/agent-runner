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
import threading
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

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
    Spec("skip_permissions", type=bool, default=False),
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


# ── 解析视图(单实例,构造时一次性解析) ───────────────────────────────────
#
# ``Config`` 是一个解析后的配置视图:每个 ``Runner`` 持自己的 ``Config`` 实例,
# 从而多线程/多 Agent 场景下可各自微调(经 ``config_overrides``)。优先级:
#   **config_overrides(实例) > AR_ env(进程) > TOML > 硬编码默认**。
# 构造时解析一次、存 ``self._resolved``,无后续缓存污染——无 ``clear_cache`` 之需。
# 模块级 ``get()`` 是向后兼容薄壳(锁守懒加载默认 ``Config``),仅供 bootstrap 与
# 老的单线程调用方;生产路径经 ``Runner._config`` 实例读取。

class Config:
    """一次性解析的配置视图(per-实例)。

    ``config_overrides`` 用规范键(如 ``{"stall_timeout": 600, "primary_model": "glm-4.7"}``),
    优先级最高,实现不同 Agent 的微调隔离。``toml`` 可显式传入(测试用),缺省从候选
    路径加载。
    """

    def __init__(self, config_overrides: dict | None = None, toml: dict | None = None):
        self._config_overrides = dict(config_overrides) if config_overrides else {}
        self._toml = toml if toml is not None else _load_toml()
        self._resolved = self._resolve()

    def _resolve(self) -> dict:
        """全量解析(config_overrides > AR_ env > TOML > 默认),含 keypool_state 派生。"""
        toml = self._toml
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
            # 4. 实例 config_overrides 覆盖(最高优先级,per-实例微调)
            if spec.key in self._config_overrides:
                ov = self._config_overrides[spec.key]
                if ov is not None:
                    val = ov
            merged[spec.key] = val

        # keypool_state 派生:未显式指定时,默认 = key_pool_config 同目录下 key-pool-state.json
        if not merged.get("keypool_state"):
            kpc = merged.get("key_pool_config")
            if kpc:
                merged["keypool_state"] = str(
                    Path(kpc).expanduser().parent / "key-pool-state.json"
                )
        return merged

    def get(self, key: str, default=None):
        """取一个配置项。无值返回 default。"""
        val = self._resolved.get(key, default)
        return val if val not in (None, "") else default


# ── 模块级默认视图(锁守懒加载,向后兼容) ──────────────────────────────────

_DEFAULT_LOCK = threading.Lock()
_default_config: Config | None = None


def _default() -> Config:
    """返回(必要时构造)模块级默认 Config。锁守,线程安全。"""
    global _default_config
    if _default_config is not None:
        return _default_config
    with _DEFAULT_LOCK:
        if _default_config is None:
            _default_config = Config()
        return _default_config


def get(key: str, default=None):
    """模块级取值(委托默认 Config,向后兼容)。生产路径优先用 ``Runner._config``。"""
    return _default().get(key, default)


def _reset_default() -> None:
    """重置模块级默认 Config(内部用:改了 env/TOML 后让其重解析)。"""
    global _default_config
    with _DEFAULT_LOCK:
        _default_config = None
