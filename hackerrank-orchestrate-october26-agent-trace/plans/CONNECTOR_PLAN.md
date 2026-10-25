# Davestigator — Universal Database Connector Design

**Goal:** let any organization point Davestigator at its own databases and data stores,
read-only, and audit deletion requests against live data. The engine stays unchanged; connectors
produce the same `Observation` records that the dataset parsers already produce.

**Status:** proposed design. Today the tool only audits a dataset folder (see `IMPLEMENTATION_STATUS.md`).

---

## 1. Core idea: one contract, many drivers

Everything the engine needs from a data store reduces to three questions:

1. **Find:** which records in this store mention this person's identifiers?
2. **Read:** what does each record contain right now (which identifiers are still on it)?
3. **Prove coverage:** did we search the whole collection, so "not found" means absent?

Every connector implements those three methods, whatever the technology:

```python
class Connector(Protocol):
    name: str                 # "prod_postgres"
    kind: str                 # "sql" | "document" | "search" | "kv" | "warehouse" | "object" | "log" | "api"

    def discover(self) -> list[CollectionInfo]: ...
        # tables/collections/indexes/buckets + columns/fields + row counts (schema only, no data)

    def find(self, collection: CollectionMap, identifiers: set[Identifier], limit: int) -> Iterator[RawRecord]: ...
        # parameterized lookups on mapped identifier fields only

    def coverage(self, collection: CollectionMap) -> Coverage: ...
        # "this search covered the full collection at time T" (or "partial: sampled / timed out")
```

`RawRecord` then flows through the existing `interpret_record()` into an `Observation`. Identity
resolution, state at audit time, policy, planning and validation don't change at all.

```text
             ┌───────────────┐    ┌──────────────────┐
config.yaml →│ Connector     │ →  │ interpret_record │ → Observation → engine (unchanged)
             │ registry      │    └──────────────────┘
             └──────┬────────┘
     ┌──────┬───────┼────────┬─────────┬─────────┬────────┐
    SQL   Mongo  Elastic  Redis   Snowflake    S3      Kafka/logs …
```

---

## 2. Driver families (covers "all kinds of DB")

Don't write one connector per product. Write one per **access pattern**; most products then plug
in through a standard library.

| Family | Products covered | Library | How `find` works |
|---|---|---|---|
| **SQL (relational)** | PostgreSQL, MySQL/MariaDB, SQL Server, Oracle, SQLite, CockroachDB, Aurora, Cloud SQL | SQLAlchemy dialects | `SELECT mapped_cols FROM t WHERE email = :v OR phone = :v …` (bound parameters only) |
| **Warehouse** | Snowflake, BigQuery, Redshift, Databricks SQL, ClickHouse | SQLAlchemy dialects / native SDK | Same as SQL, plus cost guard: partition filter + dry-run byte estimate before running |
| **Document** | MongoDB, DocumentDB, Couchbase, Firestore, CosmosDB | pymongo / SDKs | `{"$or": [{"email": v}, {"profile.phone": v}]}` with field paths from config |
| **Search index** | Elasticsearch, OpenSearch, Solr, Algolia | REST clients | `term` queries on mapped fields; never full-text "match" (avoids look-alike hits) |
| **Key-value / cache** | Redis, Memcached, DynamoDB, Cassandra | redis-py, boto3, cassandra-driver | Key pattern from config (`user:{user_id}:*`); DynamoDB/Cassandra via GSI/partition key |
| **Object storage** | S3, GCS, Azure Blob, MinIO | boto3 / gcsfs / azure-storage | List by prefix (`exports/{user_id}/`) plus object metadata/tags; optionally scan small text objects |
| **Logs / streams** | Kafka topics, CloudWatch, Loki, Splunk, ELK | consumers / query APIs | Time-bounded query for identifiers; never reads unbounded history |
| **SaaS / API** | CRM, support desk, marketing tools (Zendesk, HubSpot, Salesforce…) | HTTP + OAuth | Vendor "search contact by email" endpoint, mapped by a small adapter |
| **Backups / archives** | Backup catalogs, snapshot registries | catalog APIs | Never restores data. Reads catalog metadata only: which archive covers the subject, and whether restore is allowed |

Adding a new product within a family means a config entry, not code. A new *family* needs one
driver class, about 150–300 lines.

---

## 3. Mapping config: how a store becomes something the engine understands

The engine needs four facts about every collection: which **system** it belongs to, which
**category** it is (for `deletion_rules.csv`), its **record id**, and which **fields hold
identifiers**. Those facts come from config, not from hard-coded naming conventions.

```yaml
connectors:
  - name: prod_postgres
    kind: sql
    url_env: PROD_PG_URL              # read from the environment only, never stored in the file
    collections:
      - source: public.customers
        system: account_service
        category: USER_PROFILE
        id: id                        # becomes resource_id "customers:<id>"
        identifiers: {email: email, phone: phone, user_ref: user_id, name: full_name}
        state:
          deleted_when: "deleted_at IS NOT NULL"   # soft-delete columns count as ABSENT

      - source: public.support_messages
        system: support_platform
        category: SUPPORT_MESSAGE
        id: id
        identifiers: {email: requester_email, user_ref: customer_id}
        text_fields: [body]           # scanned for emails/phones (exposed PII inside text)

  - name: search
    kind: search
    url_env: ES_URL
    collections:
      - source: customer_directory_v3
        system: search_service
        category: SEARCH_INDEX_ENTRY
        id: _id
        identifiers: {email: email, user_ref: customer_reference}

  - name: segment_sync
    kind: api
    adapter: generic_rest
    collections:
      - source: /v1/destinations
        system: integration_service
        category: DATA_FLOW
        id: id
        identifiers: {user_ref: subject_selector, email: subject_selector}
        state: {active_when: "enabled == true"}
```

**Resource ids:** generalize the hard-coded prefixes (`account_123`) to `<connector>:<collection>:<id>`.
`interpret.py` then reads system and category from the mapping instead of guessing from the id.

---

## 4. Auto-discovery and an AI-assisted mapping draft

Writing mappings by hand for hundreds of tables is the real barrier, so the tool helps:

1. **`Davestigator connect discover prod_postgres`**: lists tables, columns, types and row counts (schema only).
2. **PII column detection**, deterministic first:
   - column-name rules (`email`, `e_mail`, `phone`, `msisdn`, `user_id`, `customer_ref`, `*_hash`);
   - sampling up to 50 values per candidate column *inside the database* to test formats (email regex, E.164 phones, 64-hex hashes). Only the match rate leaves the database, never the values.
3. **LLM-assisted draft (optional):** the local model sees **schema only** (table and column names, types, the match rates from step 2) and proposes `system` / `category` / `identifiers` per table. It never sees row data.
4. **Human review gate:** the draft is written to `mappings.draft.yaml`. Nothing is audited until a person approves it as `mappings.yaml`. Policy categories are a compliance decision, so the model only suggests them.

---

## 5. Safety rules (non-negotiable)

| Risk | Control |
|---|---|
| Accidental writes | Connect with **read-only credentials**; SQL connections open `READ ONLY` transactions; the driver API has no write methods at all |
| SQL/query injection | Only bound parameters; table and column names come from the validated mapping, never from evidence or the model |
| Production load | Per-connector rate limit, statement timeout, row `limit`, run against replicas where configured |
| Warehouse cost | Dry-run estimate and a byte budget per query; abort above the budget |
| Credential leakage | `*_env` references only; secrets never written to logs, reports, cache or `log.txt` |
| PII spreading | Raw values stay in memory for matching. Reports store identifier *kinds* and masked values. The LLM never receives row data |
| False "clean" results | Every collection reports coverage: `FULL`, `PARTIAL` (timeout/limit) or `NOT_CHECKED`. `COMPLETE` is impossible while a mapped collection is not `FULL` |
| Backups | Catalog metadata only; never restores or mounts an archive |

---

## 6. Live mode vs. snapshot mode

- **Live audit:** `audit_at` = now. Each finding cites `connector:collection@timestamp` (a new evidence kind `live`) and is written to an evidence journal (`reports/evidence/<run_id>.jsonl`) so the result can be reproduced and reviewed later.
- **Snapshot mode:** `Davestigator connect snapshot prod_postgres --out mydata/evidence/` writes SQLite-compatible SQL with `snapshot_scope`, which is the format the tool already reads. Teams that cannot allow the auditor live access export once and audit offline. This is also the bridge from today's code.

Both modes feed the same engine, so results are comparable.

---

## 7. CLI additions

```bash
Davestigator connect add --kind sql --name prod_pg --url-env PROD_PG_URL
Davestigator connect test prod_pg            # connectivity + read-only check (exit 4 if writable)
Davestigator connect discover prod_pg        # schema + PII candidate columns
Davestigator connect map prod_pg --draft     # writes mappings.draft.yaml (human review)
Davestigator connect snapshot prod_pg --out mydata/evidence/
Davestigator sources list                    # now shows live connectors + coverage
Davestigator audit --email someone@example.com --live   # ad-hoc single subject
```

---

## 8. Build order

| Step | Deliverable | Why first |
|---|---|---|
| C1 | `Connector` protocol, registry, generalized resource ids, mapping schema (Pydantic) | Everything else plugs into this |
| C2 | SQL family via SQLAlchemy (Postgres, MySQL, SQL Server, SQLite) + read-only enforcement | Covers most deployments |
| C3 | `connect snapshot` → existing SQL snapshot format | Works with the current engine immediately |
| C4 | Coverage reporting + "no COMPLETE without FULL coverage" rule | Prevents false clean results |
| C5 | Discovery + deterministic PII column detection | Makes mapping practical |
| C6 | Document (MongoDB) + search (Elasticsearch/OpenSearch) | Most common non-SQL stores |
| C7 | Warehouse cost guards (BigQuery/Snowflake), object storage (S3), Redis/DynamoDB | Exports, caches, analytics copies |
| C8 | LLM-assisted mapping draft (schema only) + review gate | Scale to hundreds of tables |
| C9 | Logs/streams and SaaS adapters | Long tail |

**Testing:** each family gets an integration test against a disposable container (Testcontainers:
postgres, mysql, mongo, elasticsearch, redis, minio) seeded with fictional people. That includes a
replica-drift case, a soft-deleted row, and a look-alike email decoy, mirroring the existing custom
evaluation cases.

---

## 9. What stays the same

Identity resolution, the link window, state at audit time, `deletion_rules.csv` policy, contract
ordering, the validator and the LLM guard are unchanged. Connectors are only a new way of
producing `Observation`s. This is why the deterministic core was kept separate from parsing.
