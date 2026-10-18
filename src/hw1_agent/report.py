"""Builds report.json"""
from __future__ import annotations

import json
import os
from pathlib import Path

from jsonschema import Draft202012Validator

from .config import load_schema


def build_report(input_arg: str, commit: str, cfg, alerts, used_tokens: int, exhausted: bool,
                 run_id: str, terminal_status: str) -> dict:
    rows = sorted((a.row() for a in alerts), key=lambda r: (r["path"], r["line"], r["test_id"]))
    return {
        "target": {"input": input_arg, "commit": commit},
        "scan": {"bandit_version": cfg.bandit_version, "min_severity": cfg.min_severity,
                 "alert_count": len(rows)},
        "budget": {"max_tokens": cfg.max_tokens, "used_tokens": used_tokens, "exhausted": exhausted},
        "run_id": run_id,
        "terminal_status": terminal_status,
        "model": {"provider": cfg.provider, "name": cfg.model},
        "alerts": rows,
    }


def write_report(path: Path, *args, **kwargs) -> list[str]:
    """Write report.json, then raise if it doesn't match report.schema.json."""
    report = build_report(*args, **kwargs)
    errors = [f"{'/'.join(map(str, e.path))}: {e.message}"
              for e in Draft202012Validator(load_schema("report.schema.json")).iter_errors(report)]
    tmp = Path(path).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    if errors:
        raise ValueError("report.json does not validate: " + "; ".join(errors[:5]))
    return errors
