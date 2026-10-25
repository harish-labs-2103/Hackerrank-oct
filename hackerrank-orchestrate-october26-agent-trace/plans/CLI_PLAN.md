# Davestigator — CLI & Terminal Flow Implementation Plan

**Source documents:** *CLI Interface Design Specification v1.0* and *User Terminal Flow Document v1.0*. Both are proposed designs.
**Companions:**
- `PLAN.md` covers the challenge pipeline.
- `PRODUCT_PLAN.md` covers the architecture and releases. This file refines its §5.7.

**Guiding decision:** The CLI is a thin presentation layer over `engine.py`. Every screen draws only from a `CaseResult`, `ScanResult` or `BatchResult` that has already been computed. The CLI holds no investigation logic of its own and makes up no numbers.

---

## 1. Reconciliations

| Spec says | Conflict / reality | Decision |
|---|---|---|
| Boxed banners, tables, ✓/! glyphs | `PRODUCT_PLAN.md` said "plain echo, no rich" | Use **`rich`**. Typer ≥0.12 already depends on it, so this adds no new dependency. All styling goes through one `ui/` module |
| Welcome screen says "not initialized → run init" | The challenge requires `python code/main.py` to work from a clean checkout | `init` is **optional**. Built-in defaults point at `./dataset` and `./reports`. The welcome screen only *suggests* `init` |
| Config at `./davestigator.yaml` | `PRODUCT_PLAN.md` used `code/config/settings.yaml` | Use one file, `davestigator.yaml` at the repo root, written by `init`. Built-in defaults live in `config.py`. `--config PATH` overrides both |
| `verify` checks "fresh evidence after remediation" | The challenge has no remediation execution, and evidence dated after `audit_at` must not affect `output.csv` | `verify --case ID` evaluates each obligation at `audit_at` by default. `verify --at <ts>` re-evaluates with evidence up to `<ts>`, for the product story only. It **never** writes `output.csv` |
| Example IDs like `DEL-1001` / `EV-001` | Real IDs are `case_NN`, `evidence_NN`, `activity_NN`, `audit_NN` | All examples in the README and tests use real ID shapes |
| `findings` shows severity | Severity must come from rules, not the model | Use a fixed map: `REMAINING_RECORD`/`EXPOSED_PII` = High, `ACTIVE_DATA_FLOW`/`RESTORE_RISK` = High, gap = Info |
| "Audit → Evidence → Report" workflow (flow doc §2) | There is no `evidence` command in the command table | Add `evidence --case ID`. It lists every fact used, with source IDs (P1) |
| `apply` (flow §8) | Not in the CLI spec command table | Add `apply --case ID --dry-run` as a preview only. Without `--dry-run`, the command exits with code 4: *"not permitted in the current mode"* |
| `audit --case` "[✓] Primary deletion supported" | Not part of the challenge steps | Show "satisfied obligations" from `CaseResult.obligations` where `satisfied=True` |
| `--format csv` for reports | Could be confused with the submission file | Per-case CSV goes only to `reports/<case>.csv`. Only `audit --all` writes the root `output.csv` |

---

## 2. Module Layout

```text
code/davestigator/
├── cli.py                 # Typer app: commands + global options only; no logic
├── ui/
│   ├── console.py         # Console factory: color/no-color, quiet, verbose, json mode
│   ├── progress.py        # StageTracker: renders real engine stage events
│   ├── views.py           # render_audit(), render_identity(), render_timeline(), ...
│   ├── masking.py         # mask_email(), mask_phone(), mask_name()
│   └── messages.py        # all user-facing error/warning strings (spec §17)
├── engine.py              # emits StageEvent(stage, status) via callback
└── agent/runner.py        # emits AgentEvent(kind, tool, summary) via callback
```

**The rendering contract** works as follows:
- Each `render_*` function takes a result model and a `Console` and returns nothing.
- `--format json` skips the view and calls `print_json(model.model_dump(mode="json"))`.
- Views therefore never compute anything, and JSON and human output always agree.

---

## 3. Command Surface (final)

```text
Davestigator [GLOBAL OPTIONS] <command> [ARGS]
python code/main.py [GLOBAL OPTIONS] <command> [ARGS]       # identical; challenge entry
```

**Global options** (spec §18):
- `--help`
- `--version`
- `-v/--verbose`
- `-q/--quiet`
- `--config PATH`
- `--no-color` (also honours the `NO_COLOR` environment variable and non-TTY output)
- `--format [text|json]`

Typer rejects unknown options by default, which satisfies the spec's rule against accepting unsupported options.

| Command | Key options | Reads | Writes | Priority |
|---|---|---|---|---|
| *(no command)* | — | config presence | — | P0 |
| `init` | `--force`, `--dataset PATH` | env, dataset | `davestigator.yaml`, `reports/`, `.env.example` | P0 |
| `sources list` | — | config, dataset | — | P1 |
| `scan` | `--rebuild` | all sources | `reports/evidence-index.json`, `cache/` | P1 |
| `audit` | `--case ID` \| `--all`, `--sample`, `--no-llm`, `--workers N`, `--output PATH` | engine | `reports/<case>.json`; with `--all`: `output.csv` | **P0** |
| `identity` | `--case ID` | engine (identity stage only) | — | P1 |
| `timeline` | `--case ID`, `--from`, `--to`, `--system` | engine | — | P1 |
| `findings` | `--case ID` | engine | — | P1 |
| `evidence` | `--case ID` | engine | — | P1 |
| `plan` | `--case ID` | engine | `reports/<case>-plan.json` | P1 |
| `verify` | `--case ID`, `--at TS` | engine | `reports/<case>-verification.json` | P1 |
| `report` | `--case ID`, `--format json\|markdown\|csv` | saved report or engine | `reports/<case>.<ext>` | P1 |
| `apply` | `--case ID`, `--dry-run` (required) | plan | — | P3 (preview only) |
| `validate-output` | `--file PATH` (default `output.csv`) | output + requests | — | **P0** |

**Case-scoped commands** load a cached report from `reports/<case>.json` if it is newer than both the evidence index and the code version. Otherwise they re-run the engine for that one case. This keeps `findings`, `timeline` and the other case commands fast without risking stale results (PRD §10 cache rule).

---

## 4. Screen-by-Screen Build Spec

### 4.1 Welcome (no command)
- Show the banner panel, the version from package metadata, `Mode: Local Evidence Audit`, and `Security: Read-only by default`.
- **If there is no config file:**
  - say *"Using built-in defaults (dataset: ./dataset)"*;
  - suggest `init`.
- **If a config file exists:** list the "Getting started" commands.
- Exit 0.

### 4.2 `init`
**Five real checks:**
1. Python ≥3.10.
2. Load or create the config.
3. The dataset directory exists.
4. The required CSVs are present and have the required columns.
5. `reports/` can be written.

**Each check** prints `OK`, `WARN` or `FAIL`, with the specific missing item.

**Existing config:** if `davestigator.yaml` exists and `--force` is not given, prompt for confirmation in a TTY. In a non-TTY, abort with exit 2.

**Exit codes:** any `FAIL` → exit 2, and the screen does **not** print "Setup complete".

### 4.3 `sources list`
- **Data source:** one table row per core CSV, plus one row per evidence format group (sql/log/json/txt/pdf/png). Each row comes from a real existence-and-validation check.
- **Statuses:** `Available`, `Unavailable`, `Invalid`, `Unsupported` or `Not configured`.
  - PNG without an LLM configured → `Unsupported (needs AI extraction)`.
- **Footer:** counts by status. Nothing is shown as "inspected" at this stage.

### 4.4 `scan`
- **Stages:** Reading, Validating, Extracting, Checking availability, Building index. Each is driven by a `StageEvent` from `FactIndex.build()`.
- **Summary:**
  - sources checked;
  - rows processed;
  - files parsed / failed / unsupported;
  - facts indexed;
  - scan time;
  - index path.
- **Errors:** listed per `file_id`, with the reason.
- The fixed footer note from spec §6 is always printed: *a successful scan ≠ verified deletion*.

### 4.5 `audit --case ID` (primary screen)

**Layout:**
1. Header panel with the case ID, masked email, `audit_at` and `READ-ONLY`.
2. A 6-stage tracker: Load request → Resolve identity → Collect evidence → Build timeline → Evaluate rules → Validate findings. A stage is ticked only when the engine emits `DONE`. If it emits `FAILED`, the tracker shows `FAILED` and an error panel.
3. A result banner: `RESULT: COMPLETE | INCOMPLETE | NOT_APPLICABLE`. The text is always present; colour is optional (green, yellow, dim).
4. **Findings:** `[!]` lines with system, resource and evidence ID, from the FINDING steps.
5. **Satisfied obligations:** `[✓]` lines, capped at 5, followed by "+N more".
6. **Gaps:** `[?]` lines, from `CaseResult.gaps`.
7. **Next commands:** depend on the status:
   - INCOMPLETE → `findings`, `timeline`, `plan`;
   - COMPLETE / NOT_APPLICABLE → `report`.
8. The report path.

**Mode differences:**
- `--verbose` adds the identity path, fact counts per stage, the rule IDs applied, and the agent tool trace (§5).
- `--format json` prints `CaseResult` as JSON and nothing else.

### 4.6 `audit --all`
- **Stages:** Load requests → Identity index → Evidence index → Evaluate cases (a `rich` progress bar, N/518) → Validate output → Write.
- **Summary table:** processed, COMPLETE, INCOMPLETE, NOT_APPLICABLE, failed processing, LLM calls, tokens, and estimated cost. Every number comes from the `BatchResult` counters.
- **Failed cases** are still written as `INCOMPLETE` (see `PRODUCT_PLAN.md` §5.9) and listed by ID with their failing stage.
- **Order of operations:**
  1. Write to `output.csv.tmp`.
  2. Validate it.
  3. Atomically rename it to `output.csv`.

  If validation fails, the existing `output.csv` stays untouched, the run exits with code 3, and the errors are printed per case.

### 4.7 `identity --case ID`
- **Requested identifier:** the masked email, and whether it matched a *current* or a *historical* identifier.
- **Supported identifiers:** a table of kind, masked value, relationship (current / historical / derived hash), source ID (`audit_NN` or the snapshot) and valid-from date.
- **Result:** `Resolved`, `Ambiguous` or `Unresolved`, with counts of evidence-backed links and conflicts.
- **Ambiguous:** print the flow doc §12 message and list the conflicting `user_id`s.
- **Masking:** values are masked unless `--reveal` is passed. `--reveal` is a P2 option, gated by config and only available in a TTY.

### 4.8 `timeline --case ID`
- **Shows:** chronological rows of `time · event label · system/resource · source ID`.
- **Event types** are visually tagged:
  - `CLAIM`: an activity result;
  - `OBSERVED`: a snapshot, file or screenshot;
  - `REQUEST`, `AUDIT_AT`: marker rows.
- **Filters:** `--from` and `--to` are clamped to `≤ audit_at`, and the screen says so when they are clamped. `--system` filters rows.
- **Summary:** generated from fixed templates keyed on the patterns detected, such as "observed PRESENT after reported DELETE". It never states a cause. When a sync precedes a reappearance, it always adds the line *"Causation requires supporting evidence."*

### 4.9 `findings --case ID`
- **Findings:** `F-001…` numbered, with type, rule-derived severity, system, resource, evidence ID, rule ID and a fixed explanation template for each finding type.
- **Unresolved questions:** shown in a separate section, taken from `gaps`. A gap is never shown as a finding.

### 4.10 `evidence --case ID`
- **Shows:** a table of every fact used, with source ID, kind (file/activity/audit), `captured_at`, system, resource, observed state and file path.
- **Excluded facts:** those dated after `audit_at` are listed separately as "excluded (after audit_at)" in a dimmed style, so reviewers can see what was ignored.

### 4.11 `plan --case ID`
- **Header:** `Mode: PLAN ONLY — No data has been changed.`
- **Body:** three phase sections, listing exactly the steps that `output.csv` would contain for this case, in the same order and with the same numbering. Each remediation step shows `Proposed operation: X · Execution: Not performed` and a *reason* that links back to its finding number.
- **Status by case:**
  - COMPLETE → "No remediation required", followed by the verification steps;
  - NOT_APPLICABLE → "No deletion authorized".
- **Saves:** `reports/<case>-plan.json`.

### 4.12 `verify --case ID [--at TS]`
- Checks each obligation and assigns one of four statuses: `VERIFIED`, `FAILED`, `NOT_ATTEMPTED` or `UNVERIFIABLE` (evidence missing).
- Each line shows `Expected:` and `Observed:` for the obligation.
- **Overall result:**
  - `VERIFIED` only if every check is `VERIFIED`;
  - `VERIFICATION FAILED` if any check fails;
  - otherwise `INCOMPLETE VERIFICATION`, listing the checks that could not be verified.
- **`--at`:** prints a banner: *"Evaluating with evidence up to TS (later than audit_at); does not affect output.csv"*.

### 4.13 `report --case ID`
- **Summary sections:** identity, evidence, remediation (proposed / executed=0 / verified) and output files.
- **Export formats:**
  - `--format json`: the full `CaseResult`;
  - `markdown`: a human-readable audit report;
  - `csv`: one row per step.

### 4.14 `apply --case ID --dry-run`
- Shows the preview from flow doc §8.1, followed by the pre-execution checklist. No other code paths exist.

### 4.15 `validate-output`
- **Checks:** 11 lines in the order of spec §15. Each is a real check function returning `(ok, errors[])`.
- **On failure:** print the first 20 errors as `Case / Problem / details / expected`, plus the total count. Exit 3.
- **Shared code:** the validator is the same one `audit --all` uses.

---

## 5. Agent Visibility (spec §16)

- `agent/runner.py` emits `AgentEvent(kind ∈ {STATE, TOOL, RESULT, POLICY, FALLBACK}, tool, summary)`.
- **Summaries** are produced by code from tool names and result counts, for example `"search_facts → 3 facts"`. The model's text is never shown, so no chain of thought can leak.
- **Default mode:** at most one `Agent: …` line per tool call.
- **Verbose mode:** `[AGENT]`, `[TOOL]`, `[RESULT]` and `[POLICY]` lines as in spec §16. The last line is always `[POLICY] Final status calculated by deterministic engine`.
- **Fallbacks:**
  - No API key, or the provider is unreachable → one `AI ASSISTANCE: UNAVAILABLE` notice per run, then a deterministic run.
  - `--no-llm` → the same path, but silent.

---

## 6. Errors & Exit Codes

All messages live in `ui/messages.py`, using the wording from spec §17 and flow doc §12, with real IDs substituted.

| Exit | Meaning | Examples |
|---|---|---|
| 0 | Success | audit done, validation passed |
| 1 | Usage error | unknown option, `--case` and `--all` both/neither |
| 2 | Input/config error | missing CSV, bad column, unparseable `audit_at`, unknown case, config overwrite refused |
| 3 | Output validation failed | invalid op/state, missing cases |
| 4 | Not permitted in current mode | `apply` without `--dry-run`, `--reveal` disabled |
| 5 | Partial: some cases failed processing | `audit --all` with ≥1 failed case (output still written and valid) |

**Rules:**
- Error output goes to stderr and normal output to stdout, so `--format json` stays pipeable.
- No stack traces unless `--verbose`.
- Secrets are never printed; the `ANTHROPIC_API_KEY` value is never echoed.
- Emails and phones are always masked in error text.

---

## 7. Config Schema (`davestigator.yaml`)

```yaml
app:          { log_level: INFO }
dataset:      { root: ./dataset }
reports:      { directory: ./reports, format: json }
output:       { path: ./output.csv }
investigation:
  default_mode: read_only
  max_agent_iterations: 6
  max_tool_calls: 12
  case_timeout_s: 60
  workers: 4
agent:
  enabled: auto            # auto = on if API key present
  provider: anthropic
  model: null              # set explicitly; recorded in usage_report.md
  cache_dir: ./cache
security:
  allow_destructive_actions: false   # 0.x: ignored if true, warning printed
  require_approval: true
  allow_reveal: false
```

The config is validated with Pydantic and unknown keys are rejected. No API key is ever stored in this file; keys come only from the environment.

---

## 8. Build Order

| Step | Deliverable | Depends on | Priority |
|---|---|---|---|
| C1 | `cli.py` skeleton: global options, `--help`, `--version`, welcome screen, `ui/console.py` | — | P0 |
| C2 | `config.py` defaults + `init` | ingestion (M1) | P0 |
| C3 | `validate-output` | validator (M4) | P0 |
| C4 | `audit --all` with progress, summary, atomic write | engine (M4) | P0 |
| C5 | `audit --case` with stage tracker + result view | engine (M4) | P0 |
| C6 | `sources list`, `scan` | FactIndex (M2) | P1 |
| C7 | `identity`, `timeline`, `findings`, `evidence` | engine models | P1 |
| C8 | `plan`, `verify`, `report` (+ report cache) | planner | P1 |
| C9 | Agent event rendering (default + verbose) | agent (M6) | P2 |
| C10 | `apply --dry-run` | plan | P3 |

C1–C5 form the critical path for submission and fit into milestone M5 of `PRODUCT_PLAN.md`. C3 and C4 can land as soon as M4 is green.

---

## 9. Testing

- **CliRunner tests** (Typer's runner) for every command:
  - exit code;
  - required lines present;
  - `--format json` parses;
  - `--no-color` output contains no ANSI escapes.
- **Golden snapshots:** the text output of `audit --case` for 3 sample cases (one per status), stored under `tests/fixtures/cli/`.
- **Contract tests:**
  - `plan --case X --format json` steps == the `output.csv` row for X;
  - the `audit --all` summary counts == the status counts in `output.csv`.
- **Honesty tests:**
  - force an engine stage to raise → the tracker shows `FAILED`, never `DONE`, and the batch counts the case as failed;
  - make a source missing → `sources list` shows `Unavailable` and `scan` reports it.
- **Safety tests:**
  - `apply` without `--dry-run` → exit 4;
  - error output never contains a raw email from the fixture dataset;
  - an API key set in the environment never appears in any output.

---

## 10. Workflows Shipped

**Workflow A — challenge batch** (documented in the README):
```bash
python code/main.py init            # optional
python code/main.py sources list
python code/main.py scan
python code/main.py audit --all     # writes ./output.csv after validation
python code/main.py validate-output
python code/evaluation/main.py      # sample-set metrics → evaluation_report.md
```

**Workflow B — single-case investigation:**
```bash
python code/main.py audit --case case_33
python code/main.py findings --case case_33
python code/main.py timeline --case case_33
python code/main.py plan --case case_33
python code/main.py verify --case case_33
python code/main.py report --case case_33 --format markdown
```

## 11. Definition of Done (CLI)

- [ ] Every command in §3 marked P0/P1 is implemented, with `--help` text and CliRunner tests.
- [ ] No screen displays a count, status or stage that did not come from an executed result.
- [ ] Every status is readable without colour; `--no-color` and `NO_COLOR` are respected.
- [ ] `plan` output and `output.csv` steps are identical for every sample case.
- [ ] No raw PII and no secrets appear in default output or error messages.
- [ ] `audit --all` never leaves a partially written or invalid `output.csv`.
