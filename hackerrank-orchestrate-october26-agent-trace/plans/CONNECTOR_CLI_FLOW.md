# Davestigator — CLI Behaviour for Database Connectors

**Companion to:** `CONNECTOR_PLAN.md` (architecture) and `CLI_PLAN.md` (existing commands).
**Status:** proposed. All screens below are illustrative; names, counts and ids are made up.

The connector commands follow the same rules as the existing CLI: read-only by default, real
progress only, every count comes from an executed operation, identifiers masked, `--format json`
everywhere, and the same exit codes (0 ok · 1 usage · 2 input/config · 3 validation · 4 not
permitted · 5 partial).

---

## 1. The user journey

```text
connect add ──► connect test ──► connect discover ──► connect map --draft
                                                          │
                                         (human reviews mappings.draft.yaml)
                                                          ▼
                                          connect map --approve
                                                          │
                     ┌────────────────────────────────────┴───────────────────────┐
                     ▼                                                            ▼
          LIVE:  audit --email x --live                         SNAPSHOT: connect snapshot --out mydata/
                 audit --requests reqs.csv --live                         audit --all --config mydata.yaml
                     │                                                            │
                     └──────────────► findings / plan / verify / report ◄─────────┘
```

---

## 2. `connect add`: register a data store (no data is read)

```bash
Davestigator connect add --name prod_pg --kind sql --url-env PROD_PG_URL
```

```text
CONNECTOR ADDED

Name:          prod_pg
Kind:          sql (driver: postgresql via SQLAlchemy)
Credentials:   from environment variable PROD_PG_URL   (value not stored, not shown)
Saved to:      davestigator.yaml  → connectors[prod_pg]

Next step:
  Davestigator connect test prod_pg
```

- The URL itself is never accepted on the command line. `--url postgres://user:pass@…` → exit 1 with
  *"Pass credentials through an environment variable (--url-env), not the command line."*
- An unknown `--kind` lists the supported families and exits 1.
- A missing driver library prints the install hint, e.g. `pip install "davestigator[postgres]"`, and exits 2.

---

## 3. `connect test`: connectivity and read-only proof

```bash
Davestigator connect test prod_pg
```

```text
CONNECTOR TEST — prod_pg

[1/4] Resolving credentials (PROD_PG_URL) ........ OK
[2/4] Connecting ................................. OK  (PostgreSQL 16.2, 41 ms)
[3/4] Read-only check ............................ OK  (role has no INSERT/UPDATE/DELETE/DDL grants;
                                                        session forced READ ONLY)
[4/4] Statement timeout / row limit .............. OK  (15 s / 500 rows)

RESULT: READY (read-only)
```

If the account can write:

```text
[3/4] Read-only check ............................ FAILED
      Role "app_admin" can UPDATE public.customers, DELETE public.orders …

RESULT: NOT PERMITTED
Use a read-only role. Davestigator will not run against writable credentials.
```

→ exit **4**. A `--allow-writable` flag does not exist; this is deliberate.

---

## 4. `connect discover`: schema and personal-data candidates

```bash
Davestigator connect discover prod_pg
```

```text
DISCOVERY — prod_pg   (schema only; sampled values never leave the database)

Collections found: 214     Scanned: 214     Skipped (no access): 3

┏━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━┓
┃ Collection              ┃ Rows    ┃ Personal-data candidates (match rate)        ┃ Confidence ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━┩
│ public.customers        │ 1.2 M   │ email (99%) · phone (94%) · full_name (name) │ high       │
│ public.support_messages │ 8.4 M   │ requester_email (97%) · body (text: 12% emails)│ high     │
│ analytics.profiles      │ 3.1 M   │ user_ref (100%) · email_sha256 (hash, 88%)   │ high       │
│ public.orders           │ 22 M    │ customer_id (ref)                            │ medium     │
│ ops.feature_flags       │ 310     │ —                                            │ none       │
└─────────────────────────┴─────────┴──────────────────────────────────────────────┴────────────┘

Collections with personal data: 38   Without: 173   No access: 3 (listed with --verbose)

Next step:
  Davestigator connect map prod_pg --draft
```

- Only match *rates* are shown. A value never appears in the output, the reports or the logs.
- Tables the role cannot read are listed as `NO ACCESS`, never silently skipped.

---

## 5. `connect map`: draft, review, approve

```bash
Davestigator connect map prod_pg --draft        # add --ai for the optional schema-only model suggestions
```

```text
MAPPING DRAFT — prod_pg

Rule-based mappings:      31 collections
AI-suggested (schema only): 7 collections   (model: gemma3:4b, row data not sent)
Unmapped with personal data: 0

Written: mappings.draft.yaml   (every entry marked  review: pending)

Nothing will be audited until the draft is approved:
  edit mappings.draft.yaml, then
  Davestigator connect map prod_pg --approve
```

Approval validates the file and refuses unsafe or ambiguous entries:

```text
MAPPING APPROVAL — prod_pg

[✓] 38 collections have system, category, id and identifier fields
[✓] all categories exist in deletion_rules.csv
[✗] analytics.events: category "ANALYTICS_EVENT" is not in deletion_rules.csv
[✗] public.orders: still marked review: pending

RESULT: NOT APPROVED (2 problems)
```

→ exit **2**. On success: `Approved → mappings.yaml (38 collections, sha256 3f9a…)`. The hash is
recorded in every later report, so a reviewer knows exactly which mapping produced a result.

---

## 6. `sources list`: now includes live stores and coverage

```text
DATA SOURCES

┏━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━━━┓
┃ Source               ┃ Kind     ┃ Status                  ┃ Mapped       ┃ Access    ┃
┡━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━━━┩
│ prod_pg              │ sql      │ Available               │ 38 / 38      │ Read-only │
│ mongo_events         │ document │ Available               │ 12 / 14      │ Read-only │
│ search_es            │ search   │ Unavailable (timeout)   │ 4 / 4        │ —         │
│ crm_hubspot          │ api      │ Not configured          │ —            │ —         │
│ dataset/ (files)     │ files    │ Available               │ 94 files     │ Read-only │
└──────────────────────┴──────────┴─────────────────────────┴──────────────┴───────────┘

Warning: search_es is unavailable. Audits that need it cannot be COMPLETE.
```

---

## 7. `audit --live`: one person, right now

```bash
Davestigator audit --email someone@example.com --live
```

```text
DAVESTIGATOR — LIVE DELETION AUDIT

Subject:     s***@example.com
Audit time:  2026-10-25 10:42:07 UTC (now)
Mode:        READ-ONLY · LIVE
Mapping:     mappings.yaml (sha256 3f9a…)

[1/6] Resolve identity ...................... DONE  (user_8812; 2 emails, 1 phone, 2 hashes)
[2/6] Query prod_pg (38 collections) ........ DONE  38 FULL
[3/6] Query mongo_events (12 collections) ... DONE  11 FULL · 1 PARTIAL (row limit 500 reached)
[4/6] Query search_es (4 indexes) ........... FAILED (timeout after 15 s)
[5/6] Evaluate rules ........................ DONE
[6/6] Validate findings ..................... DONE

RESULT: INCOMPLETE

FINDINGS
[!] Remaining record     search_service   (not checked: search_es unavailable)
[!] Exposed PII          support_platform  prod_pg:public.support_messages:88412   live@10:42:05
[!] Active data flow     integration_service  mongo_events:connectors:cn_77        live@10:42:06

COVERAGE GAPS
[?] mongo_events.audit_trail   PARTIAL (row limit)  → re-run with --row-limit 5000
[?] search_es (4 indexes)      NOT CHECKED (unavailable)

Evidence journal: reports/evidence/run_20261025T104207Z.jsonl
Report:           reports/live/user_8812.json
```

The exact search-service finding depends on the data, but the rule holds: **COMPLETE is never
printed while any mapped collection is PARTIAL or NOT CHECKED.** The status becomes INCOMPLETE,
with the gap listed so the reason is visible.

Batch form:

```bash
Davestigator audit --requests requests.csv --live --output live_output.csv
```

This produces the same `case_id,deletion_status,steps_json` format as the challenge file and runs
the same validator. The difference is that live findings cite `connector:collection:id@timestamp`
evidence ids, recorded in the evidence journal.

---

## 8. `connect snapshot`: export once, audit offline

```bash
Davestigator connect snapshot prod_pg --subjects requests.csv --out mydata/evidence/databases/
```

```text
SNAPSHOT — prod_pg

Subjects:              120 (from requests.csv)
Collections captured:  38 (snapshot_scope written)
Records exported:      1,904 (only records matching the subjects' identifiers)
File:                  mydata/evidence/databases/prod_pg_20261025T104500Z.sql
Manifest updated:      mydata/evidence_files.csv  (evidence_ prod_pg_20261025T104500Z, captured_at 10:45:00Z)

Next step:
  Davestigator --config mydata.yaml audit --all
```

This output is the SQLite-compatible format the tool reads **today**. Teams that won't allow live
access run this one command inside their network and hand over the folder.

---

## 9. Existing commands keep working

`identity`, `timeline`, `findings`, `evidence`, `plan`, `verify`, `report` and `apply --dry-run` accept
`--email … --live` as well as `--case …`.

- **`verify --live`** re-queries the stores *now*, after remediation. This is the "independent
  verification" step from the PRD: it never trusts the fact that a DELETE statement returned success.
- **`evidence --live`** prints the journal lines (connector, collection, record id, query time,
  identifier kinds matched) and never prints values.

---

## 10. Errors

| Situation | Message | Exit |
|---|---|---|
| Credentials env var missing | `Environment variable PROD_PG_URL is not set.` | 2 |
| Writable credentials | `Role can modify data. Use a read-only role.` | 4 |
| Driver not installed | `PostgreSQL driver missing: pip install "davestigator[postgres]"` | 2 |
| Mapping not approved | `prod_pg has no approved mapping. Run connect map --draft, review, then --approve.` | 2 |
| Store unreachable during audit | `search_es unreachable; affected collections marked NOT CHECKED.` | 5 (batch) / result INCOMPLETE |
| Query over cost budget (warehouse) | `bq_analytics.events: estimated 2.1 TB > budget 50 GB; skipped (NOT CHECKED).` | 5 |
| Mapping refers to a missing table | `prod_pg: public.customers_v2 not found; mapping is stale.` | 2 |

Secrets, connection strings and raw identifier values never appear in any message.
