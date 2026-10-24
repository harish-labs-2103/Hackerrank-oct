-- Ghost Data Still Exists: point-in-time database snapshot
-- Dialect: SQLite-compatible SQL
-- Captured at: 2026-09-20T10:00:00Z
-- Database topology: read_replica

BEGIN TRANSACTION;
CREATE TABLE customer_data_primary (resource_id TEXT PRIMARY KEY, origin_system TEXT NOT NULL, collection_name TEXT NOT NULL, record_json TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE customer_data_read_replica (resource_id TEXT PRIMARY KEY, origin_system TEXT NOT NULL, collection_name TEXT NOT NULL, record_json TEXT NOT NULL, updated_at TEXT NOT NULL);
INSERT INTO "customer_data_read_replica" VALUES('account_70001','account_service','customer_profiles','{"resource_id": "account_70001", "customer_reference": "user_9001", "full_name": "Tove Lindqvist", "email": "tove.lindqvist@example.org", "phone": "+46 70 111 2233", "account_status": "ACTIVE"}','2026-09-20T10:00:00Z');
COMMIT;
