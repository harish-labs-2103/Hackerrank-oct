"""Unit tests: normalization, identity, parsing, policy, ordering, validation."""
from datetime import datetime, timezone

import pytest

from davestigator.evidence.interpret import interpret_record
from davestigator.evidence.parsers import _parse_log_line
from davestigator.identity import IdentityGraph
from davestigator.intent import HeuristicIntentClassifier
from davestigator.models import AuditEvent, Phase, Rule, Step
from davestigator.normalize import email_hash, ident, parse_ts
from davestigator.planner import PlannedStep, finalize
from davestigator.policy import PolicyEngine
from davestigator.validation import validate_rows

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


def test_parse_ts_utc():
    assert parse_ts("2026-09-15T12:00:00Z") == datetime(2026, 9, 15, 12, tzinfo=timezone.utc)
    assert parse_ts("null") is None


def test_ident_normalization():
    assert ident("email", "JULES-LAURENT@PROTON.ME") == ("email", "jules-laurent@proton.me")
    assert ident("phone", "+1 (202) 555-0103") == ("phone", "12025550103")
    assert ident("user_ref", "User_42") == ("user_ref", "user_42")
    assert ident("email", None) is None


def test_identity_resolves_historical_email_and_hash():
    ids = [{"user_id": "user_1", "name": "A B", "email": "new@x.com", "phone": "+1 555 000 1111",
            "email_hash": email_hash("new@x.com"), "updated_at": "2026-01-01T00:00:00Z"}]
    audit = [AuditEvent("audit_1", "user_1", T0, "email", "old@x.com", "new@x.com")]
    g = IdentityGraph(ids, audit)
    r = g.resolve("OLD@x.com")
    assert r.user_id == "user_1"
    assert ("email", "new@x.com") in r.identifiers and ("email_hash", email_hash("old@x.com")) in r.identifiers
    assert all(k != "name" for k, _ in r.identifiers), "names never link on their own"
    assert g.resolve("old.jr@x.com").user_id is None, "similar emails are not the same identity"


def test_log_trace_formats():
    for line in [
        '2026-07-28T05:51:21Z [INFO] [account_service] [tr-6074163] profile_lookup_completed: resource_id="account_7947" rows="0" http_status="404"',
        '2026-07-03T07:51:32Z | DEBUG | security_service | tr-5156005 | audit_record_read_completed | resource_id="audit_record_4413" rows="1"',
        '2026-09-30T11:51:03Z requestId=tr-2263120 level=DEBUG function=profile_service message="resource_id=\\"account_66777\\" rows=\\"1\\""',
        '2026-09-16T08:52:59Z {"service":"security_service","trace_id":"tr-9028396","resource_id":"audit_record_4902","rows":"1"}',
    ]:
        ts, f = _parse_log_line(line)
        assert ts is not None and f.get("resource_id") and f.get("trace_id"), line


def _obs(fields, rid, **kw):
    return interpret_record(fields=fields, resource_id=rid, source_id="evidence_x", observed_at=T0, captured_at=T0, **kw)


def test_interpret_states():
    assert _obs({"http_status": "404", "rows": "0"}, "account_1").state == "ABSENT"
    assert _obs({"result": "hit", "email": "a@b.co"}, "search_1").state == "PRESENT"
    assert _obs({"enabled": "true"}, "data_flow_1").state == "ACTIVE"
    assert _obs({"restore_allowed": 0}, "backup_1").state == "BLOCKED"
    a = _obs({"customer_reference": None, "email": "a@b.co"}, "analytics_1")
    assert a.state == "PRESENT" and "email" in a.pii
    s = _obs({"record_type": "SUPPORT_MESSAGE", "result": "IDENTIFIABLE", "subject_reference": "a@b.co"}, "support_1")
    assert "direct" in s.pii
    att = _obs({"collection": "ticket_attachments", "uploaded_by": "a@b.co"}, "support_2")
    assert att.category == "SUPPORT_ATTACHMENT"


RULES = [Rule("rule_05", "SUPPORT_MESSAGE", "shared context", "REDACT"), Rule("rule_07", "ANALYTICS_PROFILE", "attributable", "ANONYMIZE"),
         Rule("rule_15", "ACCOUNT_TOMBSTONE", "only user_id", "REDACT"), Rule("rule_01", "USER_PROFILE", "Always", "DELETE"),
         Rule("rule_12", "SECURITY_AUDIT_LOG", "retention", "RETAIN"), Rule("rule_14", "BACKUP_ARCHIVE", "restore can be blocked", "RETAIN")]


def test_policy_treatments_and_tombstone():
    p = PolicyEngine(RULES)
    assert p.treatment("USER_PROFILE").verify_op == "VERIFY_RECORD_ABSENT"
    assert p.treatment("SECURITY_AUDIT_LOG").verify_op == "VERIFY_RECORD_PRESENT"
    assert p.treatment("BACKUP_ARCHIVE").remediation_op == "BLOCK_RESTORE"
    assert p.treatment("DATA_FLOW").finding_op == "REPORT_ACTIVE_DATA_FLOW"
    # tombstone may keep the stable user reference, nothing else
    assert not p.exposed("REDACT", {"user_ref"})
    assert p.exposed("REDACT", {"user_ref", "email_hash"})
    # anonymization must also unlink the user reference
    assert p.exposed("ANONYMIZE", {"user_ref"})


def test_step_contract_rejects_bad_pairs():
    with pytest.raises(ValueError):
        Step(step_no=1, phase=Phase.REMEDIATION, system_id="a", resource_id="b", operation="DELETE_RECORD", state="PRESENT", evidence_id="")
    with pytest.raises(ValueError):
        Step(step_no=1, phase=Phase.REMEDIATION, system_id="a", resource_id="b", operation="DROP_TABLE", state="ABSENT", evidence_id="")


def test_planner_order_uses_contract_not_alphabet():
    steps = finalize([
        PlannedStep("VERIFY_RECORD_ANONYMIZED", "analytics_platform", "analytics_1", ""),
        PlannedStep("DELETE_RECORD", "search_service", "search_1", ""),
        PlannedStep("REPORT_ACTIVE_DATA_FLOW", "integration_service", "data_flow_1", "e"),
        PlannedStep("DISABLE_DATA_FLOW", "integration_service", "data_flow_1", ""),
        PlannedStep("REPORT_REMAINING_RECORD", "search_service", "search_1", "e"),
        PlannedStep("VERIFY_RECORD_ABSENT", "search_service", "search_1", ""),
        PlannedStep("VERIFY_RECORD_ABSENT", "account_service", "account_1", ""),
    ])
    assert [s.operation for s in steps] == ["REPORT_REMAINING_RECORD", "REPORT_ACTIVE_DATA_FLOW", "DISABLE_DATA_FLOW", "DELETE_RECORD",
                                            "VERIFY_RECORD_ABSENT", "VERIFY_RECORD_ABSENT", "VERIFY_RECORD_ANONYMIZED"]
    assert [s.resource_id for s in steps][4:6] == ["account_1", "search_1"]
    assert [s.step_no for s in steps] == list(range(1, 8))


@pytest.mark.parametrize("text,deletion", [
    ("Please permanently delete the account associated with this email address.", True),
    ("Solicito la eliminación permanente de la cuenta asociada a este correo.", True),
    ("このメールアドレスに関連するアカウントの完全な削除を依頼します。", True),
    ("I found an old report saying my account still existed. Please confirm that the profile is gone.", True),
    ("Please stop the promotional emails. I still need the account, so I am not asking for deletion.", False),
    ("Please remove me from marketing lists only. I still want receipts and service messages for this account.", False),
    ("Could you send me a copy of the information held on my account? I still use it, so please don't delete or change anything.", False),
    ("My surname is misspelled on the account. Can you correct it without removing any of my records?", False),
    ("Can you tell me what personal information you hold about me? This is an information request only.", False),
    ("Can you tell me what personal information you hold about me? This is an information request, not a request to erase it.", False),
])
def test_heuristic_intent(text, deletion):
    assert HeuristicIntentClassifier().classify(text).is_deletion is deletion


def test_validator_catches_contract_violations():
    good = '[{"step_no":1,"phase":"VERIFICATION","system_id":"s","resource_id":"r","operation":"VERIFY_RECORD_ABSENT","state":"ABSENT","evidence_id":"evidence_1"}]'
    bad_pair = '[{"step_no":1,"phase":"REMEDIATION","system_id":"s","resource_id":"r","operation":"DELETE_RECORD","state":"PRESENT","evidence_id":""}]'
    hdr = ["case_id", "deletion_status", "steps_json"]
    ok = validate_rows(hdr, [{"case_id": "case_1", "deletion_status": "COMPLETE", "steps_json": good}], ["case_1"])
    assert ok.ok
    rep = validate_rows(hdr, [{"case_id": "case_1", "deletion_status": "INCOMPLETE", "steps_json": bad_pair},
                              {"case_id": "case_1", "deletion_status": "MAYBE", "steps_json": "not json"}], ["case_1", "case_2"])
    problems = {i.problem for i in rep.issues}
    assert {"Allowed operation/state combinations", "Duplicate case", "Missing case", "Invalid status value",
            "Invalid JSON in steps_json"} <= problems
