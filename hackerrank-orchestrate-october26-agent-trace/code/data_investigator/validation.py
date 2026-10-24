"""Challenge output contract validator. Shared by `audit --all` and `validate-output`."""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .models import OP_RANK, OPERATIONS, PHASE_RANK, STATUSES, Phase

COLUMNS = ["case_id", "deletion_status", "steps_json"]
STEP_KEYS = {"step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"}


@dataclass
class ValidationIssue:
    case_id: str
    problem: str
    detail: str = ""


@dataclass
class ValidationReport:
    checks: list[tuple[str, bool]] = field(default_factory=list)
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues


def validate_rows(header: list[str], rows: list[dict], expected_ids: list[str],
                  evidence_times: dict[str, datetime] | None = None,
                  audit_at: dict[str, datetime] | None = None) -> ValidationReport:
    rep = ValidationReport()
    issues = rep.issues

    def check(name: str, before: int):
        rep.checks.append((name, len(issues) == before))

    n = len(issues)
    if header != COLUMNS:
        issues.append(ValidationIssue("-", "Required columns", f"got {header}, expected {COLUMNS}"))
    check("Required columns", n)

    n = len(issues)
    ids = [r.get("case_id", "") for r in rows]
    if len(rows) != len(expected_ids):
        issues.append(ValidationIssue("-", "Input/output case count", f"{len(rows)} rows for {len(expected_ids)} cases"))
    check("Input/output case count", n)

    n = len(issues)
    for cid in sorted(set(expected_ids) - set(ids)):
        issues.append(ValidationIssue(cid, "Missing case"))
    for cid in sorted(set(ids) - set(expected_ids)):
        issues.append(ValidationIssue(cid, "Unexpected case"))
    check("No missing or unexpected cases", n)

    n = len(issues)
    seen = set()
    for cid in ids:
        if cid in seen:
            issues.append(ValidationIssue(cid, "Duplicate case"))
        seen.add(cid)
    check("No duplicate cases", n)

    n = len(issues)
    if ids != expected_ids:
        issues.append(ValidationIssue("-", "Sorted case IDs", "rows are not in case_id order"))
    check("Sorted case IDs", n)

    for r in rows:
        cid = r.get("case_id", "")
        status = r.get("deletion_status", "")
        if status not in STATUSES:
            issues.append(ValidationIssue(cid, "Invalid status value", status))
        try:
            steps = json.loads(r.get("steps_json", ""))
            if not isinstance(steps, list):
                raise ValueError("not a JSON array")
        except (ValueError, TypeError) as e:
            issues.append(ValidationIssue(cid, "Invalid JSON in steps_json", str(e)))
            continue
        if status == "NOT_APPLICABLE" and steps:
            issues.append(ValidationIssue(cid, "NOT_APPLICABLE must have []"))
        prev = None
        for i, s in enumerate(steps, 1):
            if not isinstance(s, dict) or set(s) != STEP_KEYS:
                issues.append(ValidationIssue(cid, "Required step fields", f"step {i}: {sorted(s) if isinstance(s, dict) else s}"))
                continue
            if s["step_no"] != i:
                issues.append(ValidationIssue(cid, "Consecutive step numbers", f"step {i} has step_no {s['step_no']}"))
            op = s["operation"]
            if op not in OPERATIONS:
                issues.append(ValidationIssue(cid, "Allowed operation/state combinations", f"unknown operation {op}"))
                continue
            phase, state = OPERATIONS[op]
            if s["phase"] != phase.value or s["state"] != state:
                issues.append(ValidationIssue(cid, "Allowed operation/state combinations",
                                              f"Operation: {op} State: {s['state']} Phase: {s['phase']} (expected {phase.value}/{state})"))
            key = (PHASE_RANK[phase], OP_RANK[op], s["system_id"], s["resource_id"])
            if prev is not None and key < prev:
                issues.append(ValidationIssue(cid, "Phase and operation ordering", f"step {i} out of order"))
            prev = key
            future = status == "INCOMPLETE" and phase != Phase.FINDING
            if future and s["evidence_id"] != "":
                issues.append(ValidationIssue(cid, "Evidence references", f"step {i}: proposed step must have empty evidence_id"))
            if not future:
                ev = s["evidence_id"]
                if not ev:
                    issues.append(ValidationIssue(cid, "Evidence references", f"step {i}: finding/verification needs evidence"))
                elif evidence_times is not None and ev not in evidence_times:
                    issues.append(ValidationIssue(cid, "Evidence references", f"step {i}: unknown evidence {ev}"))
                elif evidence_times is not None and audit_at and cid in audit_at and evidence_times[ev] > audit_at[cid]:
                    issues.append(ValidationIssue(cid, "Evidence references", f"step {i}: {ev} is dated after audit_at"))
            if status == "COMPLETE" and phase != Phase.VERIFICATION:
                issues.append(ValidationIssue(cid, "COMPLETE cases contain only verification", f"step {i}: {op}"))
        if status == "INCOMPLETE" and steps and not any(s.get("phase") == "FINDING" for s in steps if isinstance(s, dict)):
            issues.append(ValidationIssue(cid, "INCOMPLETE cases need findings"))
    groups = {
        "Valid status values": {"Invalid status value", "NOT_APPLICABLE must have []"},
        "Valid JSON in steps_json": {"Invalid JSON in steps_json"},
        "Required step fields": {"Required step fields"},
        "Allowed operation/state combinations": {"Allowed operation/state combinations"},
        "Consecutive step numbers": {"Consecutive step numbers"},
        "Phase and operation ordering": {"Phase and operation ordering"},
        "Evidence references": {"Evidence references"},
        "Status-specific step rules": {"COMPLETE cases contain only verification", "INCOMPLETE cases need findings"},
    }
    for label, problems in groups.items():
        rep.checks.append((label, not any(i.problem in problems for i in issues)))
    return rep


def read_output(path: Path) -> tuple[list[str], list[dict]]:
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        return list(reader.fieldnames or []), list(reader)


def write_output(path: Path, rows: list[tuple[str, str, list[dict]]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
        w.writerow(COLUMNS)
        for cid, status, steps in rows:
            w.writerow([cid, status, json.dumps(steps, sort_keys=True, separators=(",", ":"), ensure_ascii=False)])
