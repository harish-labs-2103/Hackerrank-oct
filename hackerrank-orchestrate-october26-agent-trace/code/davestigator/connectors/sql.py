"""SQL family (SQLAlchemy): PostgreSQL, MySQL/MariaDB, SQL Server, Oracle, SQLite, and warehouses
with a SQLAlchemy dialect (Snowflake, BigQuery, Redshift, ClickHouse, Databricks).

Safety:
  * Table and column names are only taken from reflection, so the mapping can never inject SQL.
  * Values are always bound parameters.
  * Sessions are forced read-only where the dialect supports it, and `test()` refuses roles
    that hold write grants.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from .base import (COVERAGE_FULL, COVERAGE_NOT_CHECKED, COVERAGE_PARTIAL, CollectionInfo, ColumnInfo, ConnectorError,
                   CoverageResult, DriverMissing, NotReadOnly, RawRecord, ReadOnlyReport)

_WRITE_PRIVS = {"INSERT", "UPDATE", "DELETE", "TRUNCATE", "ALTER", "DROP", "CREATE", "ALL PRIVILEGES", "ALL"}


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0)


class SQLConnector:
    kind = "sql"

    def __init__(self, name: str, url: str, options: dict | None = None):
        try:
            import sqlalchemy as sa
        except ImportError as e:  # pragma: no cover - environment dependent
            raise DriverMissing("SQL", "sql") from e
        self.sa = sa
        self.name = name
        self.options = {"statement_timeout_s": 15, "row_limit": 500, **(options or {})}
        url = self._read_only_url(url)
        try:
            self.engine = sa.create_engine(url, pool_pre_ping=True)
        except ModuleNotFoundError as e:
            raise DriverMissing(url.split(":", 1)[0], url.split("+", 1)[0].split(":", 1)[0]) from e
        self.dialect = self.engine.dialect.name
        self._tables: dict[str, object] = {}
        sa.event.listen(self.engine, "connect", self._on_connect)

    # ---------------------------------------------------------------- #
    @staticmethod
    def _read_only_url(url: str) -> str:
        # SQLite: open the file itself read-only.
        m = re.match(r"^sqlite(?:\+pysqlite)?:///(?!file:)(.+)$", url)
        if m and m.group(1) != ":memory:":
            return f"sqlite:///file:{m.group(1)}?mode=ro&uri=true"
        return url

    def _on_connect(self, dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        t = int(self.options["statement_timeout_s"])
        try:
            if self.dialect == "postgresql":
                cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
                cur.execute(f"SET statement_timeout = {t * 1000}")
            elif self.dialect in ("mysql", "mariadb"):
                cur.execute("SET SESSION TRANSACTION READ ONLY")
                cur.execute(f"SET SESSION MAX_EXECUTION_TIME = {t * 1000}")
            elif self.dialect == "sqlite":
                cur.execute("PRAGMA query_only = ON")
                dbapi_conn.create_function("di_digits", 1, lambda s: re.sub(r"\D", "", s) if s else s)
        finally:
            cur.close()
        if self.dialect in ("postgresql", "mysql", "mariadb"):
            dbapi_conn.commit()

    # ---------------------------------------------------------------- #
    def test(self) -> ReadOnlyReport:
        sa = self.sa
        try:
            with self.engine.connect() as c:
                server = f"{self.dialect} {'.'.join(map(str, self.engine.dialect.server_version_info or ()))}".strip()
                problems = self._write_grants(c)
        except NotReadOnly:
            raise
        except sa.exc.SQLAlchemyError as e:
            raise ConnectorError(f"{self.name}: cannot connect ({type(e).__name__})") from e
        if problems:
            raise NotReadOnly("; ".join(problems[:5]) + (" …" if len(problems) > 5 else ""))
        return ReadOnlyReport(True, "no write grants; session forced read-only", server)

    def _write_grants(self, c) -> list[str]:
        sa = self.sa
        if self.dialect == "sqlite":
            try:
                c.exec_driver_sql("CREATE TABLE __di_probe(x)")
            except sa.exc.SQLAlchemyError:
                return []
            return ["database file is writable"]
        if self.dialect == "postgresql":
            out = []
            if c.exec_driver_sql("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").scalar():
                out.append("role is a superuser")
            rows = c.exec_driver_sql(
                "SELECT table_schema||'.'||table_name, privilege_type FROM information_schema.role_table_grants "
                "WHERE grantee = current_user AND privilege_type IN ('INSERT','UPDATE','DELETE','TRUNCATE') "
                "AND table_schema NOT IN ('pg_catalog','information_schema') LIMIT 20").fetchall()
            out += [f"can {p} {t}" for t, p in rows]
            owned = c.exec_driver_sql(
                "SELECT count(*) FROM pg_tables WHERE tableowner = current_user "
                "AND schemaname NOT IN ('pg_catalog','information_schema')").scalar()
            if owned:
                out.append(f"owns {owned} table(s)")
            return out
        if self.dialect in ("mysql", "mariadb"):
            grants = " ".join(r[0] for r in c.exec_driver_sql("SHOW GRANTS").fetchall()).upper()
            return [f"grant includes {p}" for p in sorted(_WRITE_PRIVS) if re.search(rf"\b{p}\b", grants)]
        return [f"cannot verify read-only access for dialect {self.dialect}; use a dialect with grant inspection"]

    # ---------------------------------------------------------------- #
    def _table(self, source: str):
        if source in self._tables:
            return self._tables[source]
        sa = self.sa
        schema, _, name = source.rpartition(".")
        insp = sa.inspect(self.engine)
        names = insp.get_table_names(schema=schema or None) + insp.get_view_names(schema=schema or None)
        if name not in names:
            raise ConnectorError(f"{self.name}: {source} not found; mapping is stale")
        t = sa.Table(name, sa.MetaData(), schema=schema or None, autoload_with=self.engine)
        self._tables[source] = t
        return t

    def discover(self) -> list[CollectionInfo]:
        sa = self.sa
        insp = sa.inspect(self.engine)
        schemas = insp.get_schema_names()
        skip = {"information_schema", "pg_catalog", "mysql", "performance_schema", "sys", "pg_toast"}
        out = []
        for schema in schemas:
            if schema in skip or schema.startswith("pg_temp") or schema.startswith("pg_toast"):
                continue
            prefix = "" if self.dialect == "sqlite" and schema == "main" else f"{schema}."
            for name in insp.get_table_names(schema=schema):
                if name.startswith("__di_") or name.startswith("sqlite_"):
                    continue
                source = f"{prefix}{name}"
                try:
                    cols = [ColumnInfo(c["name"], str(c["type"]), bool(c.get("nullable", True)))
                            for c in insp.get_columns(name, schema=schema)]
                    pk = insp.get_pk_constraint(name, schema=schema).get("constrained_columns") or []
                    out.append(CollectionInfo(source, cols, self._count(source), pk))
                except sa.exc.SQLAlchemyError as e:
                    out.append(CollectionInfo(source, [], None, [], readable=False, note=type(e).__name__))
        return out

    def _count(self, source: str) -> int | None:
        sa = self.sa
        try:
            with self.engine.connect() as c:
                if self.dialect == "postgresql":
                    schema, _, name = source.rpartition(".")
                    est = c.execute(sa.text("SELECT reltuples::bigint FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                                            "WHERE n.nspname = :s AND c.relname = :t"), {"s": schema or "public", "t": name}).scalar()
                    if est is not None and est >= 0:
                        return int(est)
                t = self._table(source)
                return int(c.execute(sa.select(sa.func.count()).select_from(t)).scalar())
        except sa.exc.SQLAlchemyError:
            return None

    def sample(self, source: str, column: str, n: int) -> list:
        sa = self.sa
        t = self._table(source)
        if column not in t.c:
            return []
        with self.engine.connect() as c:
            return [r[0] for r in c.execute(sa.select(t.c[column]).where(t.c[column].is_not(None)).limit(n))]

    # ---------------------------------------------------------------- #
    def _match(self, col, kind: str, vals: list[str]):
        sa = self.sa
        if kind == "email":
            return sa.func.lower(col).in_([v.lower() for v in vals])
        if kind == "phone":
            if self.dialect == "postgresql":
                return sa.func.regexp_replace(col, r"\D", "", "g").in_(vals)
            if self.dialect in ("mysql", "mariadb"):
                return sa.func.regexp_replace(col, "[^0-9]", "").in_(vals)
            if self.dialect == "sqlite":
                return sa.func.di_digits(col).in_(vals)
            return col.in_(vals)
        return col.in_(vals)

    def find(self, cmap, values: dict[str, set[str]], limit: int | None = None) -> tuple[list[RawRecord], CoverageResult]:
        sa = self.sa
        limit = limit or int(self.options["row_limit"])
        try:
            t = self._table(cmap.source)
        except ConnectorError as e:
            return [], CoverageResult(self.name, cmap.source, COVERAGE_NOT_CHECKED, str(e))
        clauses = []
        for kind, field in cmap.identifiers.items():
            if field not in t.c or not values.get(kind):
                continue
            clauses.append(self._match(t.c[field], kind, sorted(values[kind])))
        for field in cmap.text_fields:
            if field in t.c:
                for e in sorted(values.get("email", ())):
                    clauses.append(sa.func.lower(t.c[field]).contains(e.lower(), autoescape=True))
        if not clauses:
            return [], CoverageResult(self.name, cmap.source, COVERAGE_FULL, "no identifier of this subject applies")
        q = sa.select(t).where(sa.or_(*clauses)).limit(limit + 1)
        try:
            with self.engine.connect() as c:
                rows = [dict(r._mapping) for r in c.execute(q)]
        except sa.exc.OperationalError as e:
            return [], CoverageResult(self.name, cmap.source, COVERAGE_NOT_CHECKED, f"query failed ({type(e.orig).__name__})")
        at = _now()
        status = COVERAGE_FULL if len(rows) <= limit else COVERAGE_PARTIAL
        rows = rows[:limit]
        recs = [RawRecord(str(r.get(cmap.id)), {k: _plain(v) for k, v in r.items()}, at) for r in rows]
        return recs, CoverageResult(self.name, cmap.source, status, "" if status == COVERAGE_FULL else f"row limit {limit} reached", len(recs))

    def close(self):
        self.engine.dispose()


def _plain(v):
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray, memoryview)):
        return None
    return v
