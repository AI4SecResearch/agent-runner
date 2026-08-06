"""配置访问层:统一配置文件(TOML 或 JSON/JSONC)+ ``AR_`` 前缀环境变量覆盖。

所有配置项走同一套机制——都可写进配置文件、都可用 ``AR_`` 前缀环境变量覆盖、都有
硬编码兜底默认。优先级:**``AR_`` env > 配置文件 > 硬编码默认**。

配置项全部在下方 ``SPECS`` 表中声明(键名 / 类型 / 默认值 / 可选嵌套路径)。
解析逻辑完全通用——遍历 SPECS、按声明取值/转类型。**加一个配置项 = SPECS 加一行**,
不碰任何其它代码。

两种配置文件格式,按扩展名分派(经 ``_parse_config_file``):
  - ``.toml``  → ``tomllib`` 解析。
  - ``.json`` / ``.jsonc`` → JSONC 解析(先剥离 ``//`` 与 ``/* */`` 注释,再 ``json.loads``;
    故可写注释;与 lpm 的 ``providers.jsonc`` 同一套约定)。纯 JSON 是 JSONC 的子集,
    无注释时等价于普通 ``json.loads``。

配置文件查找顺序(取第一个存在的):
  1. ``$AR_CONFIG_FILE``(显式指定,任意扩展名)
  2. ``./agent-runner.{toml,jsonc,json}``(当前工作目录)
  3. ``<agent_runner 包所在目录>/agent-runner.{toml,jsonc,json}``(随包 bundled
     的默认配置;与 agent-runner.example.* 模板同目录)
  4. ``~/.config/agent-runner/config.{toml,jsonc,json}``(XDG 风格用户级)

同一档里 ``.toml`` 优先于 ``.jsonc`` 优先于 ``.json``(便于既有 TOML 用户零迁移)。
找不到任何文件时,配置为空——所有项回落到硬编码默认(或被 env 覆盖)。即**无配置文件也能跑**。

配置键用下划线小写(如 ``primary_model``);对应的环境变量是 ``AR_`` + 大写键
(如 ``AR_PRIMARY_MODEL``)。访问器 ``get("primary_model")`` 自动按优先级解析。
"""

from __future__ import annotations

import json
import os
import threading
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ── 配置项声明表(单一真相源) ─────────────────────────────────────────────
@dataclass
class Spec:
    """一个配置项的声明。

    key:     规范键名(如 "stall_timeout")。env 变量 = AR_ + key 大写。
    type:    值类型(str / int / bool)。env 字符串按此转类型;配置文件已是正确类型。
    default: 硬编码兜底默认(None = 无默认)。
    path:    配置文件里的 dotted 嵌套路径(如 "timeouts.stall",TOML 子表 / JSON 嵌套
             对象均按此导航);None = 顶层 key。
    """
    key: str
    type: type = str
    default: Any = None
    path: str | None = None


SPECS: list[Spec] = [
    Spec("backend",                default="claude-code"),
    Spec("primary_model"),
    Spec("downgrade_model"),
    Spec("key_pool_config"),
    Spec("keypool_state"),         # 运行时派生(key_pool_config 同目录),表里占位
    Spec("run_dir"),
    Spec("skip_permissions", type=bool, default=False),
    Spec("stall_timeout",  type=int,  default=300, path="timeouts.stall"),
    Spec("total_timeout",  type=int,  default=0,   path="timeouts.total"),
    Spec("opencode_auth_env_var",   default="Z_AI_API_KEY"),
    Spec("lpm_src"),
]

# 规范键集合(供快速查找)
_KEYS = {s.key for s in SPECS}

# 含 agent_runner 包的根目录(本文件 parent.parent;agent-runner.example.* 模板所在)。
# 作为随包 bundled 默认配置的查找位置——在此放一份 agent-runner.json 即可作为默认,
# 无需 AR_CONFIG_FILE env 或 CWD 约定。暴露为模块常量便于测试 monkeypatch。
_OWN_DIR = Path(__file__).resolve().parent.parent


# ── 配置文件加载(查找 + 按扩展名解析) ────────────────────────────────────

def _candidate_paths() -> list[Path]:
    """配置文件候选路径(按优先级)。同一档内 .toml > .jsonc > .json。"""
    out: list[Path] = []
    explicit = os.environ.get("AR_CONFIG_FILE")
    if explicit:
        out.append(Path(explicit))
    # 当前工作目录(ad-hoc 覆盖)
    for name in ("agent-runner.toml", "agent-runner.jsonc", "agent-runner.json"):
        out.append(Path.cwd() / name)
    # 含本包的根目录(随包 bundled 默认配置;agent-runner.example.* 所在)
    for name in ("agent-runner.toml", "agent-runner.jsonc", "agent-runner.json"):
        out.append(_OWN_DIR / name)
    # XDG 风格用户级回退
    for name in ("config.toml", "config.jsonc", "config.json"):
        out.append(Path.home() / ".config" / "agent-runner" / name)
    return out


def _strip_jsonc_comments(text: str) -> str:
    """剥离 ``//`` 行注释与 ``/* */`` 块注释,尊重字符串字面量(串内的 ``//`` 不算注释)。

    与 ``llm_provider_manager.config._strip_jsonc_comments`` 同一套实现,这里自包含
    重复一份——配置加载在 lpm_src 解析之前发生(lpm_src 本身是配置项),不能反向依赖 lpm。
    """
    out: list[str] = []
    i = 0
    n = len(text)
    in_str = False
    str_quote = ""
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == str_quote:
                in_str = False
            i += 1
            continue
        # not in string
        if ch in ('"', "'"):
            in_str = True
            str_quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "/":
                # 行注释
                j = text.find("\n", i)
                i = n if j == -1 else j
                continue
            if nxt == "*":
                # 块注释
                j = text.find("*/", i + 2)
                i = n if j == -1 else j + 2
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _parse_config_file(p: Path) -> dict:
    """按扩展名解析单个配置文件为 dict。``.toml`` → tomllib;其余 → JSONC。"""
    if p.suffix.lower() == ".toml":
        with open(p, "rb") as f:
            return tomllib.load(f)
    # .json / .jsonc / 其它 → JSONC(剥注释后 json.loads;纯 JSON 是其子集)
    text = p.read_text(encoding="utf-8")
    return json.loads(_strip_jsonc_comments(text))


def _load_config_file() -> dict:
    """加载配置文件(取第一个存在且可解析的);找不到 / 解析失败返回空 dict,不报错。"""
    for p in _candidate_paths():
        try:
            if p.is_file():
                return _parse_config_file(p)
        except (OSError, tomllib.TOMLDecodeError, json.JSONDecodeError):
            continue
    return {}


def _dig(d: Any, path: str) -> Any:
    """按 dotted path 导航嵌套 dict(如 "timeouts.stall",TOML 子表 / JSON 嵌套通用)。"""
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
#   **config_overrides(实例) > AR_ env(进程) > 配置文件 > 硬编码默认**。
# 构造时解析一次、存 ``self._resolved``,无后续缓存污染——无 ``clear_cache`` 之需。
# 模块级 ``get()`` 是向后兼容薄壳(锁守懒加载默认 ``Config``),仅供 bootstrap 与
# 老的单线程调用方;生产路径经 ``Runner._config`` 实例读取。

class Config:
    """一次性解析的配置视图(per-实例)。

    ``config_overrides`` 用规范键(如 ``{"stall_timeout": 600, "primary_model": "glm-4.7"}``),
    优先级最高,实现不同 Agent 的微调隔离。``config_dict`` 可显式传入已解析的配置 dict
    (测试用),缺省从候选路径加载(经 ``_load_config_file`` 自动识别 TOML / JSON / JSONC)。
    """

    def __init__(self, config_overrides: dict | None = None, config_dict: dict | None = None):
        self._config_overrides = dict(config_overrides) if config_overrides else {}
        self._config_dict = config_dict if config_dict is not None else _load_config_file()
        self._resolved = self._resolve()

    def _resolve(self) -> dict:
        """全量解析(config_overrides > AR_ env > 配置文件 > 默认),含 keypool_state 派生。"""
        cfg = self._config_dict
        merged: dict = {}

        for spec in SPECS:
            # 1. 硬编码默认
            val = spec.default
            # 2. 配置文件覆盖(按 path 嵌套取值,否则顶层 key)
            cv = _dig(cfg, spec.path) if spec.path else cfg.get(spec.key)
            if cv is not None:
                val = cv
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
    """重置模块级默认 Config(内部用:改了 env/配置文件后让其重解析)。"""
    global _default_config
    with _DEFAULT_LOCK:
        _default_config = None
