"""Command-line entry point.

The CLI is an orchestration layer over two pluggable axes:
  * agents    — ``use`` and ``generate`` dispatch via the agents registry.
  * providers — ``list`` shows effective error handling via the providers
    registry.

No agent or provider id is hardcoded here; new agents/providers register
themselves and the CLI picks them up automatically.
"""

from __future__ import annotations

import argparse
import os
import sys
import unicodedata

from . import agents as agents_mod
from . import config as config_mod
from . import providers as providers_mod
from . import status as status_mod
from . import use as use_mod

# Default path to the user's provider config (overridable via env/flag).
DEFAULT_CONFIG = os.environ.get(
    "LLM_PROVIDER_CONFIG", "~/.config/llm-provider-manager/providers.jsonc"
)

# Candidate config paths in priority order (when --config is not given):
#   1. ./providers.jsonc in the current working directory
#   2. ~/.config/llm-provider-manager/providers.jsonc
_CONFIG_SEARCH_PATHS = [
    "providers.jsonc",
    DEFAULT_CONFIG,
]


def _resolve_config_path(explicit: str | None) -> str:
    """Resolve the providers.jsonc path by priority.

    1. Explicit --config flag (if given)
    2. ./providers.jsonc in CWD (if it exists)
    3. ~/.config/llm-provider-manager/providers.jsonc (fallback)
    """
    if explicit:
        return _expand(explicit)
    for candidate in _CONFIG_SEARCH_PATHS:
        expanded = _expand(candidate)
        if os.path.exists(expanded):
            return expanded
    return _expand(DEFAULT_CONFIG)


def _expand(p: str) -> str:
    return os.path.expanduser(p)


def _load_config(path: str):
    expanded = _expand(path)
    for w in config_mod.check_permissions(expanded):
        print(f"warning: {w}", file=sys.stderr)
    cfg = config_mod.load(expanded)
    if config_mod.looks_unfilled(cfg):
        print(
            f"warning: {path} appears to contain REPLACE-ME placeholders "
            "(did you copy the example?)",
            file=sys.stderr,
        )
    return cfg


def _config_out_path(agent, override: str | None) -> str:
    """Resolve an agent's config output path: flag > env > agent default.

    If the override is a directory, the agent decides how to place its file
    inside (e.g. claude preserves .claude/ substructure; opencode uses basename).
    """
    if override:
        expanded = _expand(override)
        if os.path.isdir(expanded):
            return agent.config_path_for_dir(expanded)
        return expanded
    return _expand(
        os.environ.get(agent.config_path_env_var, agent.default_config_path)
    )


# ── agent (template / inline) ─────────────────────────────────────

def cmd_agent(args: argparse.Namespace) -> int:
    cfg = _load_config(args.config)
    agent_ids = agents_mod.known_agent_ids() if args.agent == "all" else [args.agent]
    for agent_id in agent_ids:
        agent = agents_mod.get_agent(agent_id)
        path = _config_out_path(agent, args.out)
        if os.path.exists(path) and not args.force:
            print(f"warning: {path} exists; skipping (use -f to overwrite)",
                  file=sys.stderr)
            continue
        if args.inline:
            plan = use_mod.build_use_plan(
                cfg, agent_id=agent_id,
                provider_id=args.provider, key_id=args.key, model=args.model,
            )
            _, skipped = agent.render_config(cfg, path, selection=plan)
            print(f"wrote {path} (agent: {agent_id}, inline)", file=sys.stderr)
            print(f"warning: {path} contains real API keys — do not commit "
                  "to a public repo", file=sys.stderr)
        else:
            _, skipped = agent.render_config(cfg, path)
            print(f"wrote {path} (agent: {agent_id}, template)", file=sys.stderr)
        _print_skipped(skipped, agent_id)
    return 0


def _print_skipped(skipped: list[str], agent_id: str) -> None:
    for s in skipped:
        print(
            f"warning: provider '{s}' skipped (no usable baseURL for "
            f"{agent_id})",
            file=sys.stderr,
        )


# ── use ────────────────────────────────────────────────────────────

def cmd_use(args: argparse.Namespace) -> int:
    cfg = _load_config(args.config)
    plan = use_mod.build_use_plan(
        cfg,
        agent_id=args.agent,
        provider_id=args.provider,
        key_id=args.key,
        model=args.model,
    )
    # persist to active.env.sh (new shells source it to restore the selection)
    env_path = use_mod.write_active_env(plan)
    # exports → stdout (for eval); diagnostics → stderr
    sys.stdout.write(use_mod.render_exports(plan))
    if plan.provider_id:
        model_info = f"  default_model={plan.default_model}" if plan.default_model else ""
        print(
            f"# agent={plan.agent_id}  provider={plan.provider_id}/"
            f"{plan.key_id}{model_info}  → {env_path}",
            file=sys.stderr,
        )
    if plan.blocked:
        print(
            f"# warning: {plan.agent_id} blocked — {plan.block_reason}",
            file=sys.stderr,
        )
    for s in plan.skipped_providers:
        print(
            f"# warning: provider '{s}' has no usable key for "
            f"{plan.agent_id}",
            file=sys.stderr,
        )
    return 0


# ── list ───────────────────────────────────────────────────────────

def _disp_width(s: str) -> int:
    """Display width of a string, accounting for CJK wide characters."""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def _pad(s: str, width: int) -> str:
    """Left-justify s to the given display width (space-padded)."""
    return s + " " * (width - _disp_width(s))


def cmd_list(args: argparse.Namespace) -> int:
    cfg = _load_config(args.config)
    if args.provider:
        p = cfg.provider_by_id(args.provider)
        print(f"{p.id} ({p.type}) — {p.display_name}")
        print(f"  baseURLs: {', '.join(p.base_urls)}")
        for k in p.keys:
            bl = ",".join(k.agent_blacklist) or "-"
            models = p.models_for_key(k.id)
            print(f"  [{k.id}] key={k.key[:10]}…  models: "
                  f"{', '.join(m.id for m in models)}  blacklist={bl}")
        _show_error_handling(p)
    else:
        default = cfg.default
        rows: list[tuple[str, str, str, str, str]] = []
        for p in cfg.providers:
            dtag = ""
            if default and default.provider == p.id:
                agent_info = f", agent={default.agent}" if default.agent else ""
                dtag = f" (default{agent_info})"
            rows.append((
                p.id + (" *" if default and default.provider == p.id else ""),
                p.type,
                p.display_name,
                ",".join(k.id for k in p.keys),
                f"baseURLs={','.join(p.base_urls)}{dtag}",
            ))
        headers = ("PROVIDER", "TYPE", "DISPLAY", "KEYS", "BASEURLS")
        cw = [max(_disp_width(h), max(_disp_width(r[i]) for r in rows)) for i, h in enumerate(headers)]
        sep = "  "
        print(sep.join(_pad(h, w) for h, w in zip(headers, cw)))
        print(sep.join(_pad("-" * w, w) for w in cw))
        for r in rows:
            print(sep.join(_pad(c, w) for c, w in zip(r, cw)))
        print()
        print("For details (models, error handling, key values): lpm list <provider>")
    return 0


def _show_error_handling(p) -> None:
    """Print the effective code→action map (built-in defaults + overrides)."""
    backend = providers_mod.get_backend(p.id)
    builtin = dict(backend.default_error_handling)
    eff = providers_mod.effective_error_handling(p.id, p.error_handling)
    print("  error handling:")
    for code, action in sorted(eff.items()):
        in_cfg = code in p.error_handling and p.error_handling.get(code) != builtin.get(code)
        src = "config override" if in_cfg else "built-in"
        print(f"    {code} → {action}  [{src}]")


# ── status ─────────────────────────────────────────────────────────

def cmd_status(args: argparse.Namespace) -> int:
    """Show what each agent is *actually* using in this shell/dir.

    Reads the live process env (inherited from the parent shell) and any
    project/user agent config files, not just ``active.env.sh`` — so it
    reflects the real current terminal state, including overrides baked in
    by ``lpm agent --inline``.
    """
    cfg = _load_config(args.config)
    statuses = status_mod.probe_all(cfg)
    sys.stdout.write(status_mod.render_status(cfg, statuses))
    return 0


# ── init-shell-hook ────────────────────────────────────────────────

def cmd_init_shell_hook(args: argparse.Namespace) -> int:
    rc_path = _expand(args.rc)
    changed = use_mod.init_shell_hook(rc_path)
    if changed:
        # covers both fresh install and refreshing a stale/older block
        print(f"installed hook in {rc_path}", file=sys.stderr)
    else:
        print(f"hook already up to date in {rc_path}", file=sys.stderr)
    return 0


# ── arg parsing ───────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lpm",
        description="Manage LLM providers and switch which LLM (provider + key) "
                    "a given agent uses in the current shell.",
    )
    p.add_argument("-c", "--config", default=None,
                   help="path to providers.jsonc (default: ./providers.jsonc "
                        "if present, else ~/.config/llm-provider-manager/providers.jsonc)")
    sub = p.add_subparsers(dest="command", required=True)

    # agent (template / inline)
    a = sub.add_parser("agent", help="render an agent's config file")
    mode = a.add_mutually_exclusive_group(required=True)
    mode.add_argument("--template", action="store_true",
                      help="render with {env:} placeholders (no secrets)")
    mode.add_argument("--inline", action="store_true",
                      help="render with real keys baked in (contains secrets!)")
    a.add_argument(
        "agent",
        metavar="AGENT",
        choices=agents_mod.known_agent_ids() + ["all"],
        help="agent id (e.g. claude, opencode) or 'all'",
    )
    a.add_argument("-f", "--force", action="store_true",
                   help="overwrite existing files (default: skip with warning)")
    a.add_argument("-o", "--out", default=None,
                   help="output path; if a directory, the agent's default "
                        "filename is used (default: agent's default_config_path)")
    a.add_argument("-p", "--provider", default=None,
                   help="(inline only) provider id (default: config 'default.provider')")
    a.add_argument("-k", "--key", default=None,
                   help="(inline only) key id (default: provider's defaultKey)")
    a.add_argument("-m", "--model", default=None,
                   help="(inline only) model id (default: the key's first model)")
    a.set_defaults(func=cmd_agent)

    # use
    u = sub.add_parser(
        "use",
        help="switch the current shell's LLM (provider + key + model)",
        description="Switch the current shell's LLM for a given agent. "
                    "Writes active.env.sh and sources it so env vars take "
                    "effect immediately; new shells restore the last selection.",
    )
    u.add_argument("--agent", default=None,
                   help="agent id, e.g. claude or opencode "
                        "(default: config 'default.agent' or 'claude')")
    u.add_argument("provider", nargs="?", default=None,
                   help="provider id (default: config 'default.provider')")
    u.add_argument("key", nargs="?", default=None,
                   help="key id (default: provider's defaultKey)")
    u.add_argument("-m", "--model", default=None,
                   help="model id to use (default: the key's first model)")
    u.set_defaults(func=cmd_use)

    # list
    l = sub.add_parser("list", help="list providers / keys / models / error handling")
    l.add_argument("provider", nargs="?", default=None,
                   help="provider id to show details for (omit to list all providers)")
    l.set_defaults(func=cmd_list)

    # status
    s = sub.add_parser(
        "status",
        help="show what each agent is actually using in this shell/dir",
        description="Show the *effective* current configuration of each agent "
                    "in this terminal. Reads the live process env (what `lpm use` "
                    "exported) AND any project/user agent config files (which may "
                    "override env, e.g. an `lpm agent --inline` render). Compares "
                    "against active.env.sh and flags drift. Unlike `list` (which "
                    "shows the config) this reflects the real current terminal state.",
    )
    s.set_defaults(func=cmd_status)

    # init-shell-hook
    h = sub.add_parser("init-shell-hook",
                       help="install the lpm() shell function + active.env.sh restore")
    h.add_argument("--rc", default="~/.zshrc",
                   help="shell rc file to add the hook to (default: ~/.zshrc)")
    h.set_defaults(func=cmd_init_shell_hook)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.config = _resolve_config_path(getattr(args, "config", None))
    try:
        return args.func(args)
    except (ValueError, KeyError, FileNotFoundError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
