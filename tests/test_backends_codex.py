"""Codex backend — parser + flag-fragment tests.

Exercises ``CodexBackend``'s jsonl/out/err parsing (the load-bearing
correctness claims the engine's success/failure + session reuse decisions +
error classification depend on) and its flag vocabulary, against hand-crafted
fixtures matching what ``_codex_runner.py`` writes:

  - ``thread.started`` compat event   → ``session_id``
  - ``turn.completed`` compat event    → ``is_complete`` / ``result_ok``
  - ``<prefix>.out``                   → ``result_body``
  - ``<prefix>.err``                   → ``result_text``

The agent subprocess (``_codex_runner.py``) and the SDK are NOT invoked — only
the pure-Python parsers are tested, same scope as ``test_backends_jq_equiv``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
import sys  # noqa: E402
sys.path.insert(0, str(ROOT))

from agent_runner.backends.codex import CodexBackend  # noqa: E402
from agent_runner.config import Config  # noqa: E402


# ── fixture helpers ───────────────────────────────────────────────────────

def _write(prefix: Path, suffix: str, content: str) -> None:
    prefix.parent.mkdir(parents=True, exist_ok=True)
    with open(f"{prefix}{suffix}", "w", encoding="utf-8") as f:
        f.write(content)


def write_jsonl(prefix: Path, lines: list[dict | str]) -> None:
    _write(prefix, ".jsonl", "".join(
        (ln if isinstance(ln, str) else json.dumps(ln, separators=(",", ":")))
        + "\n"
        for ln in lines
    ))


THREAD_STARTED = {"type": "thread.started", "thread_id": "th_abc"}
TURN_DONE = {"type": "turn.completed", "thread_id": "th_abc",
             "turn_id": "t1", "status": "completed"}
TURN_FAIL = {"type": "turn.completed", "thread_id": "th_abc",
             "turn_id": "t1", "status": "failed"}


@pytest.fixture
def be() -> CodexBackend:
    return CodexBackend(config=Config(config_overrides={}))


@pytest.fixture
def prefix(tmp_path: Path) -> Path:
    return tmp_path / "run"


# ── result_ok / is_complete ───────────────────────────────────────────────

def test_result_ok_completed(be, prefix):
    write_jsonl(prefix, [THREAD_STARTED, TURN_DONE])
    assert be.result_ok(str(prefix)) is True
    assert be.is_complete(str(prefix)) is True


def test_result_ok_failed(be, prefix):
    write_jsonl(prefix, [THREAD_STARTED, TURN_FAIL])
    assert be.result_ok(str(prefix)) is False
    # turn.completed still marks completion (early-exit) regardless of status
    assert be.is_complete(str(prefix)) is True


def test_result_ok_no_terminal(be, prefix):
    write_jsonl(prefix, [THREAD_STARTED, {"type": "item/completed"}])
    assert be.result_ok(str(prefix)) is False
    assert be.is_complete(str(prefix)) is False


def test_result_ok_empty(be, prefix):
    assert be.result_ok(str(prefix)) is False
    assert be.is_complete(str(prefix)) is False


# ── session_id ────────────────────────────────────────────────────────────

def test_session_id(be, prefix):
    write_jsonl(prefix, [THREAD_STARTED, TURN_DONE])
    assert be.session_id(str(prefix)) == "th_abc"


def test_session_id_none(be, prefix):
    write_jsonl(prefix, [{"type": "item/completed"}])
    assert be.session_id(str(prefix)) == ""


# ── result_body (reads <prefix>.out) ──────────────────────────────────────

def test_result_body(be, prefix):
    write_jsonl(prefix, [THREAD_STARTED, TURN_DONE])
    _write(prefix, ".out", "The final answer.")
    assert be.result_body(str(prefix)) == "The final answer."


def test_result_body_missing(be, prefix):
    assert be.result_body(str(prefix)) == ""


# ── result_text (reads <prefix>.err) ───────────────────────────────────────

def test_result_text_from_err(be, prefix):
    write_jsonl(prefix, [THREAD_STARTED, TURN_FAIL])
    _write(prefix, ".err", "boom\ntrace\n")
    out = be.result_text(str(prefix))
    assert json.loads(out) == {"message": "boom\ntrace\n"}


def test_result_text_default(be, prefix):
    write_jsonl(prefix, [THREAD_STARTED, TURN_FAIL])
    assert json.loads(be.result_text(str(prefix))) == {"message": "Codex request failed"}


# ── flag fragments ────────────────────────────────────────────────────────

def test_perm_args_default(be):
    # AR_SANDBOX=False (default) → workspace-write
    assert be.perm_args() == ["--sandbox", "workspace-write"]


def test_perm_args_sandbox_true():
    b = CodexBackend(config=Config(config_overrides={"sandbox": True}))
    assert b.perm_args() == ["--sandbox", "danger-full-access"]


def test_model_args_primary(be):
    b = CodexBackend(config=Config(config_overrides={"primary_model": "gpt-5"}))
    assert b.model_args("primary") == ["--model", "gpt-5"]


def test_model_args_resolved_wins():
    b = CodexBackend(config=Config(config_overrides={"primary_model": "gpt-5"}))
    assert b.model_args("primary", resolved_model="o3") == ["--model", "o3"]


def test_model_args_empty(be):
    assert be.model_args("primary") == []


def test_resume_args(be):
    assert be.resume_args("sid1") == ["--resume", "sid1"]
    assert be.resume_args("") == []


def test_fork_args(be):
    assert be.fork_args("sid1") == ["--resume", "sid1", "--fork-session"]
    assert be.fork_args("") == []


# ── key-pool opt-out ──────────────────────────────────────────────────────

def test_opts_out_of_key_pool(be):
    # codex reads no env var from the pool → invoke inherits os.environ as-is
    assert be.api_key_env_var() == ""
    assert be.base_url_env_var() == ""
