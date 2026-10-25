"""Schema discovery, personal-data column detection, and mapping drafts.

Sampled values are only used to compute match *rates* in memory; they are never printed,
logged, cached or sent to a model.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..normalize import EMAIL_RE, HASH_RE, USER_REF_RE
from .base import CollectionInfo
from .mapping import CollectionMap

NAME_RULES = [
    ("email_hash", re.compile(r"(email|mail).*(hash|sha)|^(sha256|hash)_?email", re.I)),
    ("email", re.compile(r"e_?mail|^mail$|email_address|owner_email|requester_email|subject_selector", re.I)),
    ("phone", re.compile(r"phone|mobile|msisdn|tel(ephone)?$|^cell", re.I)),
    ("user_ref", re.compile(r"^(user|customer|subject|account|member|client)_?(id|ref|reference|key)$|customer_reference|subject_reference", re.I)),
    ("name", re.compile(r"^(full_?name|display_?name|requester_name|customer_name|first_?name|last_?name|name)$", re.I)),
]
TEXT_RULE = re.compile(r"body|message|text|note|comment|excerpt|description|content", re.I)
PHONE_VALUE = re.compile(r"^\+?[\d\s().\-]{7,20}$")
DATE_LIKE = re.compile(r"^\d{4}-\d{2}-\d{2}|^\d{1,2}[/.]\d{1,2}[/.]\d{2,4}")
TIME_COLUMN = re.compile(r"(_at|_on|date|time|timestamp)$", re.I)

# table-name keywords -> category (first match wins; order matters: specific before generic)
CATEGORY_RULES = [
    ("ACCOUNT_TOMBSTONE", r"tombstone|deleted_(customer|account|user)"),
    ("SUPPORT_ATTACHMENT", r"attachment"),
    ("SUPPORT_MESSAGE", r"support|ticket|conversation|helpdesk|case_message"),
    ("SECURITY_AUDIT_LOG", r"audit|security_event|auth_log|login_history"),
    ("LEGAL_HOLD_RECORD", r"legal_hold|retained_order|litigation"),
    ("BACKUP_ARCHIVE", r"backup|archive|snapshot_catalog|restore"),
    ("DATA_FLOW", r"connector|integration|sync|subscription_feed|webhook|pipeline"),
    ("USER_EXPORT", r"export|download|data_request_file"),
    ("SEARCH_INDEX_ENTRY", r"search|index|directory"),
    ("MARKETING_PROFILE", r"marketing|newsletter|campaign|audience|mailing_list"),
    ("EMAIL_DELIVERY_LOG", r"email_(log|deliver|event)|deliveries|notification_log|mail_log"),
    ("CACHE_ENTRY", r"cache|session"),
    ("PRODUCT_TELEMETRY", r"telemetry|event|clickstream|tracking"),
    ("ANALYTICS_PROFILE", r"analytics|profile_stats|cohort|metric"),
    ("USER_PROFILE", r"customer|user|account|profile|member|client|person"),
]
CATEGORY_SYSTEM = {
    "USER_PROFILE": "account_service", "SEARCH_INDEX_ENTRY": "search_service", "ANALYTICS_PROFILE": "analytics_platform",
    "PRODUCT_TELEMETRY": "analytics_platform", "SUPPORT_MESSAGE": "support_platform", "SUPPORT_ATTACHMENT": "support_platform",
    "USER_EXPORT": "export_service", "DATA_FLOW": "integration_service", "BACKUP_ARCHIVE": "backup_service",
    "SECURITY_AUDIT_LOG": "security_service", "ACCOUNT_TOMBSTONE": "identity_service", "LEGAL_HOLD_RECORD": "legal_service",
    "MARKETING_PROFILE": "marketing_platform", "CACHE_ENTRY": "cache_service", "EMAIL_DELIVERY_LOG": "email_delivery_service",
    "SECONDARY_IDENTIFIER": "account_service",
}
STATE_HINTS = {
    "deleted_when": re.compile(r"^(deleted_at|removed_at|purged_at)$", re.I),
    "active_when": re.compile(r"^(enabled|is_enabled|active|is_active)$", re.I),
    "restorable_when": re.compile(r"^(restore_allowed|restorable|can_restore)$", re.I),
}


@dataclass
class ColumnFinding:
    column: str
    kind: str | None
    rate: float | None
    by: str  # name | values | name+values | text


@dataclass
class CollectionFindings:
    info: CollectionInfo
    columns: list[ColumnFinding] = field(default_factory=list)
    text_fields: list[str] = field(default_factory=list)

    @property
    def has_personal_data(self) -> bool:
        return any(c.kind for c in self.columns) or bool(self.text_fields)

    @property
    def confidence(self) -> str:
        if not self.has_personal_data:
            return "none"
        if any(c.kind in ("email", "phone", "email_hash") and (c.rate or 0) >= 0.8 for c in self.columns):
            return "high"
        return "medium"


def _value_kind_rates(values: list) -> dict[str, float]:
    vals = [str(v).strip() for v in values if v is not None and str(v).strip()]
    if not vals:
        return {}
    n = len(vals)
    return {
        "email": sum(bool(EMAIL_RE.fullmatch(v)) for v in vals) / n,
        "email_hash": sum(bool(HASH_RE.fullmatch(v.lower().removeprefix("sha256:"))) for v in vals) / n,
        "phone": sum(bool(PHONE_VALUE.fullmatch(v)) and not DATE_LIKE.match(v) and sum(c.isdigit() for c in v) >= 7 for v in vals) / n,
        "user_ref": sum(bool(USER_REF_RE.fullmatch(v.lower())) for v in vals) / n,
        "text_email": sum(bool(EMAIL_RE.search(v)) for v in vals) / n,
    }


def analyze(connector, info: CollectionInfo, sample_n: int = 50) -> CollectionFindings:
    f = CollectionFindings(info)
    if not info.readable:
        return f
    for col in info.columns:
        if TIME_COLUMN.search(col.name) or col.type.upper() in ("DATE", "DATETIME", "TIMESTAMP", "INTEGER", "BIGINT", "BOOLEAN") \
                and not re.search(r"(user|customer|subject|account|member|client)", col.name, re.I):
            continue
        name_kind = next((k for k, rx in NAME_RULES if rx.search(col.name)), None)
        is_textish = bool(TEXT_RULE.search(col.name))
        try:
            rates = _value_kind_rates(connector.sample(info.source, col.name, sample_n)) if (name_kind or is_textish or "char" in col.type.lower() or "text" in col.type.lower() or col.type in ("string", "text", "keyword", "str")) else {}
        except Exception:  # noqa: BLE001 - sampling is best-effort; failure leaves name-based detection
            rates = {}
        best = max(((k, r) for k, r in rates.items() if k != "text_email"), key=lambda kr: kr[1], default=(None, 0.0))
        if name_kind and (not rates or rates.get(name_kind, 0) >= 0.5 or name_kind in ("name", "user_ref")):
            rate = None if name_kind in ("name", "user_ref") and not rates.get(name_kind) else rates.get(name_kind)
            f.columns.append(ColumnFinding(col.name, name_kind, rate, "name+values" if rate else "name"))
        elif best[0] and best[1] >= 0.8:
            f.columns.append(ColumnFinding(col.name, best[0], best[1], "values"))
        elif is_textish and rates.get("text_email", 0) > 0:
            f.text_fields.append(col.name)
            f.columns.append(ColumnFinding(col.name, None, rates["text_email"], "text"))
    return f


def guess_category(source: str) -> str | None:
    base = source.split(".")[-1].lower()
    for cat, rx in CATEGORY_RULES:
        if re.search(rx, base):
            return cat
    return None


def draft_collection(f: CollectionFindings) -> CollectionMap | None:
    if not f.has_personal_data:
        return None
    cat = guess_category(f.info.source)
    ids = {}
    for c in f.columns:
        if c.kind and c.kind not in ids:
            ids[c.kind] = c.column
    state = {}
    for key, rx in STATE_HINTS.items():
        col = next((c.name for c in f.info.columns if rx.match(c.name)), None)
        if col:
            state[key] = {"field": col, "not_null": True} if key == "deleted_when" else {"field": col, "equals": True}
    pk = (f.info.primary_key or [next((c.name for c in f.info.columns if c.name.lower() in ("id", "_id", "key")), f.info.columns[0].name if f.info.columns else "id")])[0]
    notes = []
    base = f.info.source.split(".")[-1].lower()
    if cat == "USER_PROFILE" and "user_ref" not in ids:
        ids["user_ref"] = pk
        notes.append(f"primary key '{pk}' proposed as the stable user reference; confirm")
    role = "evidence"
    if re.search(r"^(users?|accounts?|identit(y|ies)|persons?)$|email_history|identity_history|alias", base) and "email" in ids and "user_ref" in ids:
        role = "identity"
        notes.append("proposed as an identity source (resolves current and old identifiers); confirm")
    if not cat and role != "identity":
        notes.append("category not recognised from the name; choose one from deletion_rules.csv")
    return CollectionMap(source=f.info.source, system=CATEGORY_SYSTEM.get(cat or "", "unmapped_system"),
                         category=cat or ("IDENTITY_SOURCE" if role == "identity" else "UNMAPPED"), id=pk, identifiers=ids,
                         text_fields=f.text_fields, state=state, role=role, review="pending", suggested_by="rules",
                         note="; ".join(notes))
