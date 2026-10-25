"""Turn a raw record (dict of fields) into a normalized Observation.

Every parser (SQL, JSON, logs, text, PDF, image) funnels through `interpret_record`, so
category/state/PII rules live in exactly one place.
"""
from __future__ import annotations

import re
from datetime import datetime

from ..models import Observation
from ..normalize import EMAIL_RE, USER_REF_RE, classify_value, ident, is_null

# Canonical system per resource-id prefix. Gold system_ids are canonical service names,
# so logs emitted by e.g. `profile_service` or `support_replica` map back to these.
PREFIX_SYSTEM = {
    "account": "account_service",
    "search": "search_service",
    "analytics": "analytics_platform",
    "support": "support_platform",
    "export": "export_service",
    "data_flow": "integration_service",
    "backup": "backup_service",
    "audit_record": "security_service",
    "tombstone": "identity_service",
    "legal_hold": "legal_service",
    "cache": "cache_service",
    "marketing": "marketing_platform",
    "email_delivery": "email_delivery_service",
    "object": "object_storage",
}
PREFIX_CATEGORY = {
    "account": "USER_PROFILE",
    "search": "SEARCH_INDEX_ENTRY",
    "analytics": "ANALYTICS_PROFILE",
    "support": "SUPPORT_MESSAGE",
    "export": "USER_EXPORT",
    "data_flow": "DATA_FLOW",
    "backup": "BACKUP_ARCHIVE",
    "audit_record": "SECURITY_AUDIT_LOG",
    "tombstone": "ACCOUNT_TOMBSTONE",
    "legal_hold": "LEGAL_HOLD_RECORD",
    "cache": "CACHE_ENTRY",
    "marketing": "MARKETING_PROFILE",
    "email_delivery": "EMAIL_DELIVERY_LOG",
}
KNOWN_CATEGORIES = set(PREFIX_CATEGORY.values()) | {"SUPPORT_ATTACHMENT", "SECONDARY_IDENTIFIER", "PRODUCT_TELEMETRY"}

_RES_PREFIX = re.compile(r"^([a-z_]+?)_\d+$")


def resource_prefix(resource_id: str) -> str | None:
    m = _RES_PREFIX.match(resource_id)
    return m.group(1) if m else None


def system_for(resource_id: str) -> str | None:
    p = resource_prefix(resource_id)
    return PREFIX_SYSTEM.get(p) if p else None


# Field names that carry direct identifiers. Value -> identifier kind.
EMAIL_FIELDS = {"email", "requester_email", "owner_email", "customer_email", "uploaded_by", "lookup_value"}
PHONE_FIELDS = {"phone", "requester_phone", "owner_phone"}
HASH_FIELDS = {"email_hash"}
NAME_FIELDS = {"full_name", "name", "display_name", "requester_name"}
REF_FIELDS = {"customer_reference", "user_id", "subject_reference", "subject_selector", "subject_ref"}
# Lookup keys: the value a service was queried with. They link but are not record content.
LOOKUP_FIELDS = {"lookup_key", "match_key", "correlation_key", "subject_key", "subject_selector",
                 "lookup_value", "query", "subject_reference", "matched_value"}
TEXT_FIELDS = {"message_text", "message_excerpt"}


def _truthy(v) -> bool | None:
    if isinstance(v, bool):
        return v
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "y", "enabled", "on"}:
        return True
    if s in {"0", "false", "no", "n", "disabled", "off"}:
        return False
    return None


def _category(system: str | None, resource_id: str, hints: dict) -> str | None:
    rt = hints.get("record_type") or hints.get("category")
    if rt and str(rt).upper() in KNOWN_CATEGORIES:
        return str(rt).upper()
    coll = (hints.get("collection") or "").lower()
    event = (hints.get("event") or "").lower()
    storage = (hints.get("storage_class") or "").lower()
    if "attachment" in coll or "attachment" in event or "attachment" in storage or hints.get("file_name") and resource_id.startswith("support_"):
        return "SUPPORT_ATTACHMENT"
    p = resource_prefix(resource_id)
    return PREFIX_CATEGORY.get(p) if p else None


def interpret_record(
    *,
    fields: dict,
    resource_id: str,
    source_id: str,
    observed_at: datetime,
    captured_at: datetime,
    system_hint: str | None = None,
    extra_identifiers: frozenset = frozenset(),
    locator: str = "",
    hints: dict | None = None,
) -> Observation | None:
    hints = dict(hints or {})
    for k in ("record_type", "category", "collection", "event", "storage_class", "file_name"):
        if k in fields and k not in hints:
            hints[k] = fields[k]
    system = system_for(resource_id) or system_hint
    if not system:
        return None
    category = _category(system, resource_id, hints)

    identifiers: set = set(extra_identifiers)
    pii: set[str] = set()

    def add(kind: str, value, as_pii: bool = True):
        i = ident(kind, value) if kind != "auto" else classify_value(value)
        if i:
            identifiers.add(i)
            if as_pii:
                pii.add(i[0])

    # Lookup-report rows ("record_type ... result ... subject_reference") carry the lookup
    # value, not record content; their exposure is expressed by `result`.
    lookup_report = "result" in fields and ("record_type" in fields or "category" in fields)
    for k, v in fields.items():
        if is_null(v) or isinstance(v, (dict, list)):
            continue
        lk = k.lower()
        if lk in LOOKUP_FIELDS and lk not in REF_FIELDS and lk not in EMAIL_FIELDS:
            # query/lookup keys link the observation but are not stored record content,
            # except matched_value which is a value found inside the record.
            add("auto", v, as_pii=(lk == "matched_value"))
            continue
        if lk in EMAIL_FIELDS:
            add("email", v, as_pii=lk != "lookup_value" and not lookup_report)
        elif lk in PHONE_FIELDS:
            add("phone", v)
        elif lk in HASH_FIELDS:
            add("email_hash", v)
        elif lk in NAME_FIELDS:
            add("name", v)
        elif lk in REF_FIELDS:
            add("auto", v, as_pii=not lookup_report and lk not in {"subject_selector", "subject_ref"})
        elif lk in TEXT_FIELDS:
            for m in EMAIL_RE.findall(str(v)):
                add("email", m)
            for m in USER_REF_RE.findall(str(v)):
                add("user_ref", m)

    if str(fields.get("result") or "").upper() == "IDENTIFIABLE":
        pii.add("direct")  # lookup report says direct identifiers remain on the record
    if str(fields.get("direct_identifiers") or "").strip() not in {"", "0", "None"}:
        pii.add("direct")
    if category == "ANALYTICS_PROFILE" and ("email_opt_in" in fields or "contact_lookup" in str(hints.get("event") or "")):
        category = "MARKETING_PROFILE"  # opt-in contact record, not an aggregate analytics profile
    state = _state(category, fields, hints, pii)
    # Direct identifiers that count as exposure for retained categories exclude the stable
    # user reference, which policy permits on tombstones and redacted support messages.
    return Observation(
        source_id=source_id,
        source_kind="file",
        observed_at=observed_at,
        captured_at=captured_at,
        system_id=system,
        resource_id=resource_id,
        category=category,
        state=state,
        pii=frozenset(pii),
        identifiers=frozenset(identifiers),
        locator=locator,
    )


def _get(fields: dict, *names):
    for n in names:
        if n in fields and not is_null(fields[n]):
            return fields[n]
    return None


def _state(category: str | None, fields: dict, hints: dict, pii: set[str]) -> str | None:
    result = str(_get(fields, "result", "status", "state") or "").upper()
    event = str(hints.get("event") or "").lower()

    # Data flows
    if category == "DATA_FLOW":
        en = _truthy(_get(fields, "enabled", "scheduler_registered"))
        if result in {"ACTIVE", "ENABLED"} or en is True:
            return "ACTIVE"
        if result in {"DISABLED", "STOPPED", "INACTIVE"} or en is False:
            return "DISABLED"
        if result == "NOT_FOUND":
            return "ABSENT"
        return None
    # Backups
    if category == "BACKUP_ARCHIVE":
        ra = _truthy(_get(fields, "restore_allowed"))
        elig = _truthy(_get(fields, "eligible"))
        block = _truthy(_get(fields, "deletion_block"))
        if result in {"ACTIVE", "RESTORABLE"} or ra is True or (elig is True and block is not True):
            return "RESTORABLE"
        if result in {"BLOCKED"} or ra is False or block is True or elig is False:
            return "BLOCKED"
        if result == "NOT_FOUND":
            return "ABSENT"
        return None

    # Record existence
    if result in {"NOT_FOUND", "ABSENT", "DELETED", "MISS"}:
        return "ABSENT"
    if result in {"FOUND", "PRESENT", "IDENTIFIABLE", "HIT"}:
        return "PRESENT"
    if result in {"ANONYMIZED"}:
        return "ANONYMIZED"
    if result in {"REDACTED"}:
        return "REDACTED"
    http = str(_get(fields, "http_status", "status_code") or "")
    rows = _get(fields, "rows", "total_hits", "total")
    hit = str(_get(fields, "result") or "").lower()
    if http == "404" or (rows is not None and str(rows).strip() == "0") or hit == "miss":
        return "ABSENT"
    if "profile" in fields and fields["profile"] is None and http in {"", "404"}:
        return "ABSENT"
    marker = _truthy(_get(fields, "marker_found"))
    if marker is False and "deletion_marker" in event:
        return "PRESENT"
    if marker is True and "deletion_marker" in event:
        return "ABSENT"
    if event.endswith("_started"):
        return None
    if http == "200" or (rows is not None and str(rows).strip() not in {"", "0"}) or hit == "hit":
        return "PRESENT"
    if event and not any(t in event for t in ("read", "lookup", "query", "scan", "completed", "evaluated")):
        return None
    return "PRESENT"
