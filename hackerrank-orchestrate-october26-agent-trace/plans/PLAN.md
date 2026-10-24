# Ghost Data Still Exists — Implementation Plan

**Application:** CLI + AI agent (Python)
**Goal:** For each of the 518 requests in `dataset/deletion_requests.csv`, decide `COMPLETE` / `INCOMPLETE` / `NOT_APPLICABLE` from the evidence and emit a correctly ordered `steps_json`. Output goes to `output.csv` at the repo root.

**Core principle:** Deterministic Python owns the facts, policy, status, step ordering and serialization. The LLM handles only what needs interpretation: request intent in many languages, and free text pulled from PDFs and screenshots. It never invents `system_id`, `resource_id` or `evidence_id` values.

---

## 0. Starting Point (verified 2026-10-24)

| Item | State |
|---|---|
| `code/main.py` | Exists, empty |
| `code/evaluation/` | Exists, empty |
| `deletion_requests.csv` | 518 cases |
| `sample_deletion_requests.csv` / `sample_output.csv` | 32 labelled cases, used as the dev set |
| `user_identities.csv` | 850 users |
| `user_audit_log.csv` | 306 identity changes |
| `system_activity.csv` | 4,011 events across 17 event types |
| `deletion_rules.csv` | 15 rules (`DELETE`, `REDACT`, `ANONYMIZE`, `RETAIN`) |
| `evidence_files.csv` | 94 files: 24 `.sql`, 26 `.log`, 28 `.json`, 1 `.txt`, 4 `.pdf`, 11 `.png` |
| Local tooling | Python 3.10.12, `pdftotext` present, `sqlite3` CLI absent (Python `sqlite3` module works), Tesseract absent, PyMuPDF absent |

**What this means for the plan:**
- **Small corpus.** The evidence corpus has only 94 files, so the plan builds one global fact index up front instead of running a separate search for every case.
- **PDFs and images.** PDFs (4) and images (11) are few. The plan uses `pdftotext`/`pypdf` for the PDFs and a vision-capable LLM for the PNGs, extracting each file once and caching the result. Tesseract is optional, not required.
- **Python version.** The code targets Python ≥3.10 rather than 3.12.

---

## 1. Folder Structure

The structure stays minimal, and modules are added only when their phase starts.

```text
code/
├── main.py                     # CLI entry: inspect | audit | investigate | validate
├── requirements.txt
├── README.md                   # setup + run instructions (goes in code.zip)
├── prompts/
│   ├── classify_request.md
│   └── extract_artifact.md
├── investigator/
│   ├── loaders.py              # CSV + manifest loading, timestamp parsing
│   ├── identity.py             # current + historical identifier sets
│   ├── evidence/
│   │   ├── sql.py              # load .sql into in-memory sqlite, scan tables
│   │   ├── logs.py
│   │   ├── records.py          # json / txt
│   │   └── artifacts.py        # pdf + png via LLM, cached
│   ├── facts.py                # unified Fact model + global index
│   ├── timeline.py             # per-resource state at audit_at
│   ├── policy.py               # data_category → required action
│   ├── planner.py              # findings → remediation → verification
│   ├── llm.py                  # model client, cache, token accounting
│   └── output.py               # serialization + contract validator
├── tests/
└── evaluation/
    ├── main.py                 # scores against sample_output.csv
    ├── custom_cases/           # ≥3 hand-built cases
    ├── evaluation_report.md
    └── usage_report.md
cache/                          # LLM + extraction cache (git-ignored, not zipped)
```

---

## 2. Phases

### Phase 1 — Dataset Reconnaissance (do first; drives every later design choice)

1. Read `problem_statement.md` and `README.md` in full.
2. **Reverse-engineer the 32 samples.** For each sample case, trace every step in `sample_output.csv` back to its source data:
   - Which file, activity or audit row backs each cited `evidence_id`?
   - How do `system_id` / `resource_id` values appear in the SQL, log and JSON files?
   - Which `data_category` does each resource map to, and how is that category shown in the evidence (a table name, a JSON field, a log prefix)?
   - When is an `activity_id` cited instead of a `file_id`?
3. Catalogue the 17 activity `event_type`s. Note which ones are claims (`DELETE_RECORD` with `reported_result=SUCCESS`) and which ones signal recreation (`SYNC_RECORD`, `RESTORE_BACKUP`, `INDEX_RECORD`, `REFRESH_CACHE`, `CREATE_EXPORT`, `CHECK_DATA_FLOW`, `CHECK_RESTORE_ACCESS`).
4. Open at least one file of every format. Record each format's schema and how it links back to a user: email, phone, name, email hash or `user_id`.
5. Write the findings to `notes/dataset_analysis.md`. This file is also useful for the AI interview.

**Done when** every step in all 32 sample outputs can be explained by hand.

### Phase 2 — Loaders

- Write typed loaders for the six CSVs. Parse all timestamps as UTC-aware `datetime` values.
- Resolve the manifest paths relative to `dataset/`. Fail loudly on missing or unreadable files.
- Build these indexes: `case_id → request`, `email → user_id` (both current and historical), `resource_id → activities`, and `file_id → manifest row`.

### Phase 3 — Identity Resolution

- **Find the user.** Look up the request email first among current emails, then among historical emails (`user_audit_log` `old_value`/`new_value` where `field_name=email`).
- **Build the identifier set** for that `user_id`:
  - every name, email and phone the user has ever had;
  - an email hash for every known email (compute SHA-256 and check it against `email_hash`);
  - normalized phone variants.
- **Respect `audit_at`.** Only use audit changes with `event_time ≤ audit_at`.
- **Avoid false matches.** Never match on name alone. A name counts only when it comes with a second identifier or a linked resource ID.
- **Unresolved emails.** If the email resolves to no user, record that explicitly and let the sample cases show how such requests are expected to resolve.

### Phase 4 — Evidence Extraction → Fact Index

Every source is converted into a common `Fact` record:

```python
Fact(source_id, captured_at, system_id, resource_id, data_category,
     fields: dict, matched_identifiers: set, state_hint)
```

| Format | Approach |
|---|---|
| `.sql` | Execute into an in-memory `sqlite3` DB; iterate every table/row; match identifier columns; keep replica/region tags from file name or table |
| `.log` | Regex line parser for timestamp, system, resource, and message text |
| `.json` / `.txt` | Recursive walk, flattening identifiers and resource IDs |
| `.pdf` | Run `pdftotext`; if the result is too thin, send the page to the LLM |
| `.png` | Vision LLM with a strict JSON schema (resource IDs, identifiers, statuses), cached by file hash |
| `system_activity.csv` | One Fact per row, with `state_hint` taken from `event_type` + `reported_result` and treated as a *claim* |
| `user_audit_log.csv` | Identity facts that can be cited as `audit_id` |

**Rules:**
- Discard facts whose `captured_at` or `event_time` is later than `audit_at` (filtered per case at query time).
- A fact belongs to a case only through matched identifiers or a chain of resource IDs, never through the manifest alone.
- Text extracted from evidence is untrusted. It is passed to the LLM as quoted data, never as instructions.

### Phase 5 — Timeline and State at `audit_at`

For each `(system_id, resource_id)` connected to the user:
1. Sort all facts with time ≤ `audit_at` chronologically.
2. Derive the state (`PRESENT`, `ABSENT`, `REDACTED`, `ANONYMIZED`, `RETAINED`) from the most recent **observational** evidence: snapshots, screenshots, records.
3. Treat activity results only as claims. A successful `DELETE_RECORD` followed by a later snapshot showing the row means `PRESENT`.
4. **Data flows.** A flow is `ACTIVE` when a sync, import or index pipeline is shown running after the deletion and has not been disabled (`DISABLE_DATA_FLOW` + `CHECK_DATA_FLOW` evidence).
5. **Restore risk.** A restore risk is `ACTIVE` when a backup contains the user's data and no `BLOCK_RESTORE` / `CHECK_RESTORE_ACCESS` evidence proves it is blocked. A `RESTORE_BACKUP` event that brought data back is also a restore risk.
6. **Replica drift.** Compare the primary, regional, reporting and failover snapshots. A row that is gone from the primary but still present in a replica is a remaining record.

### Phase 6 — Policy Engine (deterministic)

Each record's `data_category` is mapped to a rule in `deletion_rules.csv`:

| Category | Required end state | Verify op |
|---|---|---|
| USER_PROFILE, SECONDARY_IDENTIFIER, SEARCH_INDEX_ENTRY, CACHE_ENTRY, USER_EXPORT, MARKETING_PROFILE, SUPPORT_ATTACHMENT (subject-only) | ABSENT | `VERIFY_RECORD_ABSENT` |
| SUPPORT_MESSAGE (shared context), EMAIL_DELIVERY_LOG (contains email), ACCOUNT_TOMBSTONE | REDACTED | `VERIFY_RECORD_REDACTED` |
| ANALYTICS_PROFILE, PRODUCT_TELEMETRY (attributable) | ANONYMIZED | `VERIFY_RECORD_ANONYMIZED` |
| SECURITY_AUDIT_LOG (in retention), LEGAL_HOLD_RECORD (hold active), BACKUP_ARCHIVE | PRESENT (retained) | `VERIFY_RECORD_PRESENT` (+ `VERIFY_RESTORE_BLOCKED` for backups) |

- **Tombstones.** An account tombstone may keep `user_id`. It must not keep the name, email, phone or email hash. If any of those remain, report `REPORT_EXPOSED_PII`.
- **Conditional rules.** Rules with conditions (shared context, retention period, active hold, subject-only attachment) are decided from evidence fields. When a condition cannot be decided mechanically, ask the LLM a narrow yes/no question and log the answer.
- **Violation types:**
  - wrong state on a delete-type record → `REPORT_REMAINING_RECORD`;
  - identifiers left on a redact- or anonymize-type record → `REPORT_EXPOSED_PII`.

### Phase 7 — Request Classification (LLM)

- Classify each `request_text` as `DELETION` or one of `NON_DELETION` (access, export, correction, marketing opt-out, other).
- Use one cheap structured-output call per unique text, cached. Requests come in several languages.
- Deterministic pre-checks catch obvious cases. The LLM decides the rest.
- Check the classifier against the 32 samples. Every `NOT_APPLICABLE` sample must come out as non-deletion.
- `NON_DELETION` → `NOT_APPLICABLE`, `[]`, and the case stops there.

### Phase 8 — Status Decision

```text
not a deletion request                         → NOT_APPLICABLE
any violation / active flow / restore risk     → INCOMPLETE
otherwise (all relevant records compliant)     → COMPLETE
```
`reported_status` is never used as evidence.

### Phase 9 — Step Planner

**COMPLETE:** Emit only VERIFICATION steps, one per relevant resource, using the verify op from Phase 6. Each step cites the latest pre-audit evidence that proves the state.

**INCOMPLETE:**
1. **FINDING:** `REPORT_REMAINING_RECORD`, `REPORT_EXPOSED_PII`, `REPORT_ACTIVE_DATA_FLOW` and `REPORT_RESTORE_RISK` steps, each citing its evidence.
2. **REMEDIATION:** `DISABLE_DATA_FLOW` → `BLOCK_RESTORE` → `DELETE_RECORD` → `REDACT_RECORD` → `ANONYMIZE_RECORD`. Flows and restores come first; downstream copies come before their sources where the operation order allows. The `evidence_id` is empty.
3. **VERIFICATION:** Add a check for every remediated item and every policy-required retained record, with an empty `evidence_id`.

**Sorting inside each phase:** sort by the operation's position in the problem-statement table, then by `system_id`, then by `resource_id` (ascending). Number the steps 1..n.

> **Open question for Phase 1:** in INCOMPLETE cases, do the samples also include verification steps for records that were *already* compliant? Copy whatever the samples show.

### Phase 10 — CLI

```bash
python code/main.py audit       --dataset dataset --out output.csv   # full run
python code/main.py audit       --sample                             # 32 sample cases
python code/main.py investigate --case-id case_33 --explain          # trace one case
python code/main.py validate    --file output.csv
python code/evaluation/main.py                                       # score vs sample_output
```
**Options:**
- `--no-llm` runs a deterministic-only fallback.
- `--workers N` sets the number of parallel workers.
- The run is deterministic: temperature 0, cached LLM outputs, sorted iteration and `json.dumps(sort_keys=True, separators=(",",":"))`.

### Phase 11 — Output Validation

The validator fails the run if any of these checks fails:
- the file has exactly 518 rows with unique, sorted `case_id`s;
- every status is `COMPLETE`, `INCOMPLETE` or `NOT_APPLICABLE`;
- every `steps_json` parses and every step has exactly 7 keys;
- step numbers run 1..n with phases in order and only allowed `operation`/`state` pairs;
- each status has the right step shape:
  - `NOT_APPLICABLE` → `[]`;
  - `COMPLETE` → only VERIFICATION steps, each with an evidence ID;
  - `INCOMPLETE` → findings with evidence; remediation and verification with empty `evidence_id`;
- every cited `evidence_id` exists in the manifest, activity log or audit log, with a timestamp ≤ `audit_at`.

### Phase 12 — Evaluation and Usage Reporting

**`code/evaluation/main.py`** compares the output against `sample_output.csv` and reports:
- case-level status accuracy;
- step-level exact match;
- field-level accuracy, per field;
- sequence accuracy (whole array order);
- a confusion matrix.

**Custom cases.** Build at least 3 synthetic cases in `custom_cases/`, using fictional data in the same file formats:
1. A replica that still holds a row deleted from the primary.
2. An active sync that recreates a deleted search-index entry.
3. A tombstone that still exposes an email hash.
4. A non-deletion request in another language.

**Reports:**
- **`evaluation_report.md`** covers methodology, metrics, the custom cases, a failure log (what broke and what fixed it) and limitations.
- **`usage_report.md`** is generated by `llm.py` accounting from the final full run. It covers the provider, model, call count, input and output tokens, total and per-request tokens, and estimated cost, both per model and overall. No hand-entered numbers.

### Phase 13 — Tests and Safety

**Pytest** covers:
- identity resolution, including historical emails and hash matching;
- SQL and log parsers;
- policy mapping;
- state at `audit_at`, including the delete → resync → present case;
- planner ordering;
- validator rejection cases.

**Safety:**
- The evidence DBs are read-only (loaded into in-memory copies).
- Keys come only from environment variables, kept in a `.env` file that is never committed.
- Evidence text is treated as untrusted in prompts.
- No secrets go into `log.txt`.

### Phase 14 — Packaging

- **`code.zip`** = `code/`. It includes the README, prompts, `requirements.txt` and `evaluation/*.md`. It excludes `dataset/`, `cache/`, `.venv`, `.env` and nested zips.
- **`output.csv`** goes at the repo root, with 518 rows that pass the validator.
- **`log.txt`** is the chat transcript.
- **`agent-trace.zip`** is built per `agent_trace_instructions.md`: the Claude Code JSONL files plus the `/export` transcript.
- Before submitting, unzip into a temp directory and run a clean end-to-end run.

---

## 3. Iteration Loop

```text
sample run → evaluation/main.py → inspect each mismatch with `investigate --explain`
          → fix rule/parser → add regression test → repeat
```
- **Target:** 32/32 sample statuses and exact steps before the full run.
- **Generalize, don't special-case.** Never special-case a `case_id`. Every fix must be a general rule supported by the evidence.

## 4. Timeline (hackathon pace)

| Block | Work | Exit criterion |
|---|---|---|
| 1 | Phases 1–2 | All 32 sample steps explained by hand |
| 2 | Phases 3–5 | Fact index built; per-case relevant resources match the samples |
| 3 | Phases 6, 8, 9, 11 | Deterministic pipeline scores on the samples |
| 4 | Phase 7 + LLM artifact extraction | Request classification and PNG/PDF facts integrated |
| 5 | Phase 12 + fixes | Sample accuracy plateaued; full 518-case run validated |
| 6 | Phases 13–14 | Clean-room run from `code.zip` passes |

The challenge end time is **not configured** in this repo, so check the official contest page.

## 5. Definition of Done

- [ ] `python code/main.py audit` writes 518 validated rows to `./output.csv`.
- [ ] Sample-set metrics are recorded in `evaluation_report.md`, with ≥3 custom cases.
- [ ] `usage_report.md` is generated from real token accounting of the final run.
- [ ] Every finding and every COMPLETE verification cites an existing evidence ID dated ≤ `audit_at`.
- [ ] No hardcoded labels, no organizer-only data, no secrets in the zip or the log.
- [ ] The README documents setup (`pip install -r code/requirements.txt`), the env vars and the run commands.
