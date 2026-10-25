"""Format-specific evidence parsers. Each returns observations plus coverage metadata.

Parsers never decide relevance or policy; they only extract what a source says about a
resource at a point in time. Evidence text is treated as data, never as instructions.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..models import ManifestEntry, Observation
from ..normalize import RESOURCE_RE, classify_value, parse_ts
from .interpret import interpret_record, system_for


@dataclass
class Coverage:
    """A snapshot that enumerates a collection: resources absent from it were not in that copy."""

    source_id: str
    captured_at: datetime
    system_id: str
    category_hint: str | None
    present: set[str]
    copy: str  # table / collection label
    authoritative: bool = False  # snapshot declares it captured this system in full


@dataclass
class ParseResult:
    observations: list[Observation] = field(default_factory=list)
    coverage: list[Coverage] = field(default_factory=list)
    links: list[tuple[str, tuple[str, str], datetime]] = field(default_factory=list)  # (resource, ident, at)
    errors: list[str] = field(default_factory=list)
    text: str = ""


# --------------------------------------------------------------------------- #
# SQL snapshots
# --------------------------------------------------------------------------- #
_SQL_CAPTURED = re.compile(r"Captured at:\s*(\S+)")


def parse_sql(path: Path, entry: ManifestEntry) -> ParseResult:
    res = ParseResult()
    script = path.read_text(encoding="utf-8")
    m = _SQL_CAPTURED.search(script)
    file_captured = parse_ts(m.group(1)) if m else None
    snap_at = file_captured or entry.captured_at
    con = sqlite3.connect(":memory:")
    try:
        con.executescript(script)
    except sqlite3.Error as e:  # malformed snapshot: report, keep going
        res.errors.append(f"{entry.file_id}: sqlite error {e}")
        return res
    con.row_factory = sqlite3.Row
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    scope: dict[str, datetime] = {}
    if "snapshot_scope" in tables:
        for r in con.execute("SELECT origin_system, captured_at FROM snapshot_scope"):
            scope[r[0]] = parse_ts(r[1]) or snap_at
    for t in tables:
        cols = [c[1] for c in con.execute(f'PRAGMA table_info("{t}")')]
        if "resource_id" not in cols:
            continue
        per_system: dict[str, set[str]] = defaultdict(set)
        for row in con.execute(f'SELECT * FROM "{t}"'):
            rec = dict(row)
            hints = {"collection": rec.get("collection_name") or t}
            if "record_json" in rec:
                try:
                    inner = json.loads(rec["record_json"])
                except json.JSONDecodeError:
                    inner = {}
                rec = {**{k: v for k, v in rec.items() if k != "record_json"}, **_flatten(inner)}
            rid = rec["resource_id"]
            system = system_for(rid) or rec.get("origin_system")
            at = scope.get(system, snap_at)
            obs = interpret_record(
                fields=rec, resource_id=rid, source_id=entry.file_id, observed_at=at,
                captured_at=entry.captured_at, system_hint=rec.get("origin_system"),
                locator=f"{t}", hints=hints,
            )
            if obs:
                res.observations.append(obs)
                per_system[obs.system_id].add(rid)
        # Coverage: which systems this table enumerates in full at snapshot time.
        #  - inventory snapshots declare their systems in snapshot_scope (authoritative);
        #  - single-system tables (customer_profiles_primary, search_documents_primary...) cover that system;
        #  - mixed record_json tables are partial captures and only cover rows they contain.
        for s in set(per_system) | {sys_ for sys_ in scope if _table_matches_system(t, sys_)}:
            if scope and s not in scope:
                continue
            dedicated = _table_matches_system(t, s) or (len(per_system) == 1 and "record_json" not in cols)
            res.coverage.append(Coverage(entry.file_id, scope.get(s, snap_at), s, None, per_system.get(s, set()), t,
                                         authoritative=bool(scope and s in scope and dedicated) or (not scope and dedicated)))
    con.close()
    return res


_TABLE_SYSTEM = {
    "customer_profiles": "account_service",
    "customer_search_documents": "search_service",
    "customer_analytics_profiles": "analytics_platform",
    "support_records": "support_platform",
    "customer_exports": "export_service",
    "connector_subscriptions": "integration_service",
    "backup_restore_entries": "backup_service",
    "security_audit_records": "security_service",
}


def _table_matches_system(table: str, system: str) -> bool:
    if _TABLE_SYSTEM.get(table) == system:
        return True
    # replica-suffixed single-collection tables, e.g. customer_profiles_failover_replica
    return any(table.startswith(base) and sysn == system for base, sysn in _TABLE_SYSTEM.items()) or \
        (table.startswith("search_documents") and system == "search_service")


def _flatten(obj: dict, prefix: str = "") -> dict:
    """Flatten nested dicts (e.g. search `document`) into one field namespace."""
    out: dict = {}
    for k, v in obj.items():
        if isinstance(v, dict):
            for k2, v2 in _flatten(v).items():
                out.setdefault(k2, v2)
        else:
            out[k] = v
    return out


# --------------------------------------------------------------------------- #
# JSON records
# --------------------------------------------------------------------------- #
_CONTEXT_KEYS = ("data_store", "collection", "system", "captured_at", "exported_at", "observed_at")


def parse_json(path: Path, entry: ManifestEntry) -> ParseResult:
    res = ParseResult()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        res.errors.append(f"{entry.file_id}: invalid JSON {e}")
        return res
    top_at = parse_ts(data.get("captured_at") or data.get("exported_at")) or entry.captured_at

    def walk(o, ctx: dict, path: str):
        if isinstance(o, dict):
            if "resource_id" in o and isinstance(o["resource_id"], str) and RESOURCE_RE.fullmatch(o["resource_id"]):
                emit(o, ctx, path)
                return
            ctx2 = dict(ctx)
            for k in _CONTEXT_KEYS:
                if k in o and not isinstance(o[k], (dict, list)):
                    ctx2[k] = o[k]
            for k, v in o.items():
                if isinstance(v, (dict, list)):
                    c3 = dict(ctx2)
                    # service_exports: {system: {collection: [rows]}}
                    if path.endswith("service_exports") and isinstance(v, dict):
                        c3["data_store"] = k
                    elif isinstance(v, list) and "collection" not in ctx2:
                        c3["collection"] = k
                    walk(v, c3, f"{path}.{k}" if path else k)
        elif isinstance(o, list):
            for i, x in enumerate(o):
                walk(x, ctx, f"{path}[{i}]")

    by_snapshot: dict[tuple, set[str]] = defaultdict(set)

    def emit(rec: dict, ctx: dict, path: str):
        fields = _flatten(rec)
        # search query results: {"query": .., "hits": {"total": n, "documents": [..]}}
        hits = rec.get("hits")
        if isinstance(hits, dict):
            docs = hits.get("documents") or []
            fields = {k: v for k, v in rec.items() if k != "hits"}
            fields["total_hits"] = hits.get("total", len(docs))
            if docs:
                for k, v in _flatten(docs[0]).items():
                    fields.setdefault(k, v)
            fields["result"] = "HIT" if fields["total_hits"] else "MISS"
        at = (
            parse_ts(rec.get("observed_at") or rec.get("recorded_at"))
            or parse_ts(ctx.get("captured_at") or ctx.get("observed_at"))
            or top_at
        )
        rid = rec["resource_id"]
        obs = interpret_record(
            fields=fields, resource_id=rid, source_id=entry.file_id, observed_at=at,
            captured_at=entry.captured_at, system_hint=rec.get("system") or ctx.get("data_store"),
            locator=path, hints={"collection": ctx.get("collection")},
        )
        if obs:
            res.observations.append(obs)
            if ctx.get("collection") and "record_type" not in rec:
                by_snapshot[(obs.system_id, ctx.get("collection"), at)].add(rid)

    walk(data, {}, "")
    for (system, coll, at), rids in by_snapshot.items():
        res.coverage.append(Coverage(entry.file_id, at, system, None, rids, coll))
    return res


# --------------------------------------------------------------------------- #
# Logs
# --------------------------------------------------------------------------- #
_TS_HEAD = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s+(.*)$")
_KV = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)=(?:"((?:[^"\\]|\\.)*)"|(\S+))')
_TRACE = re.compile(r"\b(tr-rp-[\w-]+|tr-\d+|rb-\d+-\d+)\b")
_BRACKET = re.compile(r"^\[(\w+)\]\s+\[([\w.-]+)\]\s+\[([\w-]+)\]\s+(\w+):")
_PIPE = re.compile(r"^\|?\s*(\w+)\s*\|\s*([\w.-]+)\s*\|\s*([\w-]+)\s*\|\s*(\w+)\s*\|")
_SYSLOG = re.compile(r"^<\d+>\d+\s+\S+\s+([\w.-]+)\s+([\w-]+)\s+-\s+-\s+")


def _parse_log_line(line: str) -> tuple[datetime | None, dict]:
    m = _TS_HEAD.match(line.strip())
    if not m:
        return None, {}
    ts = parse_ts(m.group(1))
    body = m.group(2)
    fields: dict = {}
    if body.lstrip().startswith("{"):
        try:
            fields.update(json.loads(body))
        except json.JSONDecodeError:
            pass
    b = _BRACKET.match(body)
    if b:
        fields.update({"level": b.group(1), "service": b.group(2), "trace_id": b.group(3), "event": b.group(4)})
    p = _PIPE.match(body)
    if p:
        fields.update({"level": p.group(1), "service": p.group(2), "trace_id": p.group(3), "event": p.group(4)})
    s = _SYSLOG.match(body)
    if s:
        fields.update({"service": s.group(1), "trace_id": s.group(2)})
    # Cloud-runtime exports wrap key/values in message="k=\"v\" ..."; parse the inner text too.
    inner = re.search(r'message="((?:[^"\\]|\\.)*)"', body)
    unescaped = body.replace('\\"', '"')
    if inner:
        for k, q, bare in _KV.findall(inner.group(1).replace('\\"', '"')):
            fields.setdefault(k, q if q or bare == "" else bare)
    for k, q, bare in _KV.findall(unescaped):
        fields.setdefault(k, q if q or bare == "" else bare)
    if "requestId" in fields:
        fields.setdefault("trace_id", fields["requestId"])
    if "trace_id" not in fields:
        t = _TRACE.search(body)
        if t:
            fields["trace_id"] = t.group(1)
    return ts, fields


_LINK_KEYS = ("lookup_key", "match_key", "correlation_key", "subject_key", "subject_ref", "subject_reference",
              "customer_email", "customer_reference", "query")


def parse_log(path: Path, entry: ManifestEntry) -> ParseResult:
    res = ParseResult()
    parsed = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        ts, f = _parse_log_line(line)
        if ts is None:
            continue
        parsed.append((n, ts, f))
    # Identifiers queried within a trace link to the resource that trace returned.
    trace_ids: dict[str, set] = defaultdict(set)
    for _, _, f in parsed:
        tid = f.get("trace_id")
        if not tid:
            continue
        for k in _LINK_KEYS:
            if k in f:
                i = classify_value(f[k])
                if i:
                    trace_ids[tid].add(i)
    for n, ts, f in parsed:
        rid = f.get("resource_id")
        if not rid or not RESOURCE_RE.fullmatch(rid):
            continue
        extra = frozenset(trace_ids.get(f.get("trace_id"), set()))
        obs = interpret_record(
            fields=f, resource_id=rid, source_id=entry.file_id, observed_at=ts, captured_at=entry.captured_at,
            system_hint=f.get("system") or f.get("service"), extra_identifiers=extra, locator=f"line {n}",
            hints={"event": f.get("event"), "record_type": f.get("record_type")},
        )
        if obs:
            res.observations.append(obs)
    return res


# --------------------------------------------------------------------------- #
# Plain-text handover records: "- ts | system | resource | CATEGORY | RESULT | subject"
# --------------------------------------------------------------------------- #
_PIPE_ROW = re.compile(r"^\s*-?\s*(\S+)\s*\|\s*(\w+)\s*\|\s*(\w+)\s*\|\s*([A-Z_]+)\s*\|\s*([A-Z_]+)\s*\|\s*(.+?)\s*$")


def parse_text(path: Path, entry: ManifestEntry) -> ParseResult:
    res = ParseResult()
    text = path.read_text(encoding="utf-8")
    res.text = text
    for n, line in enumerate(text.splitlines(), 1):
        m = _PIPE_ROW.match(line)
        if not m:
            continue
        ts, system, rid, cat, result, subject = m.groups()
        fields = {"system": system, "resource_id": rid, "record_type": cat, "result": result, "subject_reference": subject}
        obs = interpret_record(
            fields=fields, resource_id=rid, source_id=entry.file_id, observed_at=parse_ts(ts) or entry.captured_at,
            captured_at=entry.captured_at, system_hint=system, locator=f"line {n}",
        )
        if obs:
            res.observations.append(obs)
    return res


# --------------------------------------------------------------------------- #
# PDFs: text layer only; facts are kept as text for the artifact extractor.
# --------------------------------------------------------------------------- #
def parse_pdf(path: Path, entry: ManifestEntry) -> ParseResult:
    res = ParseResult()
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        res.text = "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception as e:  # noqa: BLE001 - any parser failure is reported, not fatal
        res.errors.append(f"{entry.file_id}: pdf extraction failed: {e}")
    return res
