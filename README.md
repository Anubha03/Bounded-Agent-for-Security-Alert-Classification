# Bounded Agent for Security Alert Classification

COMS E6998-019 HW1. The agent scans a pinned Python repository with Bandit 1.9.4 at MEDIUM+ severity,
then investigates each alert with read-only tools and labels it `1` (potential vulnerability) or `0`
(not a vulnerability), with a line-cited rationale. It writes `report.json` and `trace.jsonl`.
The design is in [REPORT.md](REPORT.md). The original assignment text is in `briefs/`.

- **Agent command:** `hw1-agent`, or `python -m hw1_agent`
- **Provider / model:** OpenAI Chat Completions, `gpt-5.4-mini` (set in `[model]` of the config)
- **Environment variable:** `OPENAI_API_KEY` (required). `OPENAI_BASE_URL` is optional and only
  needed for a non-default endpoint. Never put keys in a config file.

## 1. Setup from a fresh copy

You need Python **3.12**, because Bandit 1.9.4 needs it, and `git`.

```bash
unzip hw1-submission.zip                 # or: git clone <this-repo-url> hw1-agent-architectures
cd hw1-agent-architectures

python3.12 -m venv .venv                 # or: ~/.pyenv/versions/3.12.x/bin/python -m venv .venv
source .venv/bin/activate
python --version                         # must print Python 3.12.x

python -m pip install --upgrade pip
python -m pip install -r requirements.lock   # pinned dependencies (bandit==1.9.4, openai, jsonschema)
python -m pip install -e . --no-deps         # installs the hw1-agent command

bandit --version | head -1               # must print: bandit 1.9.4
export OPENAI_API_KEY="..."              # required; never put it in a config file
```

## 2. Clone the targets at their pinned commits

```bash
mkdir -p targets
git clone https://github.com/OWASP-Benchmark/BenchmarkPython.git targets/owasp-benchmark-python
git -C targets/owasp-benchmark-python checkout f1291485808b66e20ddb6b01b10dc71b3df8c8ba
git clone https://github.com/Kozea/Radicale.git targets/radicale
git -C targets/radicale checkout eff8027f3dc4910be1659ee71f4b4a454ade5a8c
git clone https://github.com/sgl-project/sglang.git targets/sglang
git -C targets/sglang checkout bbe9c7eeb520b0a67e92d133dfc137a3688dc7f2
```

`targets/` is git-ignored. The agent never writes to a target. It works in a disposable copy.

## 3. Configs

| File | Purpose |
|---|---|
| `configs/full.toml` | full budget (`max_tokens = 1500000`) with the Radicale `[scan]` block |
| `configs/low.toml` | low budget (`max_tokens = 50000`) with the Radicale `[scan]` block; runs out partway through Radicale |
| `configs/targets/<target>-<full\|low>.toml` | the same two configs with `[scan]` swapped for each target's required block |

Only `[scan]` differs between a target's configs and `configs/full.toml` / `configs/low.toml`.
`[model]`, `[budget]` and the optional `[agent]` table are identical. Every config validates against
`config.schema.json`. The agent refuses to start otherwise: it exits with status 2 and the trace
ends `config_invalid`.

## 4. Run

```bash
hw1-agent --input <target checkout root> --output <out dir> --config <config.toml>
```

These are the six runs. Each writes `report.json` and `trace.jsonl` into its output directory:

```bash
hw1-agent --input targets/radicale               --output runs/radicale-full --config configs/full.toml
hw1-agent --input targets/radicale               --output runs/radicale-low  --config configs/low.toml
hw1-agent --input targets/sglang                 --output runs/sglang-full   --config configs/targets/sglang-full.toml
hw1-agent --input targets/sglang                 --output runs/sglang-low    --config configs/targets/sglang-low.toml
hw1-agent --input targets/owasp-benchmark-python --output runs/owasp-full    --config configs/targets/owasp-benchmark-python-full.toml
hw1-agent --input targets/owasp-benchmark-python --output runs/owasp-low     --config configs/targets/owasp-benchmark-python-low.toml
```

When a run ends, the agent prints its terminal status, the label counts, tokens used/max and steps used/max.
Exit status is 0 for `completed`, `budget_exhausted`, `max_steps` and `no_progress`, 1 for `fatal_error`
and 2 for `config_invalid`. `report.json` and the terminal trace event are written in every case except
`config_invalid`, which writes only the trace.

**Low-budget demonstration:** `runs/radicale-low/report.json` has `"exhausted": true`, some rows
`classified` and the rest `budget_exhausted` with `label: null`. The trace shows the runtime's `routing`
event `budget_exhausted` (with `remaining` and `required`) followed by one settlement `state_change` per
unfinished alert.

## Layout

```text
src/hw1_agent/
  cli.py         argument parsing, config rejection, exit codes
  config.py      TOML load + validation against config.schema.json
  scanner.py     glob expansion, Bandit invocation (argument list), JSON parsing, normalisation
  workspace.py   disposable copy, withholding, the single path check (resolve)
  tools.py       read_file, search_code, list_dir (+ submit_verdict schema)
  dispatcher.py  validates and runs every tool request; only writer of observations
  provider.py    OpenAI Chat Completions adapter; reported usage only
  graph.py       nodes, edges, state, budget gate, failsafes, settlement, crash handling
  trace.py       append-only JSONL trace
  report.py      report.json build + schema validation
runs/            committed outputs of the six runs (report.json, trace.jsonl)
```
