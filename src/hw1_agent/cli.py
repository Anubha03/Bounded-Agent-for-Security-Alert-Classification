"""Command-line entry point: hw1-agent --input REPO --output DIR --config FILE."""
from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

from .config import ConfigError, load_config
from .trace import Trace

EXIT = {"completed": 0, "budget_exhausted": 0, "max_steps": 0, "no_progress": 0,
        "fatal_error": 1, "config_invalid": 2}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="hw1-agent", description="Bounded agent that classifies Bandit alerts.")
    ap.add_argument("--input", required=True, help="root of the pinned target checkout")
    ap.add_argument("--output", required=True, help="directory for report.json and trace.jsonl")
    ap.add_argument("--config", required=True, help="TOML config validating against config.schema.json")
    args = ap.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        # Bail out early, but still leave a trace that says why.
        trace = Trace(Path(args.output) / "trace.jsonl", f"config-invalid-{uuid.uuid4().hex[:6]}")
        trace.event("state_change", None, node="config", error=str(exc)[:1000])
        trace.terminal("config_invalid")
        trace.close()
        print(f"hw1-agent: {exc}", file=sys.stderr)
        return EXIT["config_invalid"]

    from .graph import Agent  # imported late so a bad config fails fast

    agent = Agent(cfg, args.input, args.output)
    status = agent.run()
    rows = agent.alerts
    n1 = sum(1 for a in rows if a.label == 1)
    n0 = sum(1 for a in rows if a.label == 0)
    nn = sum(1 for a in rows if a.label is None)
    print(f"{status}: {len(rows)} alerts (label1={n1}, label0={n0}, null={nn}); "
          f"tokens used {agent.usage['input'] + agent.usage['output']}/{cfg.max_tokens}; "
          f"steps {agent.steps_used}/{cfg.max_steps}; outputs in {args.output}")
    return EXIT.get(status, 1)


if __name__ == "__main__":
    raise SystemExit(main())
