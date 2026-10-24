"""Core data models shared across the investigation pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator


# --------------------------------------------------------------------------- #
# Challenge contract
# --------------------------------------------------------------------------- #
class Phase(str, Enum):
    FINDING = "FINDING"
    REMEDIATION = "REMEDIATION"
    VERIFICATION = "VERIFICATION"


# Operation -> (phase, state). Order of this dict IS the challenge operation order.
OPERATIONS: dict[str, tuple[Phase, str]] = {
    "REPORT_REMAINING_RECORD": (Phase.FINDING, "PRESENT"),
    "REPORT_EXPOSED_PII": (Phase.FINDING, "PRESENT"),
    "REPORT_ACTIVE_DATA_FLOW": (Phase.FINDING, "ACTIVE"),
    "REPORT_RESTORE_RISK": (Phase.FINDING, "ACTIVE"),
    "DISABLE_DATA_FLOW": (Phase.REMEDIATION, "DISABLED"),
    "BLOCK_RESTORE": (Phase.REMEDIATION, "BLOCKED"),
    "DELETE_RECORD": (Phase.REMEDIATION, "ABSENT"),
    "REDACT_RECORD": (Phase.REMEDIATION, "REDACTED"),
    "ANONYMIZE_RECORD": (Phase.REMEDIATION, "ANONYMIZED"),
    "VERIFY_RECORD_ABSENT": (Phase.VERIFICATION, "ABSENT"),
    "VERIFY_RECORD_PRESENT": (Phase.VERIFICATION, "PRESENT"),
    "VERIFY_RECORD_REDACTED": (Phase.VERIFICATION, "REDACTED"),
    "VERIFY_RECORD_ANONYMIZED": (Phase.VERIFICATION, "ANONYMIZED"),
    "VERIFY_DATA_FLOW_DISABLED": (Phase.VERIFICATION, "DISABLED"),
    "VERIFY_RESTORE_BLOCKED": (Phase.VERIFICATION, "BLOCKED"),
}
OP_RANK = {op: i for i, op in enumerate(OPERATIONS)}
PHASE_RANK = {Phase.FINDING: 0, Phase.REMEDIATION: 1, Phase.VERIFICATION: 2}
STATUSES = ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE")


class Step(BaseModel):
    """One element of steps_json. Invalid operation/state/phase combos cannot be built."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step_no: int
    phase: Phase
    system_id: str
    resource_id: str
    operation: str
    state: str
    evidence_id: str

    @model_validator(mode="after")
    def _check_contract(self) -> "Step":
        if self.operation not in OPERATIONS:
            raise ValueError(f"unknown operation {self.operation}")
        phase, state = OPERATIONS[self.operation]
        if phase != self.phase or state != self.state:
            raise ValueError(f"{self.operation} requires phase={phase.value} state={state}")
        if self.step_no < 1:
            raise ValueError("step_no must start at 1")
        return self

    def to_json(self) -> dict:
        return {
            "step_no": self.step_no,
            "phase": self.phase.value,
            "system_id": self.system_id,
            "resource_id": self.resource_id,
            "operation": self.operation,
            "state": self.state,
            "evidence_id": self.evidence_id,
        }


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DeletionRequest:
    case_id: str
    email: str
    requested_at: datetime
    reported_status: str  # claim only, never evidence
    reported_at: datetime
    audit_at: datetime
    request_text: str


@dataclass(frozen=True)
class ManifestEntry:
    file_id: str
    file_path: str
    origin: str
    captured_at: datetime


@dataclass(frozen=True)
class Activity:
    activity_id: str
    event_time: datetime
    event_type: str
    source_system: str
    target_system: str
    resource_id: str
    reported_result: str
    details: str


@dataclass(frozen=True)
class AuditEvent:
    audit_id: str
    user_id: str
    event_time: datetime
    field_name: str
    old_value: str
    new_value: str


@dataclass(frozen=True)
class Rule:
    rule_id: str
    data_category: str
    condition: str
    required_action: str


# --------------------------------------------------------------------------- #
# Evidence
# --------------------------------------------------------------------------- #
IdentKind = Literal["email", "phone", "email_hash", "user_ref", "name"]


@dataclass
class Observation:
    """One normalized sighting of a resource in one evidence source.

    `state` is what this sighting shows about the resource:
      PRESENT      record exists (identifiers, if any, in `pii`)
      ABSENT       an authoritative lookup found no record
      ACTIVE / DISABLED   data-flow status
      RESTORABLE / BLOCKED  backup restore status
    `pii` lists direct identifier kinds still visible on the record.
    """

    source_id: str  # file_id / activity_id / audit_id
    source_kind: Literal["file", "activity", "audit"]
    observed_at: datetime
    captured_at: datetime  # when the source became available (manifest captured_at)
    system_id: str
    resource_id: str
    category: str | None
    state: str | None
    pii: frozenset[str] = frozenset()
    identifiers: frozenset[tuple[str, str]] = frozenset()  # (kind, normalized value)
    locator: str = ""  # table/line/path inside the source
    note: str = ""

    def key(self) -> tuple[str, str]:
        return (self.system_id, self.resource_id)


@dataclass
class ResourceLink:
    """Evidence that a resource belongs to a subject."""

    system_id: str
    resource_id: str
    via: tuple[str, str]  # matched identifier (kind, value)
    source_id: str
    observed_at: datetime
    captured_at: datetime


@dataclass
class ResolvedIdentity:
    user_id: str | None
    identifiers: set[tuple[str, str]] = field(default_factory=set)
    path: list[str] = field(default_factory=list)
    ambiguities: list[str] = field(default_factory=list)
