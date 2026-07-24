#!/usr/bin/env python3
"""Codex agent runner — the spawned "binary" the ``codex`` backend drives.

File contract (consumed by ``agent_runner.backends.codex.CodexBackend``):

  - <prefix>.jsonl: streamed Codex events, one compact JSON object per line
  - <prefix>.out:   final assistant response text
  - <prefix>.err:   runner stderr and Python exceptions

It drives Codex through the Python SDK on the premise that Codex itself is
installed and configured by the user(web search, network access, the
``codex`` binary, auth). This runner owns the agent-runner contract: prompt,
session(resume/fork), model, the event log, and the sandbox mode(bridging
agent-runner's generic ``AR_SANDBOX`` bool to the Codex SDK's ``Sandbox``
object). It does NOT manage the rest of Codex's own configuration.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
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
    run.add_argument("--resume")
    run.add_argument("--fork-session", action="store_true")
    run.add_argument("--model")
    run.add_argument("--sandbox", default="workspace-write")
    run.add_argument("extra", nargs=argparse.REMAINDER)
    return p.parse_args(argv)


def _prefix(args: argparse.Namespace) -> Path:
    return Path(args.output_dir) / args.log_name


def _artifact(prefix: Path, suffix: str) -> Path:
    return Path(f"{prefix}{suffix}")


def _sandbox_obj(sandbox: str | None, Sandbox) -> Any:
    """agent-runner 的 sandbox 模式字符串 → Codex SDK ``Sandbox`` 对象。

    无别名归一表:``perm_args`` 已只发 canonical 模式(danger-full-access /
    workspace-write),这里直连 SDK 属性;未知/空 → None(交由 SDK 用其默认,
    即用户在 codex 配置里设的 sandbox/approval 策略)。"""
    def attr(name: str) -> Any:
        value = getattr(Sandbox, name, None)
        return value() if callable(value) else value
    if sandbox == "danger-full-access":
        return attr("full_access")
    if sandbox == "workspace-write":
        return attr("workspace_write")
    if sandbox == "read-only":
        return attr("read_only")
    return None


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
        from openai_codex import Codex, Sandbox
    except Exception as exc:
        raise RuntimeError("Codex runner requires installing the 'openai-codex' package") from exc

    cwd = str(Path(args.project_root).resolve())
    model = args.model or None
    kwargs: dict[str, Any] = {
        "cwd": cwd,
        "model": model,
        "sandbox": _sandbox_obj(args.sandbox, Sandbox),
    }
    kwargs = {k: v for k, v in kwargs.items() if v is not None}

    with _artifact(prefix, ".jsonl").open("w", encoding="utf-8") as log, (
        _artifact(prefix, ".err")
    ).open("w", encoding="utf-8") as err:
        try:
            with Codex() as codex:
                if args.resume:
                    if args.fork_session:
                        thread = codex.thread_fork(args.resume, **kwargs)
                    else:
                        thread = codex.thread_resume(args.resume, **kwargs)
                else:
                    thread = codex.thread_start(**kwargs)

                thread_id = _sdk_thread_id(thread)
                if not thread_id:
                    raise RuntimeError("SDK Thread did not expose a thread id")
                write_compat_event(log, "thread.started", thread_id=thread_id)

                handle = thread.turn(prompt, **kwargs)
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
                return 0
        except Exception:
            traceback.print_exc(file=err)
            raise


def run(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    prefix = _prefix(args)
    prefix.parent.mkdir(parents=True, exist_ok=True)

    started = time.time()
    try:
        return run_sdk(args, args.prompt, prefix)
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
