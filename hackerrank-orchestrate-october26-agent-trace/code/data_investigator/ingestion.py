"""Load and validate the participant-facing CSV files under dataset/."""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from .models import Activity, AuditEvent, DeletionRequest, ManifestEntry, Rule
from .normalize import parse_ts


class IngestionError(Exception):
    pass


REQUIRED = {
    "deletion_requests.csv": ["case_id", "email", "requested_at", "reported_status", "reported_at", "audit_at", "request_text"],
    "user_identities.csv": ["user_id", "name", "email", "phone", "email_hash", "updated_at"],
    "user_audit_log.csv": ["audit_id", "user_id", "event_time", "field_name", "old_value", "new_value"],
    "system_activity.csv": ["activity_id", "event_time", "event_type", "source_system", "target_system", "resource_id", "reported_result", "details"],
    "deletion_rules.csv": ["rule_id", "data_category", "condition", "required_action"],
    "evidence_files.csv": ["file_id", "file_path", "origin", "captured_at"],
}


def _read(path: Path, required: list[str]) -> list[dict[str, str]]:
    if not path.is_file():
        raise IngestionError(f"Required input file is missing: {path}")
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in required if c not in (reader.fieldnames or [])]
        if missing:
            raise IngestionError(f"{path.name}: missing columns {missing}")
        return list(reader)


def _ts(row: dict, col: str, where: str):
    dt = parse_ts(row.get(col))
    if dt is None:
        raise IngestionError(f"Could not parse {col} for {where}: {row.get(col)!r}")
    return dt


def case_sort_key(case_id: str):
    head, _, tail = case_id.rpartition("_")
    return (head, int(tail)) if tail.isdigit() else (case_id, -1)


def load_requests(path: Path) -> list[DeletionRequest]:
    rows = _read(path, REQUIRED["deletion_requests.csv"])
    out, seen = [], set()
    for r in rows:
        cid = r["case_id"].strip()
        if not cid:
            raise IngestionError(f"{path.name}: empty case_id")
        if cid in seen:
            raise IngestionError(f"{path.name}: duplicate case_id {cid}")
        seen.add(cid)
        out.append(
            DeletionRequest(
                case_id=cid,
                email=r["email"].strip(),
                requested_at=_ts(r, "requested_at", cid),
                reported_status=r["reported_status"].strip(),
                reported_at=_ts(r, "reported_at", cid),
                audit_at=_ts(r, "audit_at", cid),
                request_text=r["request_text"],
            )
        )
    return sorted(out, key=lambda q: case_sort_key(q.case_id))


@dataclass
class Dataset:
    root: Path
    identities: list[dict[str, str]]
    audit_log: list[AuditEvent]
    activities: list[Activity]
    rules: list[Rule]
    manifest: dict[str, ManifestEntry]
    warnings: list[str] = field(default_factory=list)

    def evidence_path(self, entry: ManifestEntry) -> Path:
        p = (self.root / entry.file_path).resolve()
        if not p.is_relative_to(self.root.resolve()):
            raise IngestionError(f"{entry.file_id}: path escapes dataset root")
        return p


def load_dataset(root: Path) -> Dataset:
    root = Path(root)
    identities = _read(root / "user_identities.csv", REQUIRED["user_identities.csv"])
    audit = [
        AuditEvent(
            audit_id=r["audit_id"], user_id=r["user_id"], event_time=_ts(r, "event_time", r["audit_id"]),
            field_name=r["field_name"], old_value=r["old_value"], new_value=r["new_value"],
        )
        for r in _read(root / "user_audit_log.csv", REQUIRED["user_audit_log.csv"])
    ]
    acts = [
        Activity(
            activity_id=r["activity_id"], event_time=_ts(r, "event_time", r["activity_id"]),
            event_type=r["event_type"], source_system=r["source_system"], target_system=r["target_system"],
            resource_id=r["resource_id"], reported_result=r["reported_result"], details=r["details"],
        )
        for r in _read(root / "system_activity.csv", REQUIRED["system_activity.csv"])
    ]
    rules = [Rule(**{k: r[k] for k in REQUIRED["deletion_rules.csv"]}) for r in _read(root / "deletion_rules.csv", REQUIRED["deletion_rules.csv"])]
    manifest: dict[str, ManifestEntry] = {}
    warnings: list[str] = []
    for r in _read(root / "evidence_files.csv", REQUIRED["evidence_files.csv"]):
        e = ManifestEntry(r["file_id"], r["file_path"], r["origin"], _ts(r, "captured_at", r["file_id"]))
        manifest[e.file_id] = e
    ds = Dataset(root, identities, audit, acts, rules, manifest, warnings)
    for e in manifest.values():
        if not ds.evidence_path(e).is_file():
            warnings.append(f"{e.file_id}: evidence file not found at {e.file_path}")
    return ds
