"""Checks and runs the tool calls the model asks for. A bad request never raises. It comes back to the model as a REFUSED observation instead."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from jsonschema import Draft202012Validator

from .tools import TOOLS, ToolError
from .trace import Trace
from .workspace import PathRefused, Workspace

RESULT_MAX_CHARS = 12_000


@dataclass
class Observation:
    tool: str
    ok: bool
    text: str
    novel: bool = False  
    observed: dict[str, set[int]] = field(default_factory=dict)


class Dispatcher:
    def __init__(self, ws: Workspace, trace: Trace):
        self.ws = ws
        self.trace = trace
        self._validators = {n: Draft202012Validator(spec["parameters"]) for n, (_, spec) in TOOLS.items()}

    def dispatch(self, alert_id: str, name: str, raw_args, seen: set[str]) -> Observation:
        args, parse_error = _parse_args(raw_args)
        self.trace.event("tool_request", alert_id, tool=str(name or "<missing>"),
                         args=args if isinstance(args, dict) else {"_raw": str(raw_args)[:500]})

        if parse_error:
            return self._refuse(alert_id, name, f"arguments are not a JSON object: {parse_error}")
        if name not in TOOLS:
            return self._refuse(alert_id, name or "<missing>",
                                f"unknown tool {name!r}; available: {', '.join(sorted(TOOLS))}, submit_verdict")
        errors = list(self._validators[name].iter_errors(args))
        if errors:
            return self._refuse(alert_id, name, "bad arguments: " + "; ".join(e.message[:150] for e in errors[:3]))

        fingerprint = name + json.dumps(args, sort_keys=True)
        if fingerprint in seen:
            return self._refuse(alert_id, name, "duplicate request: you already received this exact "
                                                "observation earlier for this alert")
        seen.add(fingerprint)

        fn, _ = TOOLS[name]
        try:
            text, observed = fn(self.ws, **args)
        except PathRefused as exc:
            return self._refuse(alert_id, name, f"refused: {exc}")
        except ToolError as exc:
            return self._refuse(alert_id, name, str(exc))
        except (TypeError, ValueError, OSError) as exc:
            return self._refuse(alert_id, name, f"tool failed: {type(exc).__name__}: {exc}")

        if len(text) > RESULT_MAX_CHARS:
            text = text[:RESULT_MAX_CHARS].rsplit("\n", 1)[0] + "\n[output truncated]"
            observed = _shown_lines(name, text, observed)  # drop lines that got cut off
        self.trace.event("tool_result", alert_id, tool=name, ok=True, chars=len(text),
                         observed_files=sorted(observed))
        return Observation(tool=name, ok=True, text=text, novel=True, observed=observed)

    def _refuse(self, alert_id: str, name: str, reason: str) -> Observation:
        self.trace.event("tool_result", alert_id, tool=str(name or "<missing>"), ok=False, error=reason)
        return Observation(tool=str(name), ok=False, text=f"REFUSED: {reason}")


def _shown_lines(tool: str, text: str, observed: dict[str, set[int]]) -> dict[str, set[int]]:
    if tool == "read_file":
        shown = {int(m.group(1)) for m in re.finditer(r"^\s*(\d+)\| ", text, re.M)}
        return {p: lines & shown for p, lines in observed.items()}
    if tool == "search_code":
        out: dict[str, set[int]] = {}
        for m in re.finditer(r"^(.+?):(\d+): ", text, re.M):
            out.setdefault(m.group(1), set()).add(int(m.group(2)))
        return out
    return {}


def _parse_args(raw):
    if isinstance(raw, dict):
        return raw, None
    if raw is None or raw == "":
        return {}, None
    try:
        val = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        return {"_raw": str(raw)[:500]}, str(exc)
    if not isinstance(val, dict):
        return {"_raw": str(raw)[:500]}, "expected an object"
    return val, None
