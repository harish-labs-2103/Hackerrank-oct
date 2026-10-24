# Data Investigator — Ghost Data Still Exists

An evidence-driven CLI + AI agent that audits whether a person's data was actually deleted.
For every request in `dataset/deletion_requests.csv` it decides `COMPLETE`, `INCOMPLETE` or
`NOT_APPLICABLE`, and writes ordered, evidence-cited `steps_json` to `output.csv` at the repo root.

**Core principle:** deterministic Python owns identity resolution, evidence state, policy,
status, step ordering and serialization. The local LLM (Gemma 3 4B via Ollama) is limited to
two guarded tasks — classifying request intent and transcribing console screenshots — and its
output reaches the engine only through a validation guard. A run without the model produces a
valid output too.

## Layout

`code.zip` contains the contents of this `code/` directory. Unzip it into `<repo>/code/` next to
`dataset/` so the commands below work unchanged.

## Setup

Requires Python ≥ 3.10.

```bash
pip install -r code/requirements.txt
# optional, for AI assistance (local, no API key):
ollama pull gemma3:4b          # Ollama must be serving on localhost:11434 (or set OLLAMA_HOST)
```

No API keys or secrets are needed. `OLLAMA_HOST` is the only environment variable read.

## Run (from the repository root)

```bash
python code/main.py                       # = audit --all : all 518 requests -> ./output.csv (validated)
python code/main.py audit --all --no-llm  # deterministic only
python code/main.py validate-output       # check output.csv against the contract
python code/evaluation/main.py            # score the 32 public samples + custom cases
python -m pytest -q code/tests            # unit + hallucination-guard tests
```

`audit --all` writes to `output.csv.tmp`, validates it, and only then replaces `output.csv`, so a
failed run never leaves a partial or invalid file. It also writes `reports/llm_audit.jsonl` (every
model response and the guard's verdict) and `reports/usage_full.md` (token counts for the run).

LLM responses are cached in `code/cache/llm/` keyed by prompt version, model digest, options and
input, so a re-run is deterministic and makes no new model calls.

## CLI

```text
data-investigator [--verbose] [--quiet] [--config PATH] [--no-color] [--format text|json] <command>

init                      create data-investigator.yaml, check dataset, prepare reports/ (optional)
sources list              which sources are available (read-only)
scan                      parse every evidence file once; parse/AI-extraction status
audit --case ID | --all   investigate one case / all cases (--sample, --no-llm, --output, --save-reports)
identity --case ID        identifiers linked to the request and why (masked)
timeline --case ID        evidence chronology up to audit_at (--from/--to/--system)
findings --case ID        confirmed findings, separate from unresolved questions
evidence --case ID        every evidence item used; items after audit_at listed as excluded
plan --case ID            the ordered plan (identical to the case's steps_json); nothing executed
verify --case ID [--at T] per-obligation VERIFIED / FAILED / UNVERIFIABLE
report --case ID --export json|markdown|csv
apply --case ID --dry-run preview only; execution is refused (exit 4)
validate-output           contract checks with per-case errors (exit 3 on failure)
```

`python code/main.py <command>` is identical to `data-investigator <command>`
(`pip install -e code` installs the console script). Exit codes: 0 ok, 1 usage, 2 input/config,
3 output validation failed, 4 not permitted, 5 some cases failed processing.

## How it works

```
request ──► intent (heuristic; LLM second opinion via guard) ──► NOT_APPLICABLE?
        └─► identity graph: request email → user_id → all current + historical emails,
            phones, email hashes, user_id (names never link on their own)
        └─► evidence index (built once): SQL snapshots (sqlite in-memory, every replica table),
            JSON bundles/exports, 6 log dialects (trace-id grouping), handover text, PDFs (text
            layer), screenshots (Gemma vision, guarded) ──► normalized Observations
        └─► link resources whose evidence matches the subject inside
            [requested_at − 1 day, audit_at]; catalog links contradicted by the record are dropped
        └─► state at audit_at = latest observation from a source captured ≤ audit_at
            (activity results are claims; explicit checks/"repopulated" events count);
            a full-scope inventory snapshot that captured the subject but lacks a record proves ABSENT
        └─► policy (deletion_rules.csv) per category → obligation satisfied / violated
        └─► planner: FINDING → REMEDIATION → VERIFICATION, contract operation order,
            then system_id, resource_id; evidence ids only on findings / COMPLETE verifications
        └─► validator (columns, 518 rows, order, JSON, op/state pairs, numbering, evidence ≤ audit_at)
```

Package layout: `data_investigator/` — `ingestion`, `normalize`, `identity`, `evidence/`
(`parsers`, `interpret`, `artifacts`, `registry`), `activity`, `policy`, `engine`, `planner`,
`validation`, `reporting`, `agent/` (`client`, `guard`, `tasks`), `cli`, `ui/`. Prompts are in
`code/prompts/` (versioned). Design documents are in `plans/`.

## Evaluation

`code/evaluation/evaluation_report.md` — metrics on the public samples, 7 team-authored custom
cases (`code/evaluation/custom_cases/`, rebuilt by `build_custom_cases.py`), failure analysis.
`code/evaluation/usage_report.md` — model calls and tokens for the final full run.

## Security

Read-only: SQL snapshots are loaded into in-memory SQLite, evidence paths are confined to
`dataset/`, nothing is written outside `output.csv`, `reports/` and the cache. Evidence text is
untrusted: it is passed to the model inside delimiters, the model has no tools, outputs are
schema/enum-validated and must quote the source verbatim. Emails and phones are masked in
terminal output and reports.
