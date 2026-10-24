"""Identifier and timestamp normalization helpers."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
HASH_RE = re.compile(r"\b[0-9a-f]{64}\b")
USER_REF_RE = re.compile(r"\buser_\d+\b")
RESOURCE_RE = re.compile(
    r"\b(?:account|search|analytics|support|export|data_flow|backup|audit_record|tombstone|"
    r"legal_hold|cache|marketing|email_delivery|object)_\d+\b"
)

NULLISH = {"", "null", "none", "nil", "n/a", "-", "[redacted]", "redacted"}


def parse_ts(value: str | None) -> datetime | None:
    if value is None:
        return None
    v = str(value).strip()
    if not v or v.lower() in NULLISH:
        return None
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        # Dataset convention: all timestamps are UTC ("Z"). A naive stamp is treated as UTC.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def is_null(value) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    return str(value).strip().lower() in NULLISH


def norm_email(v: str) -> str:
    return v.strip().lower()


def norm_phone(v: str) -> str:
    return re.sub(r"\D", "", v)


def norm_name(v: str) -> str:
    v = unicodedata.normalize("NFKC", v).casefold()
    return re.sub(r"\s+", " ", v).strip()


def email_hash(email: str) -> str:
    return hashlib.sha256(norm_email(email).encode()).hexdigest()


def ident(kind: str, value: str) -> tuple[str, str] | None:
    """Build a normalized (kind, value) identifier, or None for empty values."""
    if is_null(value):
        return None
    value = str(value)
    if kind == "email":
        m = EMAIL_RE.search(value)
        return ("email", norm_email(m.group(0))) if m else None
    if kind == "phone":
        digits = norm_phone(value)
        return ("phone", digits) if len(digits) >= 7 else None
    if kind == "email_hash":
        v = value.strip().lower()
        if v.startswith("sha256:"):
            v = v[7:]
        return ("email_hash", v) if HASH_RE.fullmatch(v) else None
    if kind == "user_ref":
        m = USER_REF_RE.search(value.lower())
        return ("user_ref", m.group(0)) if m else None
    if kind == "name":
        n = norm_name(value)
        return ("name", n) if n else None
    raise ValueError(kind)


def classify_value(value: str) -> tuple[str, str] | None:
    """Best-effort classification of a free-form lookup key (email / user ref / hash / phone)."""
    if is_null(value):
        return None
    s = str(value).strip()
    if EMAIL_RE.fullmatch(s):
        return ident("email", s)
    if USER_REF_RE.fullmatch(s.lower()):
        return ident("user_ref", s)
    if HASH_RE.fullmatch(s.lower()):
        return ident("email_hash", s)
    if re.fullmatch(r"\+?[\d\s().\-]{7,}", s):
        return ident("phone", s)
    return None
