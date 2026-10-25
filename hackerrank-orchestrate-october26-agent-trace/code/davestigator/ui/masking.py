"""Mask direct identifiers before they reach the terminal or reports."""
from __future__ import annotations

import re

from ..normalize import EMAIL_RE

_PHONE = re.compile(r"\+?\d[\d ()\-]{6,}\d")


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    if not domain:
        return "***"
    return f"{local[:1]}***@{domain}"


def mask_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    return f"***{digits[-2:]}" if digits else "***"


def mask_text(text: str) -> str:
    text = EMAIL_RE.sub(lambda m: mask_email(m.group(0)), text)
    return _PHONE.sub(lambda m: mask_phone(m.group(0)), text)


def mask_identifier(kind: str, value: str) -> str:
    if kind == "email":
        return mask_email(value)
    if kind == "phone":
        return mask_phone(value)
    if kind == "email_hash":
        return value[:6] + "…"
    if kind == "name":
        parts = value.split()
        return " ".join(p[:1] + "." for p in parts) if parts else "***"
    return value
