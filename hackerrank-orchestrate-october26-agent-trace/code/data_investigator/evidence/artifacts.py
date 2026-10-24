"""PDF and screenshot evidence -> Observations.

PDFs with a text layer are parsed deterministically. Screenshots go through the vision model
(L2), and only guard-accepted rows become observations. Status words are mapped to states by a
fixed table here, never by the model.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from ..models import ManifestEntry, Observation
from ..normalize import EMAIL_RE, RESOURCE_RE, USER_REF_RE, ident
from .interpret import PREFIX_CATEGORY, resource_prefix, system_for

OBJECT_RE = re.compile(r"\bobject_\d+\b")
PHONE_RE = re.compile(r"\+\d[\d ()\-]{7,}\d")


@dataclass
class ArtifactResult:
    observations: list[Observation] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    status: str | None = None


def _obs(entry: ManifestEntry, rid: str, state: str, ids: set, pii: set, category: str | None, note: str,
         system_hint: str | None = None, locator: str = "document") -> Observation | None:
    system = system_for(rid) or system_hint
    if not system:
        return None
    p = resource_prefix(rid)
    return Observation(
        source_id=entry.file_id, source_kind="file", observed_at=entry.captured_at, captured_at=entry.captured_at,
        system_id=system, resource_id=rid, category=category or (PREFIX_CATEGORY.get(p) if p else None),
        state=state, pii=frozenset(pii), identifiers=frozenset(ids), locator=locator, note=note,
    )


def pdf_observations(entry: ManifestEntry, text: str, aliases: dict[str, str]) -> ArtifactResult:
    """A business document that names a resource shows that the record existed when captured."""
    res = ArtifactResult()
    if not text.strip():
        res.status = "PENDING_AI"
        return res
    rids = set(RESOURCE_RE.findall(text))
    for alias, rid in aliases.items():
        if alias in text:
            rids.add(rid)
    ids: set = set()
    pii: set = set()
    for m in EMAIL_RE.findall(text):
        i = ident("email", m)
        if i:
            ids.add(i)
            pii.add("email")
    for m in USER_REF_RE.findall(text):
        i = ident("user_ref", m)
        if i:
            ids.add(i)
    for m in PHONE_RE.findall(text):
        i = ident("phone", m)
        if i:
            ids.add(i)
            pii.add("phone")
    lower = text.lower()
    for rid in sorted(rids):
        cat = None
        if rid.startswith("support_") and "attachment" in lower:
            cat = "SUPPORT_ATTACHMENT"
        o = _obs(entry, rid, "PRESENT", ids, pii, cat, "document text")
        if o:
            res.observations.append(o)
    return res


# Status words seen on console screenshots -> state, by resource family.
_FLOW_ACTIVE = re.compile(r"healthy|continuous|active|running|live|enabled|delivered", re.I)
_FLOW_OFF = re.compile(r"disabled|paused|stopped|inactive", re.I)
_BLOCKED = re.compile(r"restore blocked|blocked|locked|restore disabled|no restore", re.I)
_ABSENT = re.compile(r"deleted|not found|removed|purged|404", re.I)


def status_to_state(rid: str, status: str) -> str:
    p = resource_prefix(rid) or ""
    if p == "data_flow":
        if _FLOW_OFF.search(status):
            return "DISABLED"
        return "ACTIVE" if _FLOW_ACTIVE.search(status) else "ACTIVE"
    if p == "backup":
        return "BLOCKED" if _BLOCKED.search(status) else "RESTORABLE"
    if _ABSENT.search(status) and not re.search(r"retained", status, re.I):
        return "ABSENT"
    return "PRESENT"


def image_observations(entry: ManifestEntry, rows, link_hints: dict[str, tuple[str, str | None]]) -> ArtifactResult:
    res = ArtifactResult()
    for r in rows:
        pii = {k for k, _ in r.identifiers if k != "user_ref"} | ({"name"} if r.names else set())
        sys_hint, cat_hint = link_hints.get(r.resource_id, (None, None))
        ids = set(r.identifiers) | {("name", n) for n in r.names}
        o = _obs(entry, r.resource_id, status_to_state(r.resource_id, r.status_text), ids, pii,
                 cat_hint, f"screenshot: {r.status_text}", system_hint=sys_hint, locator="screenshot")
        if o:
            o.note += f" | names={sorted(r.names)}"
            res.observations.append(o)
    return res
