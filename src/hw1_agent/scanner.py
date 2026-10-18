"""Runs Bandit over the [scan] paths and turns its JSON output into Alerts."""
from __future__ import annotations

import glob
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

SEVERITY_FLAG = {"LOW": "low", "MEDIUM": "medium", "HIGH": "high"}
SEVERITY_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}


class ScanError(Exception):
    pass


@dataclass
class Alert:
    alert_id: str
    test_id: str
    test_name: str
    path: str
    line: int
    line_range: list[int]
    severity: str
    confidence: str
    issue_text: str
    cwe: int | None
    code: str
    status: str | None = None
    label: int | None = None
    rationale: str = ""
    evidence: list[dict] = field(default_factory=list)

    def row(self) -> dict:
        return {
            "alert_id": self.alert_id,
            "test_id": self.test_id,
            "path": self.path,
            "line": self.line,
            "severity": self.severity,
            "confidence": self.confidence,
            "label": self.label,
            "status": self.status,
            "rationale": self.rationale,
            "evidence": self.evidence,
        }


def bandit_version() -> str:
    """Version of Bandit installed for this interpreter."""
    out = subprocess.run([sys.executable, "-m", "bandit", "--version"],
                         capture_output=True, text=True, timeout=60)
    m = re.search(r"^\S+\s+(\d+\.\d+\.\d+)", out.stdout + out.stderr)
    if out.returncode != 0 or not m:
        raise ScanError(f"could not determine bandit version: {out.stdout[:200]} {out.stderr[:200]}")
    return m.group(1)


def expand(root: Path, patterns: tuple[str, ...], required: bool) -> list[str]:
    result: list[str] = []
    for pattern in patterns:
        if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            raise ScanError(f"scan pattern must be relative to --input: {pattern!r}")
        matches = sorted(glob.glob(pattern, root_dir=root, recursive=True))
        if not matches and required:
            raise ScanError(f"scan path matched nothing under {root}: {pattern!r}")
        result.extend(m for m in matches if m not in result)
    return result


def build_command(paths: list[str], exclude: list[str], min_severity: str, out_file: str) -> list[str]:
    cmd = [sys.executable, "-m", "bandit", "-q", "-r",
           "--severity-level", SEVERITY_FLAG[min_severity]]
    if exclude:
        cmd += ["-x", ",".join(exclude)]
    cmd += ["-f", "json", "-o", out_file, *paths]
    return cmd


def normalise_path(p: str) -> str:
    p = p.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-") or "x"


def run_bandit(root: Path, paths_cfg: tuple[str, ...], exclude_cfg: tuple[str, ...],
               min_severity: str, expected_version: str, target_slug: str) -> tuple[list[Alert], dict]:
    """Return the alerts sorted by (path, line, test_id), plus scan info for the trace."""
    version = bandit_version()
    if version != expected_version:
        raise ScanError(f"bandit {version} installed, config requires {expected_version}")

    paths = expand(root, paths_cfg, required=True)
    exclude = expand(root, exclude_cfg, required=False)

    with tempfile.TemporaryDirectory(prefix="hw1-bandit-") as tmp:
        out_file = str(Path(tmp) / "bandit.json")
        cmd = build_command(paths, exclude, min_severity, out_file)
        proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=900)
        # Bandit exits 1 when it finds issues, so that alone isn't a failure.
        if proc.returncode not in (0, 1):
            raise ScanError(f"bandit exited {proc.returncode}: {proc.stderr[-500:]}")
        try:
            data = json.loads(Path(out_file).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ScanError(f"bandit produced no parseable JSON (exit {proc.returncode}): "
                            f"{proc.stderr[-500:]}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ScanError("bandit JSON has no 'results' list")

    alerts: list[Alert] = []
    floor = SEVERITY_RANK[min_severity]
    for r in data["results"]:
        sev = r["issue_severity"].upper()
        if SEVERITY_RANK.get(sev, -1) < floor:
            continue
        path = normalise_path(r["filename"])
        alerts.append(Alert(
            alert_id="",
            test_id=r["test_id"],
            test_name=r.get("test_name", ""),
            path=path,
            line=int(r["line_number"]),
            line_range=[int(x) for x in r.get("line_range") or [r["line_number"]]],
            severity=sev,
            confidence=r["issue_confidence"].upper(),
            issue_text=r.get("issue_text", ""),
            cwe=(r.get("issue_cwe") or {}).get("id"),
            code=r.get("code", ""),
        ))
    alerts.sort(key=lambda a: (a.path, a.line, a.test_id))

    seen: dict[str, int] = {}
    for a in alerts:
        base = f"{target_slug}-{_slug(a.path)}-{a.line}-{a.test_id}"
        seen[base] = seen.get(base, 0) + 1
        a.alert_id = base if seen[base] == 1 else f"{base}-{seen[base]}"

    info = {"bandit_version": version, "argv": cmd[1:], "alerts": len(alerts),
            "bandit_errors": data.get("errors", [])[:20]}
    return alerts, info
