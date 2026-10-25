"""Connector registry: kind -> driver family. Products within a family are configured, not coded."""
from __future__ import annotations

import os

from .base import ConnectorError, CredentialsMissing, DriverMissing, NotReadOnly

__all__ = ["ConnectorError", "CredentialsMissing", "DriverMissing", "NotReadOnly", "FAMILIES", "open_connector"]

FAMILIES = {
    "sql": "relational and warehouse databases via SQLAlchemy (postgresql, mysql, mssql, oracle, sqlite, snowflake, bigquery, redshift, clickhouse …)",
    "search": "Elasticsearch / OpenSearch over REST",
    "document": "MongoDB (and wire-compatible: DocumentDB, CosmosDB Mongo API)",
    "kv": "Redis key-value caches",
    "object": "S3-compatible object storage (AWS S3, MinIO, GCS interop)",
}
PLANNED = {"log": "Kafka / CloudWatch / Loki / Splunk (planned)", "api": "SaaS adapters: CRM, helpdesk, marketing (planned)"}


def open_connector(cfg):
    """cfg: ConnectorCfg from settings. Credentials come only from the named environment variable."""
    url = os.environ.get(cfg.url_env or "")
    if not url:
        raise CredentialsMissing(f"Environment variable {cfg.url_env} is not set.")
    if cfg.kind == "sql":
        from .sql import SQLConnector

        return SQLConnector(cfg.name, url, cfg.options)
    if cfg.kind == "search":
        from .nosql import SearchConnector

        return SearchConnector(cfg.name, url, cfg.options)
    if cfg.kind == "document":
        from .nosql import MongoConnector

        return MongoConnector(cfg.name, url, cfg.options)
    if cfg.kind == "kv":
        from .nosql import RedisConnector

        return RedisConnector(cfg.name, url, cfg.options)
    if cfg.kind == "object":
        from .nosql import S3Connector

        return S3Connector(cfg.name, url, cfg.options)
    if cfg.kind in PLANNED:
        raise ConnectorError(f"kind '{cfg.kind}' is planned but not implemented yet: {PLANNED[cfg.kind]}")
    raise ConnectorError(f"unknown kind '{cfg.kind}'; supported: {', '.join(FAMILIES)}")
