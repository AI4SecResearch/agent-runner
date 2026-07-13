"""``status`` — show what each agent is *actually* using right now.

Unlike ``use`` (which writes a selection) or ``list`` (which shows the config),
``status`` answers: "in *this* shell, in *this* directory, what LLM is each
agent really going to talk to?" Three layers of truth, priority high → low:

  1. **project/user config-file literals** — values baked into ``./opencode.json``,
     ``.claude/settings{.local,}.json``, ``~/.claude/settings.local.json`` etc.
     by ``lpm agent --inline``. These **override** env vars at agent runtime,
     so a bare env read would lie. Each agent's ``probe_config_file`` extracts
     only literal values (``{env:...}`` placeholders from ``--template`` renders
     are skipped — they defer to env, which is layer 2).
  2. **process env** — what ``lpm use`` exported (inherited from the parent
     shell via ``os.environ``). Each agent's ``probe`` reverse-resolves this.
  3. **active.env.sh** — *not* a source of truth, only a drift baseline: when
     the live env disagrees with what ``use`` last persisted, ``status`` flags
     it so the user knows their terminal is out of sync with the file new
     shells will source.

The orchestration here is agent-agnostic: it walks the registry, asks each
agent to probe its env and its config files, merges overrides (re-resolving
the effective provider/key/model through the same agent ``probe`` so no
matching logic is duplicated in this layer), and renders. No ``ANTHROPIC_*`` /
``LLM_KEY_*`` names appear here — they live in the agent modules, preserving
the agent × provider orthogonality.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

from . import agents as agents_mod
from . import use as use_mod
from .schema import Config

# cwd is read once at probe time; overridable for tests via probe_all(cwd=...).
_DEFAULT_CWD = os.getcwd()


def _read_active_env_exports() -> dict[str, str]:
    """Parse ``active.env.sh`` into ``{var: value}`` (the inverse of sh_export).

    Returns ``{}`` if the file is absent or unreadable. Used only as the drift
    baseline — never as a source of "current" config.
    """
    path = use_mod.active_env_path()
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return {}
    return parse_active_env_exports(text)


def parse_active_env_exports(text: str) -> dict[str, str]:
    """Parse ``export NAME='value'`` lines into a dict.

    Handles the ``'\\''`` single-quote escaping that ``env_contract.sh_export``
    produces. Non-``export`` lines and malformed lines are skipped silently.
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"^\s*export\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if not m:
            continue
        name, raw = m.group(1), m.group(2)
        out[name] = _unquote_export_value(raw)
    return out


def _unquote_export_value(raw: str) -> str:
    """Unquote the RHS of an ``export NAME=...`` line.

    ``sh_export`` always single-quotes: ``export NAME='value'`` with inner
    single quotes escaped as ``'\\''``. Undo that. If the value is unquoted
    (shouldn't happen for our output, but be lenient), take it verbatim.
    """
    raw = raw.rstrip()
    if len(raw) >= 2 and raw[0] == "'" and raw[-1] == "'":
        inner = raw[1:-1]
        # undo the '\'' escaping
        return inner.replace("'\"'\"'", "'")
    # fall back to shlex for any other quoting shape
    try:
        parts = shlex.split(raw)
        return parts[0] if parts else raw
    except ValueError:
        return raw


def _collect_config_overrides(agent, cwd: str) -> tuple[dict[str, str], str | None]:
    """Walk an agent's config-probe paths; merge literal overrides.

    Returns ``(overrides, source_path)``. Later (lower-precedence) files do
    NOT clobber values already set by an earlier (higher-precedence) file —
    matching the agent's own merge semantics (project local > user).
    """
    overrides: dict[str, str] = {}
    source: str | None = None
    for path in agent.config_probe_paths(cwd):
        if not os.path.exists(path):
            continue
        file_overrides = agent.probe_config_file(path)
        if not file_overrides:
            continue
        for k, v in file_overrides.items():
            # first (highest-precedence) file wins for each var
            if k not in overrides:
                overrides[k] = v
                source = source or path
    return overrides, source


def _resolve_effective(agent, status, overrides, source, config: Config) -> None:
    """Re-resolve provider/key/model from the merged env+override value set.

    Mutates ``status`` in place: sets ``config_overrides``, ``override_source``,
    and — when overrides change the live values — refreshes
    ``provider_id``/``key_id``/``model``/``effective_source`` via the agent's
    own ``probe``.
    """
    status.config_overrides = dict(overrides)
    status.override_source = source
    if not overrides:
        return
    merged = {k: v for k, v in status.env_values.items() if v is not None}
    merged.update(overrides)
    rebuilt = agent.probe(merged, config)
    # carry forward the effective resolution + note; keep env_values/originals
    status.provider_id = rebuilt.provider_id
    status.key_id = rebuilt.key_id
    status.model = rebuilt.model
    status.configured = rebuilt.configured
    status.effective_source = f"config-file:{source}" if source else "env"
    if rebuilt.note and not status.note:
        status.note = rebuilt.note
    elif status.note and rebuilt.note:
        # keep the more specific (override-layer) note
        status.note = rebuilt.note


def _compute_drift(status, active_exports: dict[str, str]) -> None:
    """Flag env vars whose live value disagrees with active.env.sh's record.

    Only compares vars that are *live in env* (config-file overrides are a
    separate, higher layer — a config-file value disagreeing with
    active.env.sh is expected, not drift). Mutates ``status.drift`` in place.
    """
    drift: dict[str, str] = {}
    for var, live in status.env_values.items():
        if live is None:
            continue
        recorded = active_exports.get(var)
        if recorded is not None and recorded != live:
            drift[var] = recorded
    status.drift = drift


def probe_all(
    config: Config,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> dict[str, agents_mod.AgentStatus]:
    """Probe every registered agent; return ``{agent_id: AgentStatus}``.

    ``env`` defaults to ``os.environ`` (the parent shell's exports — this is
    what makes ``status`` reflect *the current terminal*, not a stale file).
    ``cwd`` defaults to the process cwd (where project-local config files are
    sought).
    """
    env = os.environ if env is None else env
    cwd = _DEFAULT_CWD if cwd is None else cwd
    active_exports = _read_active_env_exports()

    statuses: dict[str, agents_mod.AgentStatus] = {}
    for agent_id in agents_mod.known_agent_ids():
        agent = agents_mod.get_agent(agent_id)
        status = agent.probe(env, config)
        overrides, source = _collect_config_overrides(agent, cwd)
        _resolve_effective(agent, status, overrides, source, config)
        _compute_drift(status, active_exports)
        statuses[agent_id] = status
    return statuses


# ── rendering ──────────────────────────────────────────────────────

def _redact(value: str | None) -> str:
    """Mask the middle of a secret for display; keep head/tail for identification."""
    if value is None:
        return "(unset)"
    if value == "":
        return "(empty)"
    if len(value) <= 10:
        return value[:3] + "…" if len(value) > 3 else value
    return f"{value[:6]}…{value[-4:]}"


def render_status(
    config: Config,
    statuses: dict[str, agents_mod.AgentStatus],
) -> str:
    """Format the per-agent status as human-readable text.

    Each agent gets a block: status line, effective provider/key/model (with
    the layer it was resolved from), and the env contract table (each var's
    live value, redacted for secrets, with drift/override annotations).
    """
    lines: list[str] = []
    for agent_id in agents_mod.known_agent_ids():
        st = statuses[agent_id]
        lines.append(f"agent: {agent_id}")
        if st.configured:
            lines.append("  status:        active")
        else:
            lines.append("  status:        unconfigured")
        lines.append(f"  provider:      {st.provider_id or '(unknown)'}")
        lines.append(f"  key:           {st.key_id or '(unknown)'}")
        lines.append(f"  model:         {st.model or '(unknown)'}")
        lines.append(f"  effective:     {st.effective_source}")
        if st.note:
            lines.append(f"  note:          {st.note}")
        if st.env_values:
            lines.append("  env:")
            # determine display width for alignment
            width = max(len(v) for v in st.env_values)
            secrets = set(st.secret_vars)
            for var in st.env_values:
                live = st.env_values[var]
                # only mask genuine secrets; baseURLs/model ids are shown in full
                shown = _redact(live) if var in secrets else (live if live is not None else "(unset)")
                annot = ""
                if var in st.config_overrides:
                    annot = f"  (overridden by {st.override_source})"
                elif var in st.drift:
                    rec = _redact(st.drift[var]) if var in secrets else st.drift[var]
                    annot = f"  (drift: active.env.sh={rec})"
                lines.append(f"    {var.ljust(width)} = {shown}{annot}")
        lines.append("")
    lines.append(f"active.env.sh: {use_mod.active_env_path()}")
    return "\n".join(lines) + "\n"
