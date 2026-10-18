"""Read-only tools the model can call. Every path goes through Workspace.resolve.

Each tool returns (text, observed), where observed maps a file to the line numbers the
model was shown, so we can check later that cited evidence was really seen.
"""
from __future__ import annotations

import fnmatch
import os
from pathlib import Path

from .workspace import PathRefused, Workspace

READ_MAX_LINES = 200        # per read_file call
LINE_MAX_CHARS = 400        # longer lines get cut off
SEARCH_MAX_HITS = 50
SEARCH_MAX_FILE_BYTES = 2_000_000
SEARCH_MAX_FILES = 30_000
PATTERN_MAX_CHARS = 200
LIST_MAX_ENTRIES = 200


class ToolError(Exception):
    pass


def _lines(p: Path) -> list[str]:
    try:
        raw = p.read_bytes()
    except OSError as exc:
        raise ToolError(f"cannot read file: {exc.strerror}") from exc
    if b"\x00" in raw[:4096]:
        raise ToolError("binary file; not readable as text")
    return raw.decode("utf-8", errors="replace").splitlines()


def _clip(s: str) -> str:
    return s if len(s) <= LINE_MAX_CHARS else s[:LINE_MAX_CHARS] + " …[truncated]"


def read_file(ws: Workspace, path: str, start_line: int = 1, end_line: int | None = None):
    p = ws.resolve(path)
    if not p.is_file():
        raise ToolError(f"not a file: {path!r}")
    lines = _lines(p)
    n = len(lines)
    if start_line < 1:
        raise ToolError("start_line must be >= 1")
    if n == 0:
        return f"{ws.rel(p)} is empty (0 lines)", {}
    if start_line > n:
        raise ToolError(f"{ws.rel(p)} has {n} lines; start_line {start_line} is past the end")
    if end_line is None:
        end_line = start_line + READ_MAX_LINES - 1
    if end_line < start_line:
        raise ToolError("end_line must be >= start_line")
    end_line = min(end_line, n, start_line + READ_MAX_LINES - 1)
    rel = ws.rel(p)
    body = "\n".join(f"{i:>5}| {_clip(lines[i - 1])}" for i in range(start_line, end_line + 1))
    more = f"\n[file has {n} lines; showing {start_line}-{end_line}]"
    return f"{rel}\n{body}{more}", {rel: set(range(start_line, end_line + 1))}


def search_code(ws: Workspace, pattern: str, path: str = ".", glob: str = "*.py",
                case_sensitive: bool = True):
    if not pattern or len(pattern) > PATTERN_MAX_CHARS:
        raise ToolError(f"pattern must be 1..{PATTERN_MAX_CHARS} characters")
    if not glob or "/" in glob or len(glob) > 50:
        raise ToolError("glob must be a file-name pattern like '*.py' (no '/')")
    base = ws.resolve(path)
    needle = pattern if case_sensitive else pattern.lower()
    hits: list[str] = []
    observed: dict[str, set[int]] = {}
    files_seen = 0

    if base.is_file():
        candidates = [base]
    elif base.is_dir():
        candidates = []
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            dirnames.sort()
            for fn in sorted(filenames):
                if fnmatch.fnmatch(fn, glob):
                    candidates.append(Path(dirpath) / fn)
    else:
        raise ToolError(f"no such path: {path!r}")

    truncated = False
    for f in candidates:
        files_seen += 1
        if files_seen > SEARCH_MAX_FILES:
            truncated = True
            break
        try:
            rel_in = f.relative_to(ws.root).as_posix()
            real = ws.resolve(rel_in)  # skips withheld files and anything outside the workspace
        except (PathRefused, ValueError):
            continue
        if not real.is_file() or real.stat().st_size > SEARCH_MAX_FILE_BYTES:
            continue
        try:
            lines = _lines(real)
        except ToolError:
            continue
        for i, line in enumerate(lines, 1):
            hay = line if case_sensitive else line.lower()
            if needle in hay:
                rel = ws.rel(real)
                hits.append(f"{rel}:{i}: {_clip(line.strip())}")
                observed.setdefault(rel, set()).add(i)
                if len(hits) >= SEARCH_MAX_HITS:
                    truncated = True
                    break
        if len(hits) >= SEARCH_MAX_HITS:
            break
    if not hits:
        return f"no matches for {pattern!r} in {ws.rel(base)} ({glob})", {}
    tail = f"\n[stopped at {SEARCH_MAX_HITS} hits; narrow the search]" if truncated else ""
    return "\n".join(hits) + tail, observed


def list_dir(ws: Workspace, path: str = "."):
    p = ws.resolve(path)
    if not p.is_dir():
        raise ToolError(f"not a directory: {path!r}")
    entries = []
    for child in sorted(p.iterdir(), key=lambda c: c.name):
        rel = (p / child.name).relative_to(ws.root).as_posix()
        try:
            ws.resolve(rel)
        except PathRefused:
            continue  # don't list withheld entries
        entries.append(child.name + ("/" if child.is_dir() else ""))
    shown = entries[:LIST_MAX_ENTRIES]
    tail = f"\n[{len(entries) - LIST_MAX_ENTRIES} more entries not shown]" if len(entries) > LIST_MAX_ENTRIES else ""
    return f"{ws.rel(p)}/\n" + "\n".join(shown) + tail, {}


# name -> (function, spec shown to the model)
TOOLS = {
    "read_file": (read_file, {
        "description": ("Read a line range of a file in the repository (repo-relative path). "
                        f"Returns numbered lines; at most {READ_MAX_LINES} lines per call."),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repo-relative POSIX path, e.g. 'radicale/app/get.py'"},
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
            },
            "required": ["path", "start_line", "end_line"],
            "additionalProperties": False,
        },
    }),
    "search_code": (search_code, {
        "description": ("Literal (non-regex) substring search over files under `path` whose file name "
                        f"matches `glob`. Returns up to {SEARCH_MAX_HITS} 'path:line: text' hits."),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Literal text to find"},
                "path": {"type": "string", "description": "Directory or file to search; '.' for the whole repo"},
                "glob": {"type": "string", "description": "File-name pattern, default '*.py'"},
                "case_sensitive": {"type": "boolean"},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    }),
    "list_dir": (list_dir, {
        "description": f"List a directory of the repository (at most {LIST_MAX_ENTRIES} entries).",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Repo-relative directory; '.' for root"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    }),
}

SUBMIT_TOOL = {
    "description": ("Submit the final classification for the CURRENT alert. Call it alone (not together "
                    "with other tools). Evidence ranges must be lines you have actually been shown."),
    "parameters": {
        "type": "object",
        "properties": {
            "label": {"type": "integer", "enum": [0, 1],
                      "description": "1 = potential vulnerability, 0 = not a vulnerability"},
            "rationale": {"type": "string",
                          "description": "1-4 sentences citing specific lines, e.g. 'line 52: ...'"},
            "evidence": {
                "type": "array", "minItems": 1, "maxItems": 5,
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "start_line": {"type": "integer", "minimum": 1},
                        "end_line": {"type": "integer", "minimum": 1},
                    },
                    "required": ["path", "start_line", "end_line"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["label", "rationale", "evidence"],
        "additionalProperties": False,
    },
}


def function_specs() -> list[dict]:
    specs = [{"type": "function", "function": {"name": n, **spec}} for n, (_, spec) in TOOLS.items()]
    specs.append({"type": "function", "function": {"name": "submit_verdict", **SUBMIT_TOOL}})
    return specs
