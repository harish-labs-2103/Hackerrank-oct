"""Non-SQL families: search indexes (Elasticsearch/OpenSearch REST), document stores (MongoDB),
key-value (Redis), object storage (S3-compatible).

Each uses only read APIs. Queries are exact `term`/equality lookups on mapped fields — never
full-text matching, which would pull in look-alike identifiers (e.g. `name.jr@` for `name@`).
"""
from __future__ import annotations

import fnmatch
from datetime import datetime, timezone

import httpx

from .base import (COVERAGE_FULL, COVERAGE_NOT_CHECKED, COVERAGE_PARTIAL, CollectionInfo, ColumnInfo, ConnectorError,
                   CoverageResult, DriverMissing, NotReadOnly, RawRecord, ReadOnlyReport, flatten_path)


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0)


# ------------------------------------------------------------------------ #
class SearchConnector:
    """Elasticsearch / OpenSearch over the REST API (no client library needed)."""

    kind = "search"

    def __init__(self, name: str, url: str, options: dict | None = None, transport=None):
        self.name = name
        self.options = {"row_limit": 500, "timeout_s": 15, **(options or {})}
        self.http = httpx.Client(base_url=url.rstrip("/"), timeout=self.options["timeout_s"], transport=transport)

    def _get(self, path, **kw):
        try:
            r = self.http.get(path, **kw)
        except httpx.HTTPError as e:
            raise ConnectorError(f"{self.name}: unreachable ({type(e).__name__})") from e
        if r.status_code >= 400:
            raise ConnectorError(f"{self.name}: {path} returned HTTP {r.status_code}")
        return r.json()

    def test(self) -> ReadOnlyReport:
        info = self._get("/")
        version = info.get("version", {}).get("number", "?")
        # Security API (if enabled) tells us whether this user holds write/admin privileges.
        try:
            r = self.http.get("/_security/_authenticate")
            if r.status_code == 200:
                roles = set(r.json().get("roles", []))
                if roles & {"superuser", "admin", "all_access", "kibana_admin", "editor"}:
                    raise NotReadOnly(f"user has write/admin roles {sorted(roles)}")
                return ReadOnlyReport(True, f"roles {sorted(roles)} (no write roles)", f"search {version}")
        except httpx.HTTPError:
            pass
        return ReadOnlyReport(True, "read APIs only are used (security API not enabled: grant a read-only role)", f"search {version}")

    def discover(self) -> list[CollectionInfo]:
        out = []
        for idx in self._get("/_cat/indices", params={"format": "json"}):
            name = idx.get("index", "")
            if name.startswith("."):
                continue
            props = self._get(f"/{name}/_mapping").get(name, {}).get("mappings", {}).get("properties", {})
            cols = []

            def walk(p, prefix=""):
                for k, v in p.items():
                    if "properties" in v:
                        walk(v["properties"], f"{prefix}{k}.")
                    else:
                        cols.append(ColumnInfo(f"{prefix}{k}", v.get("type", "?")))
            walk(props)
            out.append(CollectionInfo(name, cols, int(idx.get("docs.count") or 0), ["_id"]))
        return out

    def sample(self, source, column, n):
        res = self._get(f"/{source}/_search", params={"size": n, "_source": column})
        return [flatten_path(h.get("_source", {}), column) for h in res.get("hits", {}).get("hits", [])]

    def find(self, cmap, values, limit=None):
        limit = limit or int(self.options["row_limit"])
        should = []
        for kind, field in cmap.identifiers.items():
            for v in sorted(values.get(kind, ())):
                should.append({"term": {field: v}})
                if kind == "email":
                    should.append({"term": {f"{field}.keyword": v}})
        if not should:
            return [], CoverageResult(self.name, cmap.source, COVERAGE_FULL, "no identifier of this subject applies")
        try:
            r = self.http.post(f"/{cmap.source}/_search", json={"size": limit + 1, "query": {"bool": {"should": should, "minimum_should_match": 1}}})
        except httpx.HTTPError as e:
            return [], CoverageResult(self.name, cmap.source, COVERAGE_NOT_CHECKED, f"unreachable ({type(e).__name__})")
        if r.status_code >= 400:
            return [], CoverageResult(self.name, cmap.source, COVERAGE_NOT_CHECKED, f"HTTP {r.status_code}")
        hits = r.json().get("hits", {}).get("hits", [])
        at = _now()
        status = COVERAGE_FULL if len(hits) <= limit else COVERAGE_PARTIAL
        recs = [RawRecord(str(h.get("_id")), {**h.get("_source", {}), "_id": h.get("_id")}, at) for h in hits[:limit]]
        return recs, CoverageResult(self.name, cmap.source, status, "" if status == COVERAGE_FULL else f"row limit {limit}", len(recs))

    def close(self):
        self.http.close()


# ------------------------------------------------------------------------ #
class MongoConnector:
    kind = "document"

    def __init__(self, name: str, url: str, options: dict | None = None):
        try:
            import pymongo
        except ImportError as e:
            raise DriverMissing("MongoDB", "mongo") from e
        self.name = name
        self.options = {"row_limit": 500, "timeout_s": 15, **(options or {})}
        self.client = pymongo.MongoClient(url, serverSelectionTimeoutMS=self.options["timeout_s"] * 1000,
                                          readPreference="secondaryPreferred")
        self.db = self.client.get_default_database()

    def test(self) -> ReadOnlyReport:
        status = self.db.command("connectionStatus", showPrivileges=True)
        acts = {a for p in status.get("authInfo", {}).get("authenticatedUserPrivileges", []) for a in p.get("actions", [])}
        bad = acts & {"insert", "update", "remove", "dropCollection", "createCollection", "dropDatabase"}
        if bad:
            raise NotReadOnly(f"user can {sorted(bad)}")
        return ReadOnlyReport(True, "no write actions granted", "mongodb")

    def discover(self):
        out = []
        for name in self.db.list_collection_names():
            doc = self.db[name].find_one() or {}
            out.append(CollectionInfo(name, [ColumnInfo(k, type(v).__name__) for k, v in doc.items()],
                                      self.db[name].estimated_document_count(), ["_id"]))
        return out

    def sample(self, source, column, n):
        return [flatten_path(d, column) for d in self.db[source].find({column: {"$ne": None}}, {column: 1}).limit(n)]

    def find(self, cmap, values, limit=None):
        limit = limit or int(self.options["row_limit"])
        ors = [{field: {"$in": sorted(values[kind])}} for kind, field in cmap.identifiers.items() if values.get(kind)]
        if not ors:
            return [], CoverageResult(self.name, cmap.source, COVERAGE_FULL, "no identifier of this subject applies")
        docs = list(self.db[cmap.source].find({"$or": ors}).limit(limit + 1))
        at = _now()
        status = COVERAGE_FULL if len(docs) <= limit else COVERAGE_PARTIAL
        recs = [RawRecord(str(d.get(cmap.id, d.get("_id"))), {k: (str(v) if k == "_id" else v) for k, v in d.items()}, at)
                for d in docs[:limit]]
        return recs, CoverageResult(self.name, cmap.source, status, "", len(recs))

    def close(self):
        self.client.close()


# ------------------------------------------------------------------------ #
class RedisConnector:
    """Key-value caches. `source` is a key pattern with placeholders, e.g. `profile:{user_ref}`."""

    kind = "kv"

    def __init__(self, name: str, url: str, options: dict | None = None):
        try:
            import redis
        except ImportError as e:
            raise DriverMissing("Redis", "redis") from e
        self.name = name
        self.options = {"row_limit": 500, "timeout_s": 5, **(options or {})}
        self.r = redis.Redis.from_url(url, socket_timeout=self.options["timeout_s"], decode_responses=True)

    def test(self) -> ReadOnlyReport:
        try:
            info = self.r.info("server")
        except Exception as e:  # noqa: BLE001
            raise ConnectorError(f"{self.name}: unreachable ({type(e).__name__})") from e
        try:
            perms = self.r.execute_command("ACL", "WHOAMI")
            rules = " ".join(self.r.execute_command("ACL", "GETUSER", perms) or [])
            if "+@all" in rules or "+@write" in rules or "+set" in rules:
                raise NotReadOnly(f"user {perms} has write commands; use an ACL user with +@read only")
        except NotReadOnly:
            raise
        except Exception:  # noqa: BLE001 - ACL not available (Redis < 6)
            raise NotReadOnly("cannot verify read-only ACL (Redis < 6 or ACL disabled)")
        return ReadOnlyReport(True, "ACL user limited to read commands", f"redis {info.get('redis_version', '?')}")

    def discover(self):
        prefixes: dict[str, int] = {}
        for i, k in enumerate(self.r.scan_iter(count=500)):
            if i >= 5000:
                break
            p = k.split(":")[0] + ":*"
            prefixes[p] = prefixes.get(p, 0) + 1
        return [CollectionInfo(p, [ColumnInfo("value", "string")], n, ["key"]) for p, n in sorted(prefixes.items())]

    def sample(self, source, column, n):
        return [k for _, k in zip(range(n), self.r.scan_iter(match=source))]

    def find(self, cmap, values, limit=None):
        at = _now()
        recs = []
        for kind, vals in values.items():
            if "{" + kind + "}" not in cmap.source:
                continue
            for v in sorted(vals):
                key = cmap.source.replace("{" + kind + "}", v)
                for k in self.r.scan_iter(match=key) if any(c in key for c in "*?[") else [key]:
                    t = self.r.type(k)
                    if t == "none":
                        continue
                    val = self.r.hgetall(k) if t == "hash" else {"value": self.r.get(k)} if t == "string" else {}
                    recs.append(RawRecord(k, {"key": k, **val, cmap.identifiers.get(kind, kind): v}, at))
        return recs, CoverageResult(self.name, cmap.source, COVERAGE_FULL, "", len(recs))

    def close(self):
        self.r.close()


# ------------------------------------------------------------------------ #
class S3Connector:
    """Object storage. `source` is `bucket/prefix-with-{user_ref}` ; objects are listed, never read."""

    kind = "object"

    def __init__(self, name: str, url: str, options: dict | None = None):
        try:
            import boto3
        except ImportError as e:
            raise DriverMissing("S3", "s3") from e
        self.name = name
        self.options = {"row_limit": 500, **(options or {})}
        endpoint = None if url in ("", "s3://", "aws") else url
        self.s3 = boto3.client("s3", endpoint_url=endpoint)

    def test(self) -> ReadOnlyReport:
        # S3 has no portable "am I read-only" call; IAM policy simulation needs extra permissions.
        if self.options.get("read_only_policy_verified") is True:
            self.s3.list_buckets()
            return ReadOnlyReport(True, "read-only IAM policy attested by operator (s3:List*/s3:Get* only)", "s3")
        raise NotReadOnly("cannot verify S3 credentials are read-only; attach a policy with s3:List*/s3:Get* only "
                          "and set options.read_only_policy_verified: true in davestigator.yaml")

    def discover(self):
        return [CollectionInfo(b["Name"], [ColumnInfo("key", "string")], None, ["key"]) for b in self.s3.list_buckets().get("Buckets", [])]

    def sample(self, source, column, n):
        bucket, _, prefix = source.partition("/")
        return [o["Key"] for o in self.s3.list_objects_v2(Bucket=bucket, Prefix=prefix.split("{")[0], MaxKeys=n).get("Contents", [])]

    def find(self, cmap, values, limit=None):
        bucket, _, pattern = cmap.source.partition("/")
        at = _now()
        recs = []
        for kind, vals in values.items():
            if "{" + kind + "}" not in pattern:
                continue
            for v in sorted(vals):
                prefix = pattern.replace("{" + kind + "}", v)
                for o in self.s3.list_objects_v2(Bucket=bucket, Prefix=prefix.split("*")[0]).get("Contents", []):
                    if fnmatch.fnmatch(o["Key"], prefix + "*"):
                        recs.append(RawRecord(o["Key"], {"key": o["Key"], cmap.identifiers.get(kind, kind): v,
                                                         "size": o.get("Size")}, at))
        return recs, CoverageResult(self.name, cmap.source, COVERAGE_FULL, "", len(recs))

    def close(self):
        pass
