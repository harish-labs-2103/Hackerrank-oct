"""Deterministic policy engine built from deletion_rules.csv.

Each data category maps to a required end state. The mapping from rule actions to
operations is fixed by the challenge contract; the category -> action table is read from
the rules file so a changed policy changes behaviour without code edits.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import Rule

# Identifier kinds that a REDACT/ANONYMIZE treatment must remove.
DIRECT_IDENTIFIERS = {"email", "phone", "email_hash", "name", "direct"}
# Anonymization must also unlink the stable reference; redaction may keep it (tombstones,
# support conversations keep `customer_reference` so retained records stay joinable).
ANONYMIZE_REMOVES = DIRECT_IDENTIFIERS | {"user_ref"}


@dataclass(frozen=True)
class Treatment:
    category: str
    rule_id: str
    action: str  # DELETE | REDACT | ANONYMIZE | RETAIN
    required_state: str  # ABSENT | REDACTED | ANONYMIZED | PRESENT | DISABLED | BLOCKED
    finding_op: str
    remediation_op: str | None
    verify_op: str


# Categories whose treatment is about a process rather than a stored copy.
_FLOW = Treatment("DATA_FLOW", "policy:data_flow", "DISABLE", "DISABLED", "REPORT_ACTIVE_DATA_FLOW", "DISABLE_DATA_FLOW", "VERIFY_DATA_FLOW_DISABLED")

_ACTION_OPS = {
    "DELETE": ("ABSENT", "REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT"),
    "REDACT": ("REDACTED", "REPORT_EXPOSED_PII", "REDACT_RECORD", "VERIFY_RECORD_REDACTED"),
    "ANONYMIZE": ("ANONYMIZED", "REPORT_EXPOSED_PII", "ANONYMIZE_RECORD", "VERIFY_RECORD_ANONYMIZED"),
    "RETAIN": ("PRESENT", "", None, "VERIFY_RECORD_PRESENT"),
}


class PolicyEngine:
    def __init__(self, rules: list[Rule]):
        self.rules = {r.data_category: r for r in rules}

    def treatment(self, category: str) -> Treatment | None:
        if category == "DATA_FLOW":
            return _FLOW
        if category == "BACKUP_ARCHIVE":
            r = self.rules.get(category)
            # Retained archive; compliance means restore of the subject's data is blocked.
            return Treatment(category, r.rule_id if r else "policy:backup", "RETAIN", "BLOCKED",
                             "REPORT_RESTORE_RISK", "BLOCK_RESTORE", "VERIFY_RESTORE_BLOCKED")
        r = self.rules.get(category)
        if r is None:
            return None
        state, finding, remediation, verify = _ACTION_OPS[r.required_action.strip().upper()]
        return Treatment(category, r.rule_id, r.required_action.upper(), state, finding, remediation, verify)

    @staticmethod
    def exposed(action: str, pii: frozenset[str] | set[str]) -> bool:
        if action == "ANONYMIZE":
            return bool(set(pii) & ANONYMIZE_REMOVES)
        if action == "REDACT":
            return bool(set(pii) & DIRECT_IDENTIFIERS)
        return False
