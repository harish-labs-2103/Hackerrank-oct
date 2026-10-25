# Implementation status vs. plans

Where the built system follows or deviates from `PLAN.md`, `PRODUCT_PLAN.md`, `CLI_PLAN.md` and
`HALLUCINATION_PLAN.md`, and why. Code lives in `code/`.

| Plan item | Status | Notes |
|---|---|---|
| Phase 1 recon (PLAN) | Done | Findings encoded as rules; see `code/evaluation/evaluation_report.md` → "Failures found". |
| Loaders, identity graph, evidence index (M1–M2) | Done | 6 log dialects, SQL (all replica tables), JSON bundles/exports, handover text, PDFs, screenshots. |
| Timeline / state at `audit_at` (M3) | Done | State only from sources captured ≤ `audit_at`; activity rows are claims except explicit checks and "repopulated" events. |
| Policy, planner, validator, CSV (M4) | Done | `--no-llm` produces a valid 518-row file. |
| CLI (M5, CLI_PLAN C1–C10) | Done | All commands incl. `apply --dry-run`; atomic write; exit codes 0–5. |
| LLM (M6, HALLUCINATION_PLAN H0–H7) | Done | Gemma 3 4B via Ollama; L1 intent + L2 screenshot extraction; guard; cache; usage meter. |
| Evaluation + reports (M7) | Done | 32 samples, 7 custom cases, HAL-01..12 tests, influence diff. |
| Packaging (M8) | code.zip built | `python code/tools/package.py` → `code.zip` (119 files, no cache/secrets), clean-room run verified. Uploading and `agent-trace.zip` are left to the user. |

## Deviations (decided during implementation)

1. **Open questions from PLAN §9 answered from the samples:** case ids sort numerically (input
   order); email hash = SHA-256 of the lower-cased email; verification lists *every* relevant
   resource (also already-compliant ones) in INCOMPLETE cases; findings and COMPLETE
   verifications cite the latest admissible file.
2. **Link window** `[requested_at − 1 day, audit_at]` — not in the plans; required by the data
   (stale lookups from earlier episodes).
3. **Screenshot identifiers never create links** (HALLUCINATION_PLAN said "accept if known"):
   the 4B model attaches emails to the wrong table row, so screenshots contribute state and
   name-conflict checks only.
4. **L3 (rule-condition questions) not enabled:** every rule condition in this dataset is
   decidable from structured fields (record type, collection, explicit result), so no model call
   was needed. The guard path exists for L1/L2 only.
5. **Unknown-state handling:** `NEEDS_REVIEW` is not used for unknown states; an unproven
   resource is listed as a gap, appears in INCOMPLETE verification (future check, no evidence),
   and is omitted from COMPLETE verification (nothing admissible to cite).
6. **Inferred account assignment rejected** (see evaluation report, failure #11).

## Connectors (CONNECTOR_PLAN.md / CONNECTOR_CLI_FLOW.md)

| Step | Status | Notes |
|---|---|---|
| C1 contract, registry, mapping schema | Done | `code/davestigator/connectors/{base,mapping,__init__}.py`; ids `connector:collection:id` |
| C2 SQL family + read-only enforcement | Done | Tested on SQLite and a real PostgreSQL 16 (superuser refused, `SELECT`-only role accepted) |
| C3 `connect snapshot` → offline dataset | Done | Offline audit of the snapshot reproduces the live result (test) |
| C4 coverage + "no COMPLETE without FULL" | Done | Live batch validator allows INCOMPLETE without findings only for coverage gaps |
| C5 discovery + PII detection | Done | Dates/numbers excluded from phone detection; values only counted |
| C6 MongoDB, Elasticsearch/OpenSearch | Done (ES tested with a mock transport; Mongo needs `pymongo`, not installed here) | |
| C7 Redis, S3 | Implemented, not integration-tested | S3 read-only must be attested (no portable check) |
| C8 schema-only LLM mapping draft | Done (`map --draft --ai`, enum-validated category) | |
| C9 logs/streams, SaaS adapters | Not built | registry reports them as planned |

Deviations: `--email --live` is implemented for `audit` only (not yet for findings/plan/verify);
sampled discovery values are counted in process memory (never displayed, logged or stored) rather
than inside the database; non-numeric user references cannot be represented in offline snapshots
(linking then uses emails/phones/hashes) and are counted in the snapshot summary.
