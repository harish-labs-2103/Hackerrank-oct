# Davestigator — Product & Architecture Implementation Plan

**Source documents:** *Davestigator PRD v1.0* and *Technical Architecture & Implementation Specification v1.0*. Both are proposed designs.
**Companion:** `PLAN.md` covers the challenge-specific phases (dataset reconnaissance, policy table, step ordering). This file maps the PRD and tech spec onto a buildable plan and resolves where they conflict with the repo.

**Guiding decision:** Ship Release 0.1, the challenge engine, first and without needing an LLM to run. Every later release adds to it and never changes how deterministic decisions are made.

---

## 1. Spec vs. Repo Reality: Reconciliations

| Spec says | Repo / environment reality | Decision |
|---|---|---|
| Python 3.12+ | Python 3.10.12 installed | Target **≥3.10**; avoid 3.11+ syntax (`Self`, `except*`, `tomllib`) |
| PyMuPDF + Tesseract OCR | Neither installed; `pdftotext` present; 4 PDFs, 11 PNGs total | `pypdf`/`pdftotext` for PDFs; vision LLM for PNGs (cached); Tesseract as optional extra |
| `code/evaluation/main.py` "already exists" | Only `code/main.py` (empty) and an empty `code/evaluation/` exist | Create both |
| `DeletionRequest.subject_id` | Requests carry `email`, not a subject ID | Model has `email`; `user_id` is *derived* by the identity resolver |
| Identity links: account ID, external ID, historical user ID | Data provides `user_identities` (name/email/phone/email_hash) and `user_audit_log` field changes | Identity graph nodes = emails, phones, names, hashes; edges = snapshot rows + audit changes with `event_time` |
| Case IDs like `DEL-1001` | Real IDs are `case_01`…`case_N` | CLI accepts any string; sort order follows the challenge (`case_id` as stored; confirm lexical vs numeric in Phase 1) |
| `NEEDS_REVIEW` / `INSUFFICIENT_EVIDENCE` | Challenge allows only 3 statuses | Internal enum may carry them in reports; **CSV mapper collapses them** (see §5.4) |
| Confidence scores per evidence | Challenge is graded on exact steps | Keep the 5 confidence dimensions as *labels* in reports, never as thresholds that change output |
| SQLite as metadata store / SQLAlchemy | 94 evidence files, fits in memory | In-memory indexes + pickle/JSON cache; no persistent DB in 0.1 |
| LiteLLM or provider SDK | No provider chosen | Thin `ModelClient` protocol; one Anthropic implementation first; LiteLLM optional |
| `Davestigator` console command | Challenge requires `python code/main.py` | Both: `main.py` calls `cli.app()`; `pyproject.toml` exposes the console script |

---

## 2. Scope by Release

| Release | Contents | Maps to PRD FRs | Required for submission? |
|---|---|---|---|
| **0.1 Challenge engine** | Loaders, identity, evidence, timeline, policy, status, planner, validator, `output.csv` | FR-01…FR-08 | **Yes** |
| **0.2 CLI + reports** | Typer commands, per-case JSON/Markdown reports, structured logging, dry-run plan | FR-09, FR-10 | Yes (README run commands) |
| **0.3 AI agent** | Request classifier, artifact extraction, bounded investigation loop, usage accounting | FR-11 | Yes (usage report needs real model calls) |
| 0.4 Connectors | `EvidenceSource` protocol, file + SQLite connectors, coverage report | FR-12 | No: interface only |
| 1.0 Production | Approval-gated `apply`, live verification, scheduling | FR-13, FR-14 | No: post-challenge |

The request classifier is moved into 0.1 because multilingual intent decides `NOT_APPLICABLE`. Without an LLM, the engine falls back to a keyword/heuristic classifier and records that it did.

---

## 3. Package Layout (trimmed from spec §3)

The spec's layout has 40+ modules. That many modules up front would mean many empty files. Start with the layout below and split a module only when it grows past ~400 lines.

```text
code/
├── main.py                        # `from davestigator.cli import app; app()`
├── pyproject.toml                 # deps + console script `Davestigator`
├── requirements.txt               # pinned mirror for pip users
├── README.md
├── config/settings.yaml           # paths, model names, limits, cost table
├── prompts/
│   ├── classify_request.md
│   ├── extract_artifact.md
│   └── investigate.md
├── davestigator/
│   ├── cli.py                     # Typer app
│   ├── config.py
│   ├── models.py                  # all Pydantic models (split later if large)
│   ├── ingestion.py               # CSV + manifest loaders, timestamp parsing
│   ├── identity.py                # IdentityGraph + resolver
│   ├── evidence/
│   │   ├── registry.py            # file_id → parser dispatch, global FactIndex
│   │   ├── sql.py | logs.py | records.py | artifacts.py
│   ├── timeline.py
│   ├── policy.py                  # rule table, condition evaluators
│   ├── engine.py                  # Investigation pipeline (one case → CaseResult)
│   ├── planner.py                 # findings → remediation → verification + ordering table
│   ├── agent/
│   │   ├── client.py              # ModelClient protocol, cache, usage meter
│   │   ├── tools.py               # read-only tool registry + arg validation
│   │   └── runner.py              # bounded loop
│   ├── reporting.py               # CSV writer, JSON/MD case reports, usage_report.md
│   └── validation.py              # challenge contract validator
├── tests/{unit,integration,regression,fixtures}/
└── evaluation/
    ├── main.py
    ├── custom_cases/
    ├── evaluation_report.md
    └── usage_report.md
```

**Dependencies, kept minimal:**
- **Runtime:** `typer`, `pydantic>=2`, `pyyaml`, `pypdf`, `anthropic` (optional extra).
- **Dev:** `pytest`, `ruff`.
- **Not used:** pandas. The stdlib `csv` module is enough for about 5k rows.

---

## 4. Data Models (adapted to the actual schema)

```python
class DeletionRequest(BaseModel):
    case_id: str
    email: str
    requested_at: datetime
    reported_status: Literal["COMPLETE", "PARTIALLY_COMPLETE", "NOT_STARTED"]  # claim only
    reported_at: datetime
    audit_at: datetime
    request_text: str

class Identifier(BaseModel):
    kind: Literal["email", "phone", "name", "email_hash", "user_id"]
    value: str                       # normalized
    valid_from: datetime | None
    valid_to: datetime | None
    source_id: str                   # user_identities row / audit_id

class ResolvedIdentity(BaseModel):
    user_id: str | None
    identifiers: list[Identifier]
    path: list[str]                  # how the request email reached user_id
    ambiguities: list[str]

class Fact(BaseModel):               # = spec's EvidenceRecord, normalized
    source_id: str                   # file_id | activity_id | audit_id
    source_kind: Literal["file", "activity", "audit"]
    captured_at: datetime            # manifest captured_at or event_time
    system_id: str | None
    resource_id: str | None
    data_category: str | None
    event_type: str | None           # activity rows
    reported_result: str | None      # claim, never state
    observed_state: Literal["PRESENT","ABSENT","REDACTED","ANONYMIZED","ACTIVE","DISABLED","BLOCKED"] | None
    matched_identifiers: list[str]   # identifier kinds only; no raw PII in reports
    raw_ref: str                     # file path + row/line locator

class Obligation(BaseModel):         # policy output per resource
    system_id: str; resource_id: str; data_category: str
    rule_id: str
    required_state: str
    current_state: str | None
    satisfied: bool
    evidence_ids: list[str]

class Step(BaseModel):               # challenge contract, extra="forbid"
    step_no: int; phase: Phase; system_id: str; resource_id: str
    operation: Operation; state: State; evidence_id: str

class CaseResult(BaseModel):
    case_id: str
    internal_status: Literal["COMPLETE","INCOMPLETE","NOT_APPLICABLE","NEEDS_REVIEW","INSUFFICIENT_EVIDENCE"]
    deletion_status: Literal["COMPLETE","INCOMPLETE","NOT_APPLICABLE"]
    steps: list[Step]
    obligations: list[Obligation]
    timeline: list[TimelineEvent]
    gaps: list[str]
    errors: list[str]
```

`Step` validates its own operation/state pair with a `model_validator`. That way an invalid pair cannot even be constructed.

---

## 5. Component Plans

### 5.1 Ingestion (spec §5)
- Use `csv.DictReader` with a required-columns check for each file. Fail immediately if a required column is missing.
- **Timestamps:** parse the ISO-8601 `Z` suffix as UTC. A naive timestamp is recorded as an error and never given a guessed timezone.
- **Duplicate `case_id`s** stop the run with an error. Duplicate activity or audit IDs are only warned about.
- **Manifest:** a path that is missing, unreadable or outside `dataset/` (path traversal) becomes an `IngestionError` on that file and does not abort the batch.

### 5.2 Identity Graph (spec §6)
- **Nodes** are normalized identifiers:
  - emails are lowercased and trimmed;
  - phones are reduced to digits plus country code;
  - names are case-folded but only used as a *corroborating* attribute.
- **Edges come from two sources:**
  - each snapshot row links `user_id` to its identifiers;
  - each audit row with `field_name ∈ {email, phone, name}` links `old_value` and `new_value` to the same `user_id`, with a validity window at `event_time`.
- **Email hashes:** compute SHA-256 for every known email of the user and add it as a hash node. Phase 1 confirms the hash recipe against `user_identities.email_hash`.
- **Resolution:** request email → `user_id` → every identifier with `event_time ≤ audit_at`.
- **Conflict handling:** if one email maps to more than one `user_id` → `ambiguities`, and the case is marked internally as `NEEDS_REVIEW`.
- **Never** link records on a name alone, or because two values co-occur in the same log line.

### 5.3 Evidence & Timeline (spec §7–8)
- **Build the index once.** `FactIndex` parses every file once at startup and indexes facts by identifier value, `resource_id` and `system_id`.
- **Per-case queries** filter on `captured_at ≤ audit_at`. Facts after `audit_at` are not deleted; they are only invisible to that case.
- **Resource closure.** Starting from facts matching the user's identifiers, follow `resource_id` links (activity `LINK_RESOURCE_IDENTITY`, `SYNC_RECORD` source → target) to collect downstream copies. Each hop records its evidence.
- **State resolution** per resource takes the latest *observational* fact before `audit_at`. Activity results are ranked below snapshots and screenshots, and a later observation always beats an earlier claim.
- **Causation.** Ordering alone never establishes cause. A sync is reported as `REPORT_ACTIVE_DATA_FLOW` only when there is evidence the flow is active (for example, a later `SYNC_RECORD` or `CHECK_DATA_FLOW` showing it enabled). Phase 1 confirms the exact rule against the samples.
- **Missing evidence ≠ evidence of absence (spec §9.4).** `ABSENT` requires positive proof, such as a snapshot that covers the table but lacks the row, or an explicit verification artifact. Otherwise the resource state is `UNKNOWN`, which is recorded as a gap.

### 5.4 Policy Engine & Status (spec §9)
- The rule table is loaded from `deletion_rules.csv`. Each rule is keyed by `data_category`, and each condition is mapped to an evaluator function, e.g. `retention_period_active(fact, audit_at)` or `legal_hold_active(...)`.
- **Precedence**, highest first:
  - RETAIN with its condition met (legal hold, security retention);
  - REDACT or ANONYMIZE;
  - DELETE.

  BACKUP_ARCHIVE is retained, but the restore path must still be blocked.
- **Conditions the code can't decide** go to the LLM as a narrow yes/no question with quoted evidence. The answer is logged. If no LLM is available, the case is marked internally as `NEEDS_REVIEW`.
- **Status:**
  ```text
  not deletion intent                        → NOT_APPLICABLE
  any unsatisfied obligation / flow / risk   → INCOMPLETE
  all obligations satisfied with evidence    → COMPLETE
  ```
- **Collapsing internal statuses** into the challenge CSV:
  - `NEEDS_REVIEW` → `INCOMPLETE`;
  - `INSUFFICIENT_EVIDENCE` → `INCOMPLETE`.

  The conservative reason: COMPLETE requires proof. Phase 1 confirms this against the samples.

### 5.5 Planner (spec §11)
- Use an explicit `OP_ORDER` table copied from the problem-statement operation table. Never sort operations alphabetically.
- **Sort key:** `(phase_rank, op_rank, system_id, resource_id)`. Then number the steps 1..n.
- **Evidence IDs:**
  - FINDING steps use the evidence ID of the fact that proves the violation;
  - in INCOMPLETE cases, REMEDIATION and VERIFICATION steps use `""`;
  - in COMPLETE cases, VERIFICATION steps use the proving evidence ID.
- **Dependencies (spec §12.3):** disable the flow, then block restores, then change records. The fixed operation order already enforces this, and the planner asserts it.

### 5.6 Agent (spec §10), bounded, read-only

**Uses, in order of value:**
1. **Request intent classification:** `DELETION` vs. `ACCESS`, `EXPORT`, `CORRECTION`, `OPT_OUT` or `OTHER`. Requests come in many languages. Cached per text hash.
2. **Artifact extraction:** PNG and image-only PDF → JSON facts, matching the `Fact` schema.
3. **Gap-driven investigation loop:** runs only when the deterministic engine reports gaps (an unresolved identity, an `UNKNOWN` state, or an undecidable rule condition).

**Tools** (all read-only, with Pydantic-validated arguments):

| Tool | Returns |
|---|---|
| `get_case(case_id)` | Request fields |
| `get_identity(case_id)` | Identifier *kinds* + `user_id` (raw values only when needed for matching) |
| `search_facts(case_id, resource_id? , system_id?, identifier_kind?)` | Facts with `captured_at ≤ audit_at`, capped at 25 |
| `get_activity(resource_id, before=audit_at)` | Activity rows |
| `get_rule(data_category)` | Rule row |
| `read_evidence_excerpt(file_id, locator)` | ≤2 KB quoted excerpt, wrapped in `<evidence>` tags |
| `propose_fact(...)` | Agent proposes a fact; engine accepts only if `source_id` exists and excerpt supports it |

**Limits** (set in `settings.yaml`):
- at most 6 iterations and 12 tool calls per case;
- at most 8k output tokens;
- 60 s wall-clock per case.

When a limit is hit, the agent returns what it has, with the remaining gaps listed.

**Guardrails:**
- The agent has no shell, no raw SQL and no file paths.
- Any ID the model returns is checked against the index.
- Proposals that cite unknown evidence are dropped and counted in the report.

**Determinism:**
- temperature 0;
- a response cache keyed by `(prompt_version, model, input_hash)`;
- the cache is committed to `cache/` locally but excluded from the zip.

**Usage meter:** records every call (provider, model, input and output tokens, cost from the `settings.yaml` price table). `usage_report.md` is generated from this record.

### 5.7 CLI (PRD §7)

| Command | 0.x behaviour |
|---|---|
| `init` | Write `config/settings.yaml` template + `.env.example` |
| `sources list` | Show manifest summary by format/origin; parse status |
| `scan` | Build the FactIndex, report parser failures and coverage |
| `audit --case ID` / `audit --all [--sample] [--no-llm] [--workers N]` | Run engine; `--all` writes `./output.csv` |
| `report --case ID [--format md\|json]` | Timeline, obligations, steps, gaps |
| `plan --case ID` | Remediation plan only |
| `verify --case ID` | Re-evaluate state at `audit_at`; print per-obligation pass/fail |
| `apply --case ID --dry-run` | Print the actions that would run; **non-dry-run refuses** in 0.x |
| `validate-output [--file output.csv]` | Contract validator; non-zero exit on failure |

The `rich`-style progress output in PRD §7.2 is replaced by plain Typer `echo` output, to keep dependencies minimal. Raw PII is never printed; the email is masked as `a***@gmail.com`.

### 5.8 Reporting & Validation (spec §13)
- Write `output.csv` with `csv.writer(quoting=QUOTE_MINIMAL)`. Serialize `steps_json` with `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)`, which matches the sample style.
- **Validator checks:**
  - the full contract from `PLAN.md` §Phase 11;
  - every cited evidence ID exists;
  - every cited evidence item is timestamped ≤ `audit_at`.
- **Per-case reports** go to `reports/<case_id>.json` and `.md`. They are git-ignored and not zipped.

### 5.9 Error Handling (spec §15)
- Each case runs in a `try` block. On an exception, `errors` is recorded, the internal status is set to `NEEDS_REVIEW` and the CSV status to `INCOMPLETE`, and the batch keeps going.
- A case that errors is **never** reported as COMPLETE.
- The end-of-run summary prints counts by status and error stage. The run exits non-zero if any case is missing from the output.

---

## 6. Security Plan (PRD §6, spec §14)

| Control | Implementation |
|---|---|
| Read-only | SQL snapshots loaded into `:memory:` sqlite; evidence opened `rb`; no write paths into `dataset/` |
| Path confinement | All manifest paths resolved and checked `is_relative_to(dataset_root)` |
| Prompt injection | Evidence only inside `<evidence>` blocks with a system rule that it is data; tool allowlist enforced in code; model output schema-validated |
| Secrets | `ANTHROPIC_API_KEY` etc. from env / `.env` (gitignored); never echoed; `.env.example` has placeholders |
| PII minimization | Prompts get identifier *kinds* and masked values unless matching requires raw; reports mask emails/phones |
| Destructive ops | `apply` without `--dry-run` raises `NotImplementedError("approval-gated execution is a 1.0 feature")` |
| Adversarial tests | Spec §16.3 cases 1–8 become fixtures in `tests/regression/` |

---

## 7. Testing & Evaluation (spec §16)

- **Unit tests** for each module named in spec §16.1.
- **Integration tests:** the 32 sample cases run end-to-end. Assert status plus exact `steps_json` equality, and report diffs field by field.
- **Adversarial fixtures:** a small synthetic `dataset/` in `tests/fixtures/` covering the 8 spec §16.3 scenarios. At least 3 of them are written up as the custom evaluation cases in `evaluation_report.md`.
- **Regression tests:** each sample mismatch that gets fixed is frozen as a fixture.
- **Metrics reported by `code/evaluation/main.py`:**
  - status accuracy;
  - step exact-match;
  - per-field accuracy;
  - sequence accuracy;
  - invalid-output rate;
  - evidence-reference validity;
  - identity-resolution accuracy (on samples where the cited resources reveal the user);
  - share of cases decided without an LLM;
  - cost per request.

---

## 8. Milestones

| # | Milestone | Exit criterion |
|---|---|---|
| M0 | Recon (`PLAN.md` Phase 1) | Every sample step traced by hand; open questions answered (sort order, hash recipe, flow-active rule, COMPLETE verification set) |
| M1 | Models + ingestion + identity | Unit tests green; all 518 request emails resolve or are logged as unresolved |
| M2 | FactIndex for all 5 formats | `scan` reports 94/94 files parsed (PNGs via cached LLM or flagged) |
| M3 | Timeline + policy + status | Sample status accuracy measured; deterministic only |
| M4 | Planner + validator + CSV | `audit --all --no-llm` produces a valid 518-row `output.csv` |
| M5 | CLI surface + reports | All §5.7 commands work; README written |
| M6 | Agent (classifier, extraction, gap loop) + usage meter | Sample accuracy ≥ M4; usage logged |
| M7 | Evaluation + hardening | `evaluation_report.md`, adversarial suite green, final full run → `usage_report.md` |
| M8 | Package | `code.zip` clean-room run passes; `agent-trace.zip` built per `agent_trace_instructions.md` |
| post | 0.4 connectors / 1.0 apply+verify | Out of challenge scope |

---

## 9. Open Questions (resolve in M0)

1. Is `case_id` sorting lexical (`case_10` < `case_2`) or natural order? Check the order of rows in `sample_output.csv`.
2. What is the email hash recipe (SHA-256 of the lowercased email? salted?)? Verify against `user_identities.csv`.
3. What evidence counts as "active data flow" versus a one-off sync?
4. Which evidence makes a COMPLETE verification step cite a `file_id` rather than an `activity_id`?
5. In INCOMPLETE cases, do already-compliant retained records still get a `VERIFY_RECORD_PRESENT` step?
6. What happens when a request email resolves to no user? Look for a sample with that situation.

## 10. Definition of Done

This plan uses spec §18 items 1–15 as its definition of done, plus:
- `python code/main.py audit --all --no-llm` produces a valid output, which proves the deterministic fallback works.
- No LLM call can change a status unless it goes through a validated fact or a logged rule-condition answer.
- `log.txt` entries are present for every session and turn.
