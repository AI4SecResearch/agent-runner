#!/usr/bin/env python3
"""Codex agent runner.

This runner provides the same file contract expected by runner.sh:

  - <prefix>.jsonl: streamed Codex events, one compact JSON object per line
  - <prefix>.out:   final assistant response text
  - <prefix>.err:   runner stderr and Python exceptions

It drives Codex only through the Python SDK.
"""

from __future__ import annotations

import argparse
import dataclasses
import fcntl
import json
import os
import shutil
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Iterable


def _json_line(obj: Any) -> str:
    return json.dumps(_to_jsonable(obj), ensure_ascii=False, separators=(",", ":")) + "\n"


def _to_jsonable(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_to_jsonable(v) for v in obj]
    if dataclasses.is_dataclass(obj):
        return _to_jsonable(dataclasses.asdict(obj))
    model_dump = getattr(obj, "model_dump", None)
    if callable(model_dump):
        return _to_jsonable(model_dump(mode="json"))
    as_dict = getattr(obj, "dict", None)
    if callable(as_dict):
        return _to_jsonable(as_dict())
    if hasattr(obj, "__dict__"):
        return _to_jsonable(vars(obj))
    return str(obj)


def _deep_get(obj: Any, path: Iterable[str]) -> Any:
    cur = obj
    for key in path:
        if isinstance(cur, dict):
            cur = cur.get(key)
        else:
            cur = getattr(cur, key, None)
        if cur is None:
            return None
    return cur


def _event_method(event: dict[str, Any]) -> str:
    return str(event.get("method") or event.get("type") or "")


def _event_params(event: dict[str, Any]) -> dict[str, Any]:
    params = event.get("params")
    if isinstance(params, dict):
        return params
    payload = event.get("payload")
    if isinstance(payload, dict):
        return payload
    return event


def _should_log_event(event: dict[str, Any]) -> bool:
    return _event_method(event) not in {"item/agentMessage/delta", "agentMessageDelta"}


def _extract_text_fragment(event: dict[str, Any]) -> str:
    """Best-effort extraction of assistant text from SDK events."""
    method = _event_method(event)
    params = _event_params(event)

    if method == "item/completed":
        item = params.get("item")
        item_phase = _deep_get(item, ["phase"]) if isinstance(item, dict) else None
        item_type = _deep_get(item, ["type"]) if isinstance(item, dict) else None
        if item_phase != "final_answer":
            return ""
        if item_type is not None and item_type != "agentMessage":
            return ""
        return _extract_text_from_item(item)

    return ""


def _extract_text_from_item(item: Any) -> str:
    item = _to_jsonable(item)
    if not isinstance(item, dict):
        return ""

    candidates = [
        ["text"],
        ["message", "text"],
        ["message", "content"],
        ["content"],
        ["outputText"],
        ["finalResponse"],
    ]
    for path in candidates:
        val = _deep_get(item, path)
        if isinstance(val, str):
            return val

    content = item.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                val = part.get("text") or part.get("content")
                if isinstance(val, str):
                    parts.append(val)
        return "".join(parts)

    return ""


def _normalize_prompt(prompt: str) -> str:
    if prompt.startswith("/fix-json "):
        path = prompt[len("/fix-json ") :]
        return f"请修复 JSON 文件，使其成为严格合法 JSON。只修改该文件，不改变语义。文件路径：{path}"
    return prompt


def _session_file(output_dir: Path) -> Path:
    return output_dir / "codex-sessions.tsv"


def lookup_session(output_dir: Path, logical_id: str) -> str | None:
    path = _session_file(output_dir)
    if not path.exists():
        return None
    result: str | None = None
    with path.open("r", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        try:
            for line in f:
                cols = line.rstrip("\n").split("\t")
                if len(cols) >= 2 and cols[0] == logical_id and cols[1]:
                    result = cols[1]
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    return result


def record_session(output_dir: Path, logical_id: str | None, thread_id: str | None) -> None:
    if not logical_id or not thread_id:
        return
    path = _session_file(output_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(f"{logical_id}\t{thread_id}\n")
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def sandbox_mode(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().replace("_", "-")
    aliases = {
        "full-access": "danger-full-access",
        "danger-full-access": "danger-full-access",
        "workspace-write": "workspace-write",
        "read-only": "read-only",
        "readonly": "read-only",
    }
    return aliases.get(normalized, normalized)


def config_overrides(args: argparse.Namespace) -> dict[str, Any] | None:
    cfg: dict[str, Any] = {}
    if args.web_search:
        cfg["web_search"] = args.web_search
    if args.network_access:
        cfg["sandbox_workspace_write"] = {"network_access": True}
    return cfg or None


def write_event(log, event: Any) -> dict[str, Any]:
    data = _to_jsonable(event)
    if not isinstance(data, dict):
        data = {"type": type(event).__name__, "value": data}
    if _should_log_event(data):
        log.write(_json_line(data))
        log.flush()
    return data


def write_compat_event(log, event_type: str, **params: Any) -> None:
    log.write(_json_line({"type": event_type, **params}))
    log.flush()


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run one Codex agent request")
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run")
    run.add_argument("--prompt", required=True)
    run.add_argument("--output-dir", required=True)
    run.add_argument("--log-name", required=True)
    run.add_argument("--project-root", default=os.getcwd())
    run.add_argument("--session-id")
    run.add_argument("--resume")
    run.add_argument("--fork-session", action="store_true")
    run.add_argument("--model")
    run.add_argument("--codex-bin", default=os.environ.get("CODEX_BIN", "codex"))
    run.add_argument("--sandbox", default=os.environ.get("CODEX_SANDBOX") or "danger-full-access")
    run.add_argument("--web-search", default=os.environ.get("CODEX_WEB_SEARCH"))
    run.add_argument("--network-access", action="store_true", default=_truthy(os.environ.get("CODEX_NETWORK_ACCESS")))
    run.add_argument("extra", nargs=argparse.REMAINDER)
    return p.parse_args(argv)


def _truthy(value: str | None) -> bool:
    return str(value or "").lower() in {"1", "true", "yes", "y", "on"}


def _prefix(args: argparse.Namespace) -> Path:
    return Path(args.output_dir) / args.log_name


def _artifact(prefix: Path, suffix: str) -> Path:
    return Path(f"{prefix}{suffix}")


def _resolve_model(args: argparse.Namespace) -> str | None:
    return args.model or os.environ.get("CODEX_MODEL") or None


def _resolve_codex_bin(codex_bin: str | None) -> str | None:
    if not codex_bin:
        return None
    if os.path.sep in codex_bin:
        return codex_bin
    return shutil.which(codex_bin) or codex_bin


def _sdk_thread_id(thread: Any) -> str | None:
    for attr in ("id", "thread_id", "_id", "_thread_id"):
        val = getattr(thread, attr, None)
        if isinstance(val, str) and val:
            return val
    try:
        data = _to_jsonable(thread.read(include_turns=False))
        return _deep_get(data, ["thread", "id"]) or _deep_get(data, ["id"])
    except Exception:
        return None


def run_sdk(args: argparse.Namespace, prompt: str, prefix: Path) -> int:
    try:
        from openai_codex import Codex, CodexConfig, Sandbox
    except Exception as exc:
        raise RuntimeError("Codex runner requires installing the 'openai-codex' package") from exc

    def sdk_sandbox() -> Any:
        def _sandbox_attr(name: str) -> Any:
            value = getattr(Sandbox, name, None)
            return value() if callable(value) else value

        mode = sandbox_mode(args.sandbox)
        if mode == "danger-full-access":
            return _sandbox_attr("full_access")
        if mode == "workspace-write":
            return _sandbox_attr("workspace_write")
        if mode == "read-only":
            return _sandbox_attr("read_only")
        return None

    codex_bin = _resolve_codex_bin(args.codex_bin)
    config = CodexConfig(codex_bin=codex_bin) if codex_bin else None
    kwargs: dict[str, Any] = {
        "cwd": str(Path(args.project_root).resolve()),
        "model": _resolve_model(args),
        "sandbox": sdk_sandbox(),
        "config": config_overrides(args),
    }
    kwargs = {k: v for k, v in kwargs.items() if v is not None}

    with _artifact(prefix, ".jsonl").open("w", encoding="utf-8") as log, (
        _artifact(prefix, ".err")
    ).open("w", encoding="utf-8") as err:
        try:
            with Codex(config=config) as codex:
                if args.resume:
                    actual = lookup_session(Path(args.output_dir), args.resume) or args.resume
                    if args.fork_session:
                        thread = codex.thread_fork(actual, **kwargs)
                    else:
                        thread = codex.thread_resume(actual, **kwargs)
                else:
                    thread = codex.thread_start(**kwargs)

                thread_id = _sdk_thread_id(thread)
                if not thread_id:
                    raise RuntimeError("SDK Thread did not expose a thread id")
                write_compat_event(log, "thread.started", thread_id=thread_id)

                turn_kwargs = {
                    "cwd": str(Path(args.project_root).resolve()),
                    "model": _resolve_model(args),
                    "sandbox": sdk_sandbox(),
                }
                turn_kwargs = {k: v for k, v in turn_kwargs.items() if v is not None}
                handle = thread.turn(prompt, **turn_kwargs)
                completed_fragments: list[str] = []
                last_turn_id = None
                status = "completed"
                for event in handle.stream():
                    data = write_event(log, event)
                    fragment = _extract_text_fragment(data)
                    if fragment:
                        completed_fragments.append(fragment)
                    params = _event_params(data)
                    last_turn_id = params.get("turnId") or _deep_get(params, ["turn", "id"]) or last_turn_id
                    if _event_method(data) in {"turn/completed", "turn.completed"}:
                        status = _deep_get(params, ["turn", "status"]) or params.get("status") or status

                final_text = "".join(completed_fragments)
                _artifact(prefix, ".out").write_text(final_text, encoding="utf-8")
                write_compat_event(log, "turn.completed", thread_id=thread_id, turn_id=last_turn_id, status=status)
                record_session(Path(args.output_dir), args.session_id, thread_id)
                return 0
        except Exception:
            traceback.print_exc(file=err)
            raise


def run(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    prefix = _prefix(args)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    prompt = _normalize_prompt(args.prompt)

    started = time.time()
    try:
        return run_sdk(args, prompt, prefix)
    except Exception as exc:
        err_path = _artifact(prefix, ".err")
        with err_path.open("a", encoding="utf-8") as err:
            print(f"[codex_runner] failed after {time.time() - started:.1f}s: {exc}", file=err)
            traceback.print_exc(file=err)
        if not _artifact(prefix, ".jsonl").exists():
            _artifact(prefix, ".jsonl").write_text("", encoding="utf-8")
        if not _artifact(prefix, ".out").exists():
            _artifact(prefix, ".out").write_text("", encoding="utf-8")
        return 1


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.command == "run":
        return run(args)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
