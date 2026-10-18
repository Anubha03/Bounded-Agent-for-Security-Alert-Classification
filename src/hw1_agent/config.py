"""Load the TOML config and check it against config.schema.json."""
from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path

from jsonschema import Draft202012Validator

SCHEMA_DIR = Path(__file__).resolve().parents[2]

class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    raw: dict
    paths: tuple[str, ...]
    exclude: tuple[str, ...]
    min_severity: str
    bandit_version: str
    provider: str
    model: str
    reasoning_effort: str | None
    max_tokens: int
    output_allowance: int
    max_steps: int
    max_turns_per_alert: int
    no_progress_limit: int
    request_timeout_s: float


def load_schema(name: str) -> dict:
    for base in (SCHEMA_DIR, Path.cwd()):
        if (base / name).is_file():
            return json.loads((base / name).read_text(encoding="utf-8"))
    raise ConfigError(f"{name} not found next to the package or in the working directory")


def load_config(path: str | Path) -> Config:
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"config not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"config is not valid TOML: {exc}") from exc

    errors = sorted(Draft202012Validator(load_schema("config.schema.json")).iter_errors(raw),
                    key=lambda e: list(e.path))
    if errors:
        msgs = "; ".join(f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message[:200]}" for e in errors)
        raise ConfigError(f"config does not validate against config.schema.json: {msgs}")

    scan, model, budget = raw["scan"], raw["model"], raw["budget"]
    agent = raw.get("agent", {})
    if not isinstance(agent, dict):
        raise ConfigError("[agent] must be a table")

    def _int(key: str, default: int, minimum: int) -> int:
        v = agent.get(key, default)
        if not isinstance(v, int) or isinstance(v, bool) or v < minimum:
            raise ConfigError(f"agent.{key} must be an integer >= {minimum}")
        return v

    effort = model.get("reasoning_effort")
    if effort is not None and not isinstance(effort, str):
        raise ConfigError("model.reasoning_effort must be a string")

    return Config(
        raw=raw,
        paths=tuple(scan["paths"]),
        exclude=tuple(scan["exclude"]),
        min_severity=scan["min_severity"],
        bandit_version=scan["bandit_version"],
        provider=model["provider"],
        model=model["name"],
        reasoning_effort=effort,
        max_tokens=budget["max_tokens"],
        output_allowance=budget["output_allowance"],
        max_steps=budget["max_steps"],
        max_turns_per_alert=_int("max_turns_per_alert", 8, 1),
        no_progress_limit=_int("no_progress_limit", 4, 1),
        request_timeout_s=float(agent.get("request_timeout_s", 120)),
    )
