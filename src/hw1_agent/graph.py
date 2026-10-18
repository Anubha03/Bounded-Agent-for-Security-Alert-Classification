"""The agent loop, written as a small graph.
Every branch point logs a routing event, with 'by' saying whether the model's reply
or a runtime check picked the branch. The model only proposes budgets, counters and
report rows are all owned here.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from jsonschema import Draft202012Validator

from . import prompts
from .config import Config, load_schema
from .dispatcher import Dispatcher
from .provider import FatalProviderError, ProviderError, make_provider
from .report import write_report
from .scanner import Alert, ScanError, run_bandit
from .tools import SUBMIT_TOOL, ToolError, function_specs, read_file
from .trace import Trace
from .workspace import PathRefused, Workspace

CONTEXT_RADIUS = 25          
MAX_CONSECUTIVE_PROVIDER_FAILURES = 3
EVIDENCE_MAX_SPAN = 200
LINE_REF = re.compile(r"(?i)\blines?\s*\d+|\bL\d+\b|:\d+")


class StopRun(Exception):
    def __init__(self, status: str, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


@dataclass
class AlertSession:
    """State for the alert being worked on; thrown away once it settles."""
    alert: Alert
    messages: list[dict] = field(default_factory=list)          # the conversation the model sees
    observed: dict[str, set[int]] = field(default_factory=dict)  # lines shown to the model, per file
    seen_requests: set[str] = field(default_factory=set)        # tool calls already run
    turns: int = 0
    last_input: int | None = None    # reported input tokens of the previous call
    last_bytes: int | None = None    # request size in bytes of the previous call
    last_msgs: int = 0
    pending_reply: object = None     # the reply waiting to be routed


class Agent:
    def __init__(self, cfg: Config, input_dir: str, out_dir: str, provider=None):
        self.cfg = cfg
        self.input_arg = input_dir
        self.input_dir = Path(input_dir).resolve()
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.target_slug = re.sub(r"[^A-Za-z0-9._-]+", "-", self.input_dir.name) or "target"
        self.run_id = f"{self.target_slug}-{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
        self.trace = Trace(self.out_dir / "trace.jsonl", self.run_id)
        self._provider = provider
        self.tools = function_specs()
        self._submit_validator = Draft202012Validator(SUBMIT_TOOL["parameters"])

        # Run-wide state.
        self.alerts: list[Alert] = []          
        self.queue: list[Alert] = []           
        self.usage = {"input": 0, "output": 0}  # tokens charged so far
        self.steps_used = 0                    # model calls + tool runs
        self.no_progress_streak = 0
        self.provider_failures = 0
        self.first_call_input: int | None = None
        self.scan_info: dict = {}
        self.commit = "0" * 40
        self.session: AlertSession | None = None
        self.stop: StopRun | None = None
        self.terminal_status: str | None = None
        self.ws: Workspace | None = None

    def run(self) -> str:
        node = "prepare"
        try:
            with Workspace(self.input_dir) as ws:
                self.ws = ws
                self.dispatcher = Dispatcher(ws, self.trace)
                while node != "end":
                    node = getattr(self, f"n_{node}")()
        except BaseException as exc:  
            self._crash(exc)
            if not isinstance(exc, Exception):
                raise
        finally:
            self.trace.close()
        return self.terminal_status

    def _route(self, decision: str, by: str, **extra) -> None:
        aid = self.session.alert.alert_id if self.session else None
        self.trace.event("routing", aid, decision=decision, by=by, **extra)

    def n_prepare(self) -> str:
        self.commit = _git_commit(self.input_dir)
        self.trace.event("state_change", None, node="prepare", input=self.input_arg, commit=self.commit,
                         workspace="disposable copy", withheld_run_level=self.ws.withheld(),
                         config={k: self.cfg.raw[k] for k in ("scan", "model", "budget")})
        if self._provider is None:
            try:
                self._provider = make_provider(self.cfg.provider, self.cfg.model, self.cfg.output_allowance,
                                               self.cfg.reasoning_effort, self.cfg.request_timeout_s)
            except FatalProviderError as exc:
                self.stop = StopRun("fatal_error", str(exc))
        return "discover"

    def n_discover(self) -> str:
        args = {"paths": list(self.cfg.paths), "exclude": list(self.cfg.exclude),
                "min_severity": self.cfg.min_severity}
        self.trace.event("tool_request", None, tool="bandit", args=args)
        try:
            alerts, info = run_bandit(self.input_dir, self.cfg.paths, self.cfg.exclude, self.cfg.min_severity,
                                      self.cfg.bandit_version, self.target_slug)
        except (ScanError, OSError, subprocess.SubprocessError) as exc:
            self.trace.event("tool_result", None, tool="bandit", ok=False, error=str(exc)[:1000])
            self.stop = StopRun("fatal_error", f"discovery failed: {exc}")
            self.trace.event("routing", None, decision="stop", by="runtime", reason="discovery failed")
            return "stop"
        self.trace.event("tool_result", None, tool="bandit", ok=True, alerts=len(alerts),
                         bandit_version=info["bandit_version"], argv=info["argv"],
                         bandit_errors=info["bandit_errors"])
        self.alerts, self.queue, self.scan_info = alerts, list(alerts), info
        if self.stop:
            self.trace.event("routing", None, decision="stop", by="runtime", reason=self.stop.reason[:300])
            return "stop"
        return "select"

    def n_select(self) -> str:
        self.session = None
        if not self.queue:
            self.trace.event("routing", None, decision="finalize", by="runtime", reason="all alerts settled")
            self.terminal_status = "completed"
            return "finalize"
        alert = self.queue[0]
        self.session = AlertSession(alert)
        self._route("triage", "runtime", remaining=len(self.queue))
        return "triage"

    def n_triage(self) -> str:
        s = self.session
        self.ws.withhold_for_case(s.alert.path)
        start = max(1, s.alert.line - CONTEXT_RADIUS)
        end = max(s.alert.line_range[-1], s.alert.line) + CONTEXT_RADIUS
        try:
            text, observed = read_file(self.ws, s.alert.path, start, end)
        except (ToolError, PathRefused) as exc:
            text, observed = f"(runtime could not read the flagged file: {exc})", {}
        _merge(s.observed, observed)
        s.messages = [{"role": "system", "content": prompts.SYSTEM},
                      {"role": "user", "content": prompts.alert_message(s.alert, text)}]
        self.trace.event("state_change", s.alert.alert_id, node="triage",
                         context={"path": s.alert.path, "start_line": start,
                                  "end_line": max(observed.get(s.alert.path, {start}))},
                         withheld_for_case=len(self.ws.withheld()))
        return "gate"

    def n_gate(self) -> str:
        """Checks before every model call: turn limit, then step limit, then token budget."""
        s = self.session
        if s.turns >= self.cfg.max_turns_per_alert:
            self._route("turn_limit", "runtime", turns=s.turns)
            return self._settle_error("no verdict within the per-alert turn limit")
        if self.steps_used + 1 > self.cfg.max_steps:
            self._route("max_steps", "runtime", steps_used=self.steps_used, max_steps=self.cfg.max_steps)
            self.stop = StopRun("max_steps", f"max_steps={self.cfg.max_steps} reached")
            return "stop"
        messages, _ = self._next_request()
        projected = self._projected_input(messages)
        required = projected + self.cfg.output_allowance
        remaining = self.cfg.max_tokens - self._charged()
        if required > remaining:
            self._route("budget_exhausted", "runtime", remaining=remaining, required=required,
                        projected_input=projected, output_allowance=self.cfg.output_allowance)
            self.stop = StopRun("budget_exhausted", "token budget cannot cover the next model call")
            return "stop"
        return "classify"

    def n_classify(self) -> str:
        s = self.session
        messages, tool_choice = self._next_request()
        final_turn = len(messages) > len(s.messages)
        req_bytes, req_msgs = _request_bytes(messages, self.tools), len(messages)
        self.steps_used += 1
        s.turns += 1
        try:
            reply = self._provider.complete(messages, self.tools, tool_choice)
        except FatalProviderError as exc:
            self.trace.event("state_change", s.alert.alert_id, node="classify", error=str(exc)[:500],
                             usage_reported=False)
            self._route("fatal_error", "runtime")
            self.stop = StopRun("fatal_error", str(exc))
            return "stop"
        except ProviderError as exc:
            self.trace.event("state_change", s.alert.alert_id, node="classify", error=str(exc)[:500],
                             usage_reported=False)
            self.provider_failures += 1
            if self.provider_failures >= MAX_CONSECUTIVE_PROVIDER_FAILURES:
                self._route("fatal_error", "runtime", reason="repeated provider failures")
                self.stop = StopRun("fatal_error", f"{self.provider_failures} consecutive provider failures")
                return "stop"
            self._route("error", "runtime", reason="provider call failed")
            return self._settle_error(f"model call failed: {exc}"[:300])

        self.provider_failures = 0
        self.usage["input"] += reply.usage["input"]
        self.usage["output"] += reply.usage["output"]
        self.trace.event("model_call", s.alert.alert_id, role="classifier", model=self.cfg.model,
                         usage=reply.usage, turn=s.turns, finish_reason=reply.finish_reason,
                         tool_calls=[c.name for c in reply.tool_calls])
        s.last_input, s.last_bytes, s.last_msgs = reply.usage["input"], req_bytes, req_msgs
        if s.turns == 1 and self.first_call_input is None:
            self.first_call_input = reply.usage["input"]
        if final_turn:  # keep the final-turn reminder in the history
            s.messages.append(messages[-1])
        s.messages.append(reply.assistant_message)
        s.pending_reply = reply

        names = [c.name for c in reply.tool_calls]
        if not names:
            self._route("no_tool_call", "runtime", finish_reason=reply.finish_reason)
            s.messages.append({"role": "user", "content": prompts.NUDGE_LENGTH
                               if reply.finish_reason == "length" else prompts.NUDGE})
            return self._after_turn(progress=False)
        if names == ["submit_verdict"]:
            self._route("submit", "model")
            return "validate"
        self._route("tools", "model", tools=names)
        return "dispatch"

    def n_dispatch(self) -> str:
        s = self.session
        progress = False
        for call in s.pending_reply.tool_calls:
            if call.name == "submit_verdict":
                text = "REFUSED: call submit_verdict alone, after you have read the tool results."
                self.trace.event("state_change", s.alert.alert_id, node="validate", accepted=False,
                                 reason="submit_verdict mixed with other tool calls")
            else:
                if self.steps_used + 1 > self.cfg.max_steps:
                    self._route("max_steps", "runtime", steps_used=self.steps_used,
                                max_steps=self.cfg.max_steps)
                    self.stop = StopRun("max_steps", f"max_steps={self.cfg.max_steps} reached")
                    return "stop"
                self.steps_used += 1
                obs = self.dispatcher.dispatch(s.alert.alert_id, call.name, call.arguments, s.seen_requests)
                _merge(s.observed, obs.observed)
                progress |= obs.novel
                text = obs.text
            s.messages.append({"role": "tool", "tool_call_id": call.id, "content": text})
        return self._after_turn(progress)

    def n_validate(self) -> str:
        s = self.session
        call = s.pending_reply.tool_calls[0]
        verdict, problems = self._check_verdict(call.arguments)
        if problems:
            self.trace.event("state_change", s.alert.alert_id, node="validate", accepted=False,
                             reason="; ".join(problems)[:500])
            self._route("reject_verdict", "runtime")
            s.messages.append({"role": "tool", "tool_call_id": call.id,
                               "content": "REFUSED: verdict rejected: " + "; ".join(problems)
                               + ". Fix it and call submit_verdict again."})
            return self._after_turn(progress=False)
        self.trace.event("state_change", s.alert.alert_id, node="validate", accepted=True)
        self._route("settle", "runtime")
        return self._settle(s.alert, "classified", verdict["label"], verdict["rationale"], verdict["evidence"])

    def n_stop(self) -> str:
        """Close out every open alert with a status that matches why the run stopped."""
        st = self.stop
        row_status = "budget_exhausted" if st.status == "budget_exhausted" else "error"
        rationale = "" if row_status == "budget_exhausted" else f"run stopped ({st.status}): {st.reason}"[:500]
        for alert in list(self.queue):
            self._settle(alert, row_status, None, rationale, [], node="stop")
        self.session = None
        self.terminal_status = st.status
        return "finalize"

    def n_finalize(self) -> str:
        self._write_report()
        self.trace.terminal(self.terminal_status, steps_used=self.steps_used,
                            alerts=len(self.alerts),
                            **({"reason": self.stop.reason} if self.stop else {}))
        return "end"

    def _after_turn(self, progress: bool) -> str:
        """Stop the run if too many turns in a row turned up nothing new."""
        self.no_progress_streak = 0 if progress else self.no_progress_streak + 1
        if self.no_progress_streak >= self.cfg.no_progress_limit:
            self._route("no_progress", "runtime", streak=self.no_progress_streak)
            self.stop = StopRun("no_progress", f"{self.no_progress_streak} consecutive turns without progress")
            return "stop"
        return "gate"

    def _settle_error(self, reason: str) -> str:
        return self._settle(self.session.alert, "error", None, reason, [])

    def _settle(self, alert: Alert, status: str, label, rationale: str, evidence: list, node="settle") -> str:
        if alert.status is not None:
            raise RuntimeError(f"alert {alert.alert_id} settled twice")
        alert.status, alert.label, alert.rationale, alert.evidence = status, label, rationale, evidence
        self.queue.remove(alert)
        self.trace.event("state_change", alert.alert_id, node=node, alert_status=status, label=label)
        if self.ws is not None:
            self.ws.restore_case()
        if node == "settle":
            self.no_progress_streak = 0
        return "select"

    def _charged(self) -> int:
        return self.usage["input"] + self.usage["output"]

    def _next_request(self):
        """Messages and tool_choice for the next model call."""
        s = self.session
        if s.turns + 1 >= self.cfg.max_turns_per_alert:
            return (s.messages + [{"role": "user", "content": prompts.FINAL_TURN}],
                    {"type": "function", "function": {"name": "submit_verdict"}})
        return s.messages, "required"  # the model always has to call a tool

    def _projected_input(self, messages: list[dict]) -> int:
        s = self.session
        now_bytes = _request_bytes(messages, self.tools)
        if s.last_input is not None:
            new_msgs = len(messages) - s.last_msgs
            return s.last_input + max(0, now_bytes - s.last_bytes) + 16 * new_msgs
        if self.first_call_input is not None:  # new alert, but same system prompt and tools
            return self.first_call_input + sum(len(m["content"].encode("utf-8")) + 16
                                               for m in messages[1:]) + 64
        return now_bytes + 256

    def _check_verdict(self, raw: str):
        try:
            v = json.loads(raw or "")
        except json.JSONDecodeError as exc:
            return None, [f"arguments are not valid JSON ({exc})"]
        if not isinstance(v, dict):
            return None, ["arguments must be an object"]
        errs = [e.message[:150] for e in self._submit_validator.iter_errors(v)]
        if errs:
            return None, errs
        problems = []
        if isinstance(v["label"], bool) or v["label"] not in (0, 1):
            problems.append("label must be 0 or 1")
        rationale = v["rationale"].strip()
        if not rationale:
            problems.append("rationale is empty")
        elif not LINE_REF.search(rationale):
            problems.append("rationale must cite at least one line (e.g. 'line 42')")
        evidence = []
        for ev in v["evidence"]:
            path = ev["path"].strip()
            while path.startswith("./"):
                path = path[2:]
            try:
                p = self.ws.resolve(path)
            except PathRefused as exc:
                problems.append(f"evidence path {ev['path']!r}: {exc}")
                continue
            if not p.is_file():
                problems.append(f"evidence path {ev['path']!r} is not a file")
                continue
            rel = PurePosixPath(p.relative_to(self.ws.root).as_posix()).as_posix()
            a, b = ev["start_line"], ev["end_line"]
            if b < a or b - a > EVIDENCE_MAX_SPAN:
                problems.append(f"evidence {rel}:{a}-{b} is not a valid range (<= {EVIDENCE_MAX_SPAN} lines)")
                continue
            seen = self.session.observed.get(rel, set())
            unseen = [n for n in range(a, b + 1) if n not in seen]
            if unseen:
                problems.append(f"evidence {rel}:{a}-{b} includes lines you were never shown "
                                f"(e.g. {unseen[0]}); cite only observed lines")
                continue
            evidence.append({"path": rel, "start_line": a, "end_line": b})
        if problems:
            return None, problems
        return {"label": v["label"], "rationale": rationale[:3000], "evidence": evidence}, []

    def _write_report(self) -> None:
        write_report(self.out_dir / "report.json", self.input_arg, self.commit, self.cfg, self.alerts,
                     used_tokens=self.trace.usage["input"] + self.trace.usage["output"],
                     exhausted=self.terminal_status == "budget_exhausted",
                     run_id=self.run_id, terminal_status=self.terminal_status)

    def _crash(self, exc: BaseException) -> None:
        """Last resort: mark open alerts as errors, write the report and end the trace."""
        if self.trace.ended:
            return
        try:
            self.trace.event("state_change", self.session.alert.alert_id if self.session else None,
                             node="runtime", exception=type(exc).__name__, message=str(exc)[:500],
                             traceback=traceback.format_exc()[-2000:])
            reason = f"run crashed: {type(exc).__name__}: {exc}"[:500]
            self.stop = StopRun("fatal_error", reason)
            for alert in list(self.queue):
                if alert.status is None:
                    alert.status, alert.label, alert.rationale, alert.evidence = "error", None, reason, []
                    self.trace.event("state_change", alert.alert_id, node="crash", alert_status="error", label=None)
            self.queue.clear()
            self.terminal_status = "fatal_error"
            try:
                self._write_report()
            except Exception as rexc:  # still end the trace
                self.trace.event("state_change", None, node="report", error=f"report not written: {rexc}"[:500])
        finally:
            if not self.trace.ended:
                self.trace.terminal("fatal_error", reason=str(exc)[:500])


def _merge(into: dict[str, set[int]], new: dict[str, set[int]]) -> None:
    for k, v in new.items():
        into.setdefault(k, set()).update(v)


def _request_bytes(messages: list[dict], tools: list[dict]) -> int:
    return len(json.dumps([messages, tools], ensure_ascii=False).encode("utf-8"))


def _git_commit(root: Path) -> str:
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True,
                             text=True, timeout=30)
        sha = out.stdout.strip()
        if re.fullmatch(r"[0-9a-f]{40}", sha):
            return sha
    except (OSError, subprocess.SubprocessError):
        pass
    head = root / ".git" / "HEAD"
    try:
        ref = head.read_text().strip()
        if ref.startswith("ref: "):
            ref = (root / ".git" / ref[5:]).read_text().strip()
        if re.fullmatch(r"[0-9a-f]{40}", ref):
            return ref
    except OSError:
        pass
    raise ScanError(f"cannot determine the commit of {root} (is it a git checkout?)")
