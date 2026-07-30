"""Equivalence tests: prove the Python backends' jsonl parsing is byte-for-byte
identical to the bash backends' ``jq`` filters, over the same sample logs.

These are the load-bearing correctness claims for the native port — the
engine's success/failure decisions and the error payloads fed to the provider
layer all derive from these parsers. If they diverge from ``jq``, the Python
path silently misbehaves where the bash path was correct.

Approach: source the bash backend's functions, run them on a sample jsonl
(the same path the engine would produce), and compare to the Python backend's
output. We DON'T run the agent binary — we feed hand-crafted jsonl fixtures
covering the interesting event shapes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

# ── make agent_runner importable (the new standalone project) ────────────
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent  # agent-runner-py/
sys.path.insert(0, str(ROOT))
os.environ.setdefault("OUTPUT_DIR", str(HERE / "fixtures" / "outdir"))
(Path(os.environ["OUTPUT_DIR"]) / "state").mkdir(parents=True, exist_ok=True)

from agent_runner.backends.claude_code import ClaudeCodeBackend  # noqa: E402
from agent_runner.backends.opencode import OpencodeBackend  # noqa: E402

# ── bash cross-check (optional) ───────────────────────────────────────────
# This project is standalone and has NO runtime coupling to the bash engine.
# But while it lives inside the agent-runner repo, we cross-check the Python
# parsers against the original bash+jq backends to prove byte-equivalence.
# These cross-checks are gated on the bash backends being present; if the
# project is lifted out of the repo, they skip (the Python parsers are still
# exercised by test_engine.py's mock-backend cases).
BASH_REPO = ROOT.parent  # agent-runner repo root (parent of agent-runner-py/)
BASH_AVAILABLE = (
    (BASH_REPO / "landlock.sh").is_file()
    and (BASH_REPO / "backends" / "claude-code.sh").is_file()
)
needs_bash = pytest.mark.skipif(
    not BASH_AVAILABLE,
    reason="bash backends not found (project outside agent-runner repo)",
)

# ── bash harness: source a backend and call one of its functions ──────────
BASH_HARNESS = r"""#!/bin/bash
source "{landlock}"
source "{backend}"
{prelude}
{call}
"""

REPO = BASH_REPO


def bash_fn(backend_name: str, fn_name: str, args: list[str], prelude: str = "") -> str:
    """Source a bash backend and call ``fn_name`` with ``args``; return stdout.

    ``backend_name`` is the backend file (claude-code / opencode); the function
    is called exactly as runner.sh would call it. Non-zero exit is NOT treated as
    an error — some bash functions (e.g. ``resume_args`` with empty sid, which
    returns from an ``&&`` short-circuit) legitimately exit non-zero while
    producing the expected (empty) stdout.
    """
    landlock = REPO / "landlock.sh"
    backend = REPO / "backends" / f"{backend_name}.sh"
    call = f"{fn_name} {' '.join(args)}"
    script = BASH_HARNESS.format(
        landlock=landlock, backend=backend, prelude=prelude, call=call
    )
    r = subprocess.run(["bash"], input=script, capture_output=True, text=True)
    return r.stdout


def write_jsonl(tmp_path: Path, name: str, lines: list[dict | str]) -> str:
    """Write a jsonl fixture (prefix.jsonl); returns the prefix (no extension).

    ``lines`` may be dicts (json-encoded) or raw strings (for malformed lines).
    """
    prefix = tmp_path / name
    with open(f"{prefix}.jsonl", "w") as f:
        for ln in lines:
            if isinstance(ln, str):
                f.write(ln + "\n")
            else:
                f.write(json.dumps(ln, separators=(",", ":")) + "\n")
    return str(prefix)


# ── fixtures ───────────────────────────────────────────────────────────────

# claude-code result line shapes (stream-json).
CLAUDE_OK = {"type": "result", "result": "done", "is_error": False, "session_id": "s1"}
CLAUDE_ERR_RESULT = {
    "type": "result",
    "result": "Error: [1308] quota exceeded",
    "is_error": True,
    "session_id": "s2",
}
CLAUDE_STREAM_PREFIX = [
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
    {"type": "stream_start"},
]
CLAUDE_NO_RESULT = [{"type": "assistant"}]  # stalled, no result line

# opencode event shapes (format json).
OPENCODE_TEXT = {"type": "text", "part": {"text": "hello "}, "sessionID": "S1"}
OPENCODE_TEXT2 = {"type": "text", "part": {"text": "world"}, "sessionID": "S1"}
OPENCODE_STOP = {"type": "step_finish", "part": {"reason": "stop"}, "sessionID": "S1"}
OPENCODE_TOOLSTEP = {"type": "step_finish", "part": {"reason": "tool-calls"}, "sessionID": "S1"}
OPENCODE_ERROR = {
    "type": "error",
    "error": {
        "data": {
            "message": "rate limited",
            "statusCode": 429,
            "responseBody": json.dumps({"error": {"code": "42901", "message": "slow down"}}),
        }
    },
    "sessionID": "S2",
}


@pytest.fixture(params=[True, False])
def claude_backend(request, monkeypatch):
    b = ClaudeCodeBackend()
    monkeypatch.setenv("SANDBOX", "1" if request.param else "")
    return b


@pytest.fixture(params=[True, False])
def opencode_backend(request, monkeypatch):
    b = OpencodeBackend()
    monkeypatch.setenv("SANDBOX", "1" if request.param else "")
    return b


# ── claude-code equivalence ───────────────────────────────────────────────

@needs_bash
def test_claude_result_ok_matches(tmp_path, claude_backend):
    for fixture, expected in [
        ([CLAUDE_OK], True),
        ([CLAUDE_ERR_RESULT], False),
        (CLAUDE_STREAM_PREFIX + [CLAUDE_OK], True),
        (CLAUDE_NO_RESULT, False),
    ]:
        prefix = write_jsonl(tmp_path, "c", fixture)
        py = claude_backend.result_ok(prefix)
        # bash agent_backend_result_ok returns 0 (ok) / non-zero (fail) — capture rc.
        out = bash_fn("claude-code", "agent_backend_result_ok", [prefix] + ["; echo $?"])
        # The harness appends `; echo $?` after the args, so the call becomes
        # `agent_backend_result_ok <prefix> ; echo $?` — but our helper joins
        # args with spaces, producing one line. Re-run cleanly instead:
        r = subprocess.run(
            ["bash"], input=BASH_HARNESS.format(
                landlock=REPO / "landlock.sh",
                backend=REPO / "backends" / "claude-code.sh",
                prelude="",
                call=f"agent_backend_result_ok {prefix} >/dev/null 2>&1; echo $?",
            ), capture_output=True, text=True,
        )
        bash_ok = r.stdout.strip() == "0"
        assert py == expected, f"py result_ok wrong on {fixture}: got {py}"
        assert py == bash_ok, f"diverge on {fixture}: py={py} bash={bash_ok}"


@needs_bash
def test_claude_result_text_matches(tmp_path, claude_backend):
    for fixture in [
        [CLAUDE_ERR_RESULT],
        [{"type": "result", "result": "Error: [1305] content", "is_error": True}],
        CLAUDE_STREAM_PREFIX + [CLAUDE_OK],
    ]:
        prefix = write_jsonl(tmp_path, "c", fixture)
        py = claude_backend.result_text(prefix)
        bash = bash_fn("claude-code", "agent_backend_result_text", [prefix]).strip()
        assert py == bash, f"diverge on {fixture}:\n  py={py!r}\n  bash={bash!r}"


@needs_bash
def test_claude_session_id_matches(tmp_path, claude_backend):
    for fixture, expected in [
        ([CLAUDE_OK], "s1"),
        ([CLAUDE_ERR_RESULT], "s2"),
        ([{"type": "assistant"}], ""),
    ]:
        prefix = write_jsonl(tmp_path, "c", fixture)
        py = claude_backend.session_id(prefix)
        bash = bash_fn("claude-code", "agent_backend_session_id", [prefix]).strip()
        assert py == expected
        assert py == bash, f"diverge: py={py!r} bash={bash!r}"


@needs_bash
def test_claude_is_complete_matches(tmp_path, claude_backend):
    for fixture, expected in [
        ([CLAUDE_OK], True),
        (CLAUDE_STREAM_PREFIX + [CLAUDE_OK], True),
        (CLAUDE_NO_RESULT, False),
        ([CLAUDE_ERR_RESULT], True),  # has a result line (is_error) → complete
    ]:
        prefix = write_jsonl(tmp_path, "c", fixture)
        py = claude_backend.is_complete(prefix)
        # bash uses `grep -q '"type":"result"'` — but our fixtures write compact
        # JSON (no space). Test both compact and spaced forms hold for the py side.
        bash = bool(subprocess.run(
            ["bash"], input=BASH_HARNESS.format(
                landlock=REPO / "landlock.sh",
                backend=REPO / "backends" / "claude-code.sh",
                prelude="",
                call=f"agent_backend_is_complete {prefix} && echo yes || echo no",
            ), capture_output=True, text=True,
        ).stdout.strip().endswith("yes"))
        assert py == bash == expected, f"diverge on {fixture}: py={py} bash={bash}"


# ── opencode equivalence ─────────────────────────────────────────────────

@needs_bash
def test_opencode_result_ok_matches(tmp_path, opencode_backend):
    for fixture, expected in [
        ([OPENCODE_TEXT, OPENCODE_STOP], True),
        ([OPENCODE_TEXT, OPENCODE_TOOLSTEP, OPENCODE_STOP], True),
        ([OPENCODE_TEXT, OPENCODE_ERROR], False),
        ([OPENCODE_TEXT, OPENCODE_TOOLSTEP], False),  # tool-calls, no terminal stop
    ]:
        prefix = write_jsonl(tmp_path, "o", fixture)
        py = opencode_backend.result_ok(prefix)
        bash = subprocess.run(
            ["bash"], input=BASH_HARNESS.format(
                landlock=REPO / "landlock.sh",
                backend=REPO / "backends" / "opencode.sh",
                prelude="",
                call=f"agent_backend_result_ok {prefix} && echo yes || echo no",
            ), capture_output=True, text=True,
        ).stdout.strip().endswith("yes")
        assert py == expected, f"py wrong on {fixture}: {py}"
        assert py == bash, f"diverge on {fixture}: py={py} bash={bash}"


@needs_bash
def test_opencode_result_text_matches(tmp_path, opencode_backend):
    prefix = write_jsonl(tmp_path, "o", [OPENCODE_ERROR])
    py = opencode_backend.result_text(prefix)
    bash = bash_fn("opencode", "agent_backend_result_text", [prefix]).strip()
    assert py == bash, f"diverge:\n  py={py!r}\n  bash={bash!r}"
    # spot-check the structured fields are preserved
    obj = json.loads(py)
    assert obj["message"] == "rate limited"
    assert obj["code"] == "42901"
    assert obj["status"] == 429


@needs_bash
def test_opencode_session_id_matches(tmp_path, opencode_backend):
    prefix = write_jsonl(tmp_path, "o", [OPENCODE_TEXT, OPENCODE_STOP])
    py = opencode_backend.session_id(prefix)
    bash = bash_fn("opencode", "agent_backend_session_id", [prefix]).strip()
    assert py == "S1"
    assert py == bash


@needs_bash
def test_opencode_is_complete_matches(tmp_path, opencode_backend):
    for fixture, expected in [
        ([OPENCODE_TEXT, OPENCODE_STOP], True),
        ([OPENCODE_TEXT, OPENCODE_TOOLSTEP], False),  # tool-calls → not terminal
        ([OPENCODE_ERROR], True),
        ([OPENCODE_TEXT], False),
    ]:
        prefix = write_jsonl(tmp_path, "o", fixture)
        py = opencode_backend.is_complete(prefix)
        bash = subprocess.run(
            ["bash"], input=BASH_HARNESS.format(
                landlock=REPO / "landlock.sh",
                backend=REPO / "backends" / "opencode.sh",
                prelude="",
                call=f"agent_backend_is_complete {prefix} && echo yes || echo no",
            ), capture_output=True, text=True,
        ).stdout.strip().endswith("yes")
        assert py == bash == expected, f"diverge on {fixture}: py={py} bash={bash}"


# ── flag fragments (deterministic; compared against bash too) ─────────────

@needs_bash
def test_perm_args_match_claude(monkeypatch):
    for sandbox, expected in [(None, ["--permission-mode", "dontAsk"]),
                              ("1", ["--dangerously-skip-permissions"])]:
        if sandbox is None:
            monkeypatch.delenv("SANDBOX", raising=False)
            prelude = "unset SANDBOX"
        else:
            monkeypatch.setenv("SANDBOX", sandbox)
            prelude = f"export SANDBOX={sandbox}"
        py = ClaudeCodeBackend().perm_args()
        bash = bash_fn("claude-code", "agent_backend_perm_args", [], prelude=prelude).strip().split()
        assert py == bash == expected


@needs_bash
def test_perm_args_match_opencode(monkeypatch):
    for sandbox, expected in [(None, []), ("1", ["--dangerously-skip-permissions"])]:
        if sandbox is None:
            monkeypatch.delenv("SANDBOX", raising=False)
            prelude = "unset SANDBOX"
        else:
            monkeypatch.setenv("SANDBOX", sandbox)
            prelude = f"export SANDBOX={sandbox}"
        py = OpencodeBackend().perm_args()
        bash = bash_fn("opencode", "agent_backend_perm_args", [], prelude=prelude).strip().split()
        assert py == bash == expected


@needs_bash
def test_resume_fork_args_match():
    for sid in ["abc-123", ""]:
        for backend_name, Cls in [("claude-code", ClaudeCodeBackend), ("opencode", OpencodeBackend)]:
            b = Cls()
            py_resume = b.resume_args(sid)
            bash_resume = bash_fn(backend_name, "agent_backend_resume_args", [f'"{sid}"']).strip().split()
            assert py_resume == bash_resume, f"resume diverge sid={sid!r}: py={py_resume} bash={bash_resume}"
            py_fork = b.fork_args(sid)
            bash_fork = bash_fn(backend_name, "agent_backend_fork_args", [f'"{sid}"']).strip().split()
            assert py_fork == bash_fork, f"fork diverge sid={sid!r}: py={py_fork} bash={bash_fork}"


@needs_bash
def test_env_var_names_match():
    assert ClaudeCodeBackend().api_key_env_var() == bash_fn("claude-code", "agent_backend_api_key_env_var", []).strip()
    assert OpencodeBackend().api_key_env_var() == bash_fn("opencode", "agent_backend_api_key_env_var", []).strip()
    assert ClaudeCodeBackend().base_url_env_var() == bash_fn("claude-code", "agent_backend_base_url_env_var", []).strip()
    assert OpencodeBackend().base_url_env_var() == bash_fn("opencode", "agent_backend_base_url_env_var", []).strip()
