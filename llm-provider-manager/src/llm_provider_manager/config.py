"""Load and validate the user's provider config (JSONC supported).

JSONC = JSON with comments. We strip `//` line comments and `/* */` block
comments before handing the text to `json.loads`, so users may keep comments
in `providers.jsonc`. String literals are respected (a `//` inside a string
is not treated as a comment).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
from pathlib import Path

from .schema import Config, StrictConfigError

# Permit reading group/other-readable configs but warn loudly: the file
# contains real API keys.
INSECURE_PERM_WARN = 0o077  # any group/other read/write/exec bits
_HELD_DESCRIPTOR_READ_LOCK = threading.Lock()


def _strip_jsonc_comments(text: str, *, strict: bool = False) -> str:
    """Remove // and /* */ comments from JSONC, respecting string literals."""
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
                # line comment
                j = text.find("\n", i)
                i = n if j == -1 else j
                continue
            if nxt == "*":
                # block comment
                j = text.find("*/", i + 2)
                if j == -1:
                    if strict:
                        raise StrictConfigError(
                            code="invalid_jsonc",
                            path="$",
                        )
                    i = n
                else:
                    i = j + 2
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def parse_text(
    text: str,
    source: str = "<string>",
    *,
    strict: bool = False,
) -> Config:
    cleaned = _strip_jsonc_comments(text, strict=strict)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"{source}: invalid JSON ({e})") from e
    if not isinstance(data, dict):
        raise ValueError(f"{source}: top-level must be an object")
    return Config.from_dict(data, strict=strict)


def load(
    path: str | os.PathLike[str],
    *,
    file_descriptor: int | None = None,
    expected_sha256: str | None = None,
    strict: bool = False,
) -> Config:
    p = Path(path)
    descriptor = (
        os.dup(file_descriptor)
        if file_descriptor is not None
        else os.open(
            p,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError(f"{p}: provider config is not a regular file")
        pread = getattr(os, "pread", None)
        if file_descriptor is not None and callable(pread):
            offset = 0
            content = bytearray()
            while chunk := pread(descriptor, 64 * 1024, offset):
                content.extend(chunk)
                offset += len(chunk)
            text = bytes(content).decode("utf-8")
        elif file_descriptor is not None:
            with _HELD_DESCRIPTOR_READ_LOCK:
                original_offset = os.lseek(
                    descriptor,
                    0,
                    os.SEEK_CUR,
                )
                try:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    content = bytearray()
                    while chunk := os.read(descriptor, 64 * 1024):
                        content.extend(chunk)
                    text = bytes(content).decode("utf-8")
                finally:
                    os.lseek(
                        descriptor,
                        original_offset,
                        os.SEEK_SET,
                    )
        else:
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                descriptor = -1
                text = stream.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        expected_sha256 is not None
        and hashlib.sha256(text.encode("utf-8")).hexdigest()
        != expected_sha256
    ):
        raise ValueError(f"{p}: provider config changed after startup")
    return parse_text(text, source=str(p), strict=strict)


def check_permissions(path: str | os.PathLike[str]) -> list[str]:
    """Return a list of warnings about insecure file permissions (may be empty)."""
    warnings: list[str] = []
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return warnings
    if st.st_mode & INSECURE_PERM_WARN:
        warnings.append(
            f"{path}: world/group-readable (mode {oct(st.st_mode & 0o777)}); "
            f"it contains API keys — run: chmod 600 {path}"
        )
    return warnings


# A token that unambiguously marks a placeholder in the example config so the
# loader refuses to use a half-filled example as if it were real.
def looks_unfilled(config: Config) -> bool:
    placeholder_re = re.compile(r"REPLACE-ME", re.IGNORECASE)
    for p in config.providers:
        for k in p.keys:
            if placeholder_re.search(k.key):
                return True
    return False
