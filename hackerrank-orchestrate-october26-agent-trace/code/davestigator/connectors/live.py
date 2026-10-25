"""Live audits and snapshot export over configured connectors.

Live:     audit_at = now; records are fetched from each approved collection, turned into
          Observations (source_kind="live"), and fed to the unchanged Investigator.
Snapshot: the same fetch, written as a SQLite-compatible snapshot folder that the dataset
          pipeline reads offline.

Rule: a case is never COMPLETE while any approved evidence collection was PARTIAL or NOT_CHECKED.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..engine import CaseResult, Investigator
from ..evidence.registry import EvidenceIndex
from ..ingestion import Dataset, _read
from ..intent import IntentClassifier
from ..models import DeletionRequest, Observation, ResolvedIdentity, Rule
from ..normalize import EMAIL_RE, email_hash, ident, is_null
from . import open_connector
from .base import COVERAGE_FULL, ConnectorError, CoverageResult, RawRecord, flatten_path
from .mapping import CollectionMap, MappingFile

PHONE_TEXT = re.compile(r"\+\d[\d ()\-]{7,}\d")


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class LiveRun:
    run_id: str
    started_at: datetime
    journal: list[dict] = field(default_factory=list)
    coverage: list[CoverageResult] = field(default_factory=list)
    evidence_times: dict[str, datetime] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


class LiveAuditor:
    def __init__(self, settings, mapping: MappingFile, mapping_sha: str, rules: list[Rule],
                 classifier: IntentClassifier | None = None):
        self.settings = settings
        self.mapping = mapping
        self.mapping_sha = mapping_sha
        self.rules = rules
        self.classifier = classifier or IntentClassifier()
        self.connectors = {}
        self.open_errors: dict[str, str] = {}
        cfgs = {c.name: c for c in settings.connectors}
        for name, cm in mapping.connectors.items():
            if not any(c.review == "approved" for c in cm.collections):
                continue
            if name not in cfgs:
                self.open_errors[name] = "mapped but not configured in davestigator.yaml"
                continue
            try:
                self.connectors[name] = open_connector(cfgs[name])
            except ConnectorError as e:
                self.open_errors[name] = str(e)

    def approved(self, role: str):
        """role="identity": identity sources. role="evidence": evidence collections plus identity
        collections that also carry a policy category (e.g. the users table is both)."""
        for name, cm in self.mapping.connectors.items():
            for c in cm.collections:
                if c.review != "approved":
                    continue
                if c.role == role or (role == "evidence" and c.role == "identity" and c.category != "IDENTITY_SOURCE"):
                    yield name, c

    # ------------------------------------------------------------------ #
    def resolve(self, email: str, run: LiveRun) -> ResolvedIdentity:
        """Identity from approved `role: identity` collections, expanded once through the stable
        reference (so older emails/phones recorded against the same user are included)."""
        e = ident("email", email)
        ids: set[tuple[str, str]] = {e} if e else set()
        if e:
            ids.add(("email_hash", email_hash(e[1])))
        user_ids: set[str] = set()
        for _round in range(2):
            values = _values(ids)
            for name, c in self.approved("identity"):
                conn = self.connectors.get(name)
                if conn is None:
                    continue
                recs, cov = conn.find(c, values)
                run.coverage.append(cov)
                for r in recs:
                    ids |= _record_identifiers(c, r.fields)
                    ref = _field(c, r.fields, "user_ref")
                    if ref is not None:
                        user_ids.add(str(ref))
        res = ResolvedIdentity(sorted(user_ids)[0] if user_ids else None, {i for i in ids if i[0] != "name"},
                               path=["live identity collections" if user_ids else "request email only (no identity collection matched)"])
        if len(user_ids) > 1:
            res.ambiguities.append(f"email maps to multiple users {sorted(user_ids)}")
        if res.user_id is None:
            res.user_id = f"unresolved:{hashlib.sha256((e[1] if e else email).encode()).hexdigest()[:10]}"
            res.ambiguities.append("no identity record matched; searched with the request email only")
        return res

    def collect(self, identity: ResolvedIdentity, run: LiveRun) -> EvidenceIndex:
        idx = EvidenceIndex()
        values = _values(identity.identifiers)
        for name in self.open_errors:
            for _, c in [(n, c) for n, c in self.approved("evidence") if n == name]:
                run.coverage.append(CoverageResult(name, c.source, "NOT_CHECKED", self.open_errors[name]))
        for name, c in self.approved("evidence"):
            conn = self.connectors.get(name)
            if conn is None:
                continue
            try:
                recs, cov = conn.find(c, values)
            except ConnectorError as e:
                recs, cov = [], CoverageResult(name, c.source, "NOT_CHECKED", str(e))
            run.coverage.append(cov)
            for r in recs:
                obs = to_observation(name, c, r)
                idx.add(obs)
                run.evidence_times[obs.source_id] = obs.observed_at
                run.journal.append({"evidence_id": obs.source_id, "connector": name, "collection": c.source,
                                    "record_id": r.record_id, "observed_at": _ts(r.observed_at), "system_id": c.system,
                                    "category": c.category, "state": obs.state,
                                    "identifier_kinds": sorted({k for k, _ in obs.identifiers}),
                                    "exposed_kinds": sorted(obs.pii), "mapping_sha256": self.mapping_sha})
        return idx

    # ------------------------------------------------------------------ #
    def audit(self, req: DeletionRequest, run: LiveRun) -> tuple[CaseResult, list[CoverageResult]]:
        # A live audit evaluates the state *now*; an earlier audit_at from a request file would
        # exclude every observation just read.
        from dataclasses import replace

        before = len(run.coverage)
        identity = self.resolve(req.email, run)
        idx = self.collect(identity, run)
        # audit_at is fixed once every read has finished, so all live observations are included
        now = _now()
        if req.audit_at < now:
            req = replace(req, audit_at=now)
        ds = Dataset(root=Path("."), identities=[], audit_log=[], activities=[], rules=self.rules, manifest={})
        inv = Investigator(ds, idx, self.classifier, resolver=lambda _email: identity)
        result = inv.investigate(req)
        cov = list({(c.connector, c.source, c.status): c for c in run.coverage[before:]}.values())
        gaps = [c for c in cov if c.status != COVERAGE_FULL]
        if result.deletion_status != "NOT_APPLICABLE" and gaps:
            for g in gaps:
                result.gaps.append(f"{g.connector}:{g.source} {g.status} ({g.reason})")
            if result.deletion_status == "COMPLETE":
                result.steps, _ = inv._plan(result.obligations, force_incomplete=True)
                result.deletion_status = "INCOMPLETE"
                result.internal_status = "NEEDS_REVIEW"
        return result, cov

    def close(self):
        for c in self.connectors.values():
            c.close()


# ---------------------------------------------------------------------- #
def _values(ids: set[tuple[str, str]]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for k, v in ids:
        if k != "name":
            out.setdefault(k, set()).add(v)
    return out


def _field(c: CollectionMap, rec: dict, kind: str):
    path = c.identifiers.get(kind)
    if not path:
        return None
    v = flatten_path(rec, path) if "." in path else rec.get(path)
    return None if is_null(v) else v


def _record_identifiers(c: CollectionMap, rec: dict) -> set[tuple[str, str]]:
    out = set()
    for kind in c.identifiers:
        v = _field(c, rec, kind)
        if v is None:
            continue
        if kind == "user_ref":
            out.add(("user_ref", str(v).strip().lower()))
        else:
            i = ident(kind, str(v))
            if i:
                out.add(i)
                if kind == "email":
                    out.add(("email_hash", email_hash(i[1])))
    return out


def to_observation(connector: str, c: CollectionMap, r: RawRecord) -> Observation:
    ids = _record_identifiers(c, r.fields)
    pii = {k for k, _ in ids if not (k == "email_hash" and "email_hash" not in c.identifiers)}
    for f in c.text_fields:
        text = str(r.fields.get(f) or "")
        for m in EMAIL_RE.findall(text):
            i = ident("email", m)
            if i:
                ids.add(i)
                pii.add("email")
        if PHONE_TEXT.search(text):
            pii.add("phone")
    st = c.state
    if st.deleted_when and st.deleted_when.holds(r.fields):
        state = "ABSENT"
    elif c.category == "DATA_FLOW":
        state = ("ACTIVE" if st.active_when.holds(r.fields) else "DISABLED") if st.active_when else None
    elif c.category == "BACKUP_ARCHIVE":
        state = ("RESTORABLE" if st.restorable_when.holds(r.fields) else "BLOCKED") if st.restorable_when else None
    else:
        state = "PRESENT"
    return Observation(
        source_id=f"{connector}:{c.source}@{_ts(r.observed_at)}", source_kind="live", observed_at=r.observed_at,
        captured_at=r.observed_at, system_id=c.system, resource_id=f"{connector}:{c.source}:{r.record_id}",
        category=c.category, state=state, pii=frozenset(pii), identifiers=frozenset(ids), locator=c.source,
        note=f"live read via {connector}",
    )


def write_journal(path: Path, run: LiveRun) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for e in run.journal:
            fh.write(json.dumps(e, sort_keys=True) + "\n")
        for c in run.coverage:
            fh.write(json.dumps({"coverage": c.status, "connector": c.connector, "collection": c.source,
                                 "reason": c.reason, "records": c.records}, sort_keys=True) + "\n")


def load_rules(path: Path) -> list[Rule]:
    cols = ["rule_id", "data_category", "condition", "required_action"]
    return [Rule(**{k: r[k] for k in cols}) for r in _read(path, cols)]


def live_request(email: str, text: str = "Live audit of a deletion request.", case_id: str | None = None,
                 requested_at: datetime | None = None) -> DeletionRequest:
    now = _now()
    return DeletionRequest(case_id=case_id or f"live_{hashlib.sha256(email.lower().encode()).hexdigest()[:8]}", email=email,
                           requested_at=requested_at or now - timedelta(days=1), reported_status="NOT_STARTED",
                           reported_at=now, audit_at=now, request_text=text)


# ---------------------------------------------------------------------- #
def export_snapshot(auditor: LiveAuditor, reqs: list[DeletionRequest], out_root: Path, run: LiveRun) -> dict:
    """Write a dataset folder (SQL snapshot + manifest + identities + requests) for offline audit.

    Only records matching the requested subjects are exported. Stable references are written as
    `user_<digits>` when numeric so the offline identity graph can join on them; other reference
    formats are dropped from the snapshot (linking then relies on emails/phones/hashes) and counted.
    """
    out_root.mkdir(parents=True, exist_ok=True)
    (out_root / "evidence" / "databases").mkdir(parents=True, exist_ok=True)
    cap = _now()
    stamp = cap.strftime("%Y%m%dT%H%M%SZ")
    rows_by_coll: dict[tuple[str, str], list] = {}
    identities = {}
    audit_rows: list[list[str]] = []
    dropped_refs = 0

    def ref_token(v) -> str | None:
        s = str(v).strip().lower()
        if re.fullmatch(r"user_\d+", s):
            return s
        if s.isdigit():
            return f"user_{s}"
        return None

    for q in reqs:
        ident_res = auditor.resolve(q.email, run)
        idx = auditor.collect(ident_res, run)
        uid = ref_token(ident_res.user_id) if ident_res.user_id and not ident_res.user_id.startswith("unresolved:") else None
        emails = sorted(v for k, v in ident_res.identifiers if k == "email")
        phones = sorted(v for k, v in ident_res.identifiers if k == "phone")
        if uid:
            req_e = ident("email", q.email)
            current = req_e[1] if req_e and req_e[1] in emails else (emails[0] if emails else "")
            identities[uid] = [uid, "", current, phones[0] if phones else "", email_hash(current) if current else "", _ts(cap)]
            for other in emails:
                if other != current:  # other known emails become history rows so the offline graph resolves them
                    audit_rows.append([f"audit_snap_{len(audit_rows) + 1}", uid, _ts(cap), "email", other, current])
        for o in idx.observations:
            conn, src, _rid = o.resource_id.split(":", 2)
            rec_fields = {"resource_id": o.resource_id, "record_type": o.category, "system": o.system_id}
            # canonical identifier fields from the observation (values only for identifier kinds)
            for k, v in o.identifiers:
                if k == "user_ref":
                    t = ref_token(v)
                    if t:
                        rec_fields["customer_reference"] = t
                    else:
                        dropped_refs += 1
                elif k == "email" and "email" in o.pii:
                    rec_fields.setdefault("email", v)
                elif k == "phone":
                    rec_fields.setdefault("phone", v)
                elif k == "email_hash" and "email_hash" in o.pii:
                    rec_fields.setdefault("email_hash", v)
            if "name" in o.pii:
                rec_fields["full_name"] = "[present]"  # names are exposure markers, not join keys
            if o.state == "ABSENT":
                rec_fields["result"] = "NOT_FOUND"
            if o.category == "DATA_FLOW" and o.state:
                rec_fields["enabled"] = o.state == "ACTIVE"
            if o.category == "BACKUP_ARCHIVE" and o.state:
                rec_fields["restore_allowed"] = o.state == "RESTORABLE"
            rows_by_coll.setdefault((conn, src), []).append((o.resource_id, o.system_id, rec_fields))
    lines = ["-- Ghost Data Still Exists: point-in-time database snapshot", "-- Dialect: SQLite-compatible SQL",
             f"-- Captured at: {_ts(cap)}", "-- Database topology: connector_export", "", "BEGIN TRANSACTION;",
             "CREATE TABLE snapshot_scope (origin_system TEXT PRIMARY KEY, captured_at TEXT NOT NULL);"]
    for system in sorted({c.system for _, c in auditor.approved("evidence")}):
        lines.append(f"INSERT INTO snapshot_scope VALUES ('{_sq(system)}', '{_ts(cap)}');")
    for (conn, src), rows in sorted(rows_by_coll.items()):
        t = "di_" + re.sub(r"[^a-z0-9]+", "_", f"{conn}_{src}".lower())
        lines.append(f'CREATE TABLE "{t}" (resource_id TEXT PRIMARY KEY, origin_system TEXT NOT NULL, collection_name TEXT NOT NULL, '
                     "record_json TEXT NOT NULL, updated_at TEXT NOT NULL);")
        seen = set()
        for rid, system, rec in rows:
            if rid in seen:
                continue
            seen.add(rid)
            lines.append(f"INSERT INTO \"{t}\" VALUES('{_sq(rid)}','{_sq(system)}','{_sq(src)}','{_sq(json.dumps(rec, sort_keys=True))}','{_ts(cap)}');")
    lines.append("COMMIT;")
    rel = Path("evidence") / "databases" / f"connectors_{stamp}.sql"
    (out_root / rel).write_text("\n".join(lines) + "\n", encoding="utf-8")

    man = out_root / "evidence_files.csv"
    existing = list(csv.reader(man.open())) if man.is_file() else [["file_id", "file_path", "origin", "captured_at"]]
    fid = f"evidence_snapshot_{stamp}"
    existing.append([fid, rel.as_posix(), "connector_snapshot", _ts(cap)])
    _write(man, existing)
    _write_if_missing(out_root / "user_identities.csv", [["user_id", "name", "email", "phone", "email_hash", "updated_at"], *identities.values()])
    _write_if_missing(out_root / "user_audit_log.csv",
                      [["audit_id", "user_id", "event_time", "field_name", "old_value", "new_value"], *audit_rows])
    for name, header in (("system_activity.csv", ["activity_id", "event_time", "event_type", "source_system", "target_system",
                                                  "resource_id", "reported_result", "details"]),
                         ("sample_deletion_requests.csv", ["case_id", "email", "requested_at", "reported_status", "reported_at", "audit_at", "request_text"])):
        _write_if_missing(out_root / name, [header])
    _write_if_missing(out_root / "deletion_requests.csv",
                      [["case_id", "email", "requested_at", "reported_status", "reported_at", "audit_at", "request_text"],
                       *[[q.case_id, q.email, _ts(q.requested_at), q.reported_status, _ts(q.reported_at), _ts(max(q.audit_at, cap)), q.request_text] for q in reqs]])
    rules_dst = out_root / "deletion_rules.csv"
    if not rules_dst.exists():
        _write(rules_dst, [["rule_id", "data_category", "condition", "required_action"],
                           *[[r.rule_id, r.data_category, r.condition, r.required_action] for r in auditor.rules]])
    return {"file": str(out_root / rel), "file_id": fid, "subjects": len(reqs), "collections": len(rows_by_coll),
            "records": sum(len(v) for v in rows_by_coll.values()), "dropped_refs": dropped_refs,
            "systems_in_scope": len({c.system for _, c in auditor.approved('evidence')})}


def _sq(s: str) -> str:
    return str(s).replace("'", "''")


def _write(path: Path, rows):
    with path.open("w", newline="", encoding="utf-8") as fh:
        csv.writer(fh, lineterminator="\n").writerows(rows)


def _write_if_missing(path: Path, rows):
    if not path.exists():
        _write(path, rows)
