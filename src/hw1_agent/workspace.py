"""A throwaway copy of --input that hides what the agent shouldn't see."""
from __future__ import annotations

import fnmatch
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath

ALWAYS_WITHHELD_DIRS = (".git",)
RUN_WITHHELD_GLOBS = ("expectedresults*.csv",)
CASE_WITHHELD_GLOBS = ("BenchmarkTest*",)

CASE_RE = re.compile(r"^(BenchmarkTest\d+)")
STASH_SUFFIX = "-withheld"


class PathRefused(Exception):
    """Raised by resolve() when a path isn't allowed."""


def case_of(path: str) -> str | None:
    m = CASE_RE.match(PurePosixPath(path).name)
    return m.group(1) if m else None


class Workspace:
    def __init__(self, source: Path):
        self.source = Path(source).resolve()
        self.root: Path | None = None
        self.stash: Path | None = None
        self.active_case: str | None = None
        self._tmp: Path | None = None

    def __enter__(self) -> "Workspace":
        if not self.source.is_dir():
            raise FileNotFoundError(f"--input is not a directory: {self.source}")
        self._tmp = Path(tempfile.mkdtemp(prefix="hw1-ws-")).resolve()
        root = self._tmp / "repo"
        shutil.copytree(self.source, root, symlinks=True,
                        ignore=shutil.ignore_patterns(*ALWAYS_WITHHELD_DIRS))
        self.root = root.resolve()
        self.stash = self._tmp / f"repo{STASH_SUFFIX}"
        self.stash.mkdir()
        for pattern in RUN_WITHHELD_GLOBS:
            for p in sorted(self.root.rglob(pattern)):
                self._move_out(p)
        return self

    def __exit__(self, *exc) -> None:
        self.restore_case()
        self.restore_all()
        if self._tmp is not None:
            shutil.rmtree(self._tmp, ignore_errors=True)
        self.root = None

    def _move_out(self, p: Path) -> None:
        if p.is_symlink() or p.is_file():
            target = self.stash / p.relative_to(self.root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(target))

    def _move_back(self, rel_paths: list[Path]) -> None:
        for rel in rel_paths:
            src = self.stash / rel
            dst = self.root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))

    def withhold_for_case(self, alert_path: str) -> None:
        """Hide every BenchmarkTest* file except the ones for this alert's case."""
        self.restore_case()
        self.active_case = case_of(alert_path)
        moved = []
        for pattern in CASE_WITHHELD_GLOBS:
            for p in sorted(self.root.rglob(pattern)):
                if self.active_case and case_of(p.name) == self.active_case:
                    continue
                if p.is_file() or p.is_symlink():
                    moved.append(p.relative_to(self.root))
                    self._move_out(p)
        self._case_moved = moved

    def restore_case(self) -> None:
        """Put back whatever withhold_for_case() hid."""
        moved = getattr(self, "_case_moved", [])
        self._move_back(moved)
        self._case_moved = []
        self.active_case = None

    def restore_all(self) -> None:
        if self.stash is None or not self.stash.exists():
            return
        rels = [p.relative_to(self.stash) for p in sorted(self.stash.rglob("*"))
                if p.is_file() or p.is_symlink()]
        self._move_back(rels)

    def withheld(self) -> list[str]:
        if self.stash is None or not self.stash.exists():
            return []
        return sorted(str(p.relative_to(self.stash)) for p in self.stash.rglob("*") if p.is_file())

    def is_withheld_name(self, rel: PurePosixPath) -> bool:
        if any(part in ALWAYS_WITHHELD_DIRS for part in rel.parts):
            return True
        name = rel.name
        if any(fnmatch.fnmatch(name, g) for g in RUN_WITHHELD_GLOBS):
            return True
        if any(fnmatch.fnmatch(name, g) for g in CASE_WITHHELD_GLOBS):
            return case_of(name) is None or case_of(name) != self.active_case
        return False

    def resolve(self, path: str) -> Path:
        """Map a repo-relative path to a real path inside the copy, or raise PathRefused."""
        if self.root is None:
            raise RuntimeError("workspace is not open")
        if not isinstance(path, str) or "\x00" in path:
            raise PathRefused("path must be a string")
        raw = path.strip().replace("\\", "/") or "."
        pp = PurePosixPath(raw)
        if pp.is_absolute() or re.match(r"^[A-Za-z]:", raw):
            raise PathRefused(f"absolute paths are refused: {path!r}")
        if ".." in pp.parts:
            raise PathRefused(f"'..' is refused: {path!r}")
        if self.is_withheld_name(pp):
            raise PathRefused(f"no such file or not permitted: {path!r}")
        candidate = (self.root / pp).resolve()
        if not candidate.is_relative_to(self.root):
            raise PathRefused(f"resolves outside the workspace: {path!r}")
        rel_resolved = PurePosixPath(candidate.relative_to(self.root).as_posix())
        if self.is_withheld_name(rel_resolved): 
            raise PathRefused(f"no such file or not permitted: {path!r}")
        return candidate

    def rel(self, p: Path) -> str:
        return p.resolve().relative_to(self.root).as_posix() if p.resolve() != self.root else "."
