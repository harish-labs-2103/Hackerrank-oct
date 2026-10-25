"""Interpret system_activity rows.

Most activity rows are workflow *claims* ("Policy action completed for X") and never prove
a resulting state. A few rows are themselves observations of state — explicit checks that
a flow "remains enabled" or an archive "can still be restored", and connector/restore jobs
that "repopulated" a resource. Those become Observations citeable by activity_id.
"""
from __future__ import annotations

import re

from .evidence.interpret import PREFIX_CATEGORY, resource_prefix, system_for
from .models import Activity, Observation
from .normalize import EMAIL_RE, USER_REF_RE, ident

_LINK_RE = re.compile(r"linked (\S+) \(([^)]*)\) to (\S+?) and (user_\d+)")
_ATTACH_RE = re.compile(r"confirmed (\S+) \(([^)]*)\) belongs only to (\S+?) and (user_\d+)")


def activity_observations(acts: list[Activity]) -> list[Observation]:
    out: list[Observation] = []
    for a in acts:
        state = None
        d = a.details.lower()
        if a.event_type == "CHECK_DATA_FLOW" and "remains enabled" in d:
            state = "ACTIVE"
        elif a.event_type == "CHECK_RESTORE_ACCESS" and "can still be restored" in d:
            state = "RESTORABLE"
        elif "repopulated" in d:  # SYNC_RECORD / RESTORE_BACKUP recreated the resource
            state = "PRESENT"
        if state is None:
            continue
        system = system_for(a.resource_id) or a.target_system
        p = resource_prefix(a.resource_id)
        out.append(
            Observation(
                source_id=a.activity_id, source_kind="activity", observed_at=a.event_time,
                captured_at=a.event_time, system_id=system, resource_id=a.resource_id,
                category=PREFIX_CATEGORY.get(p) if p else None, state=state,
                note=f"{a.event_type}: {a.details}",
            )
        )
    return out


def identity_links(acts: list[Activity]) -> list[tuple[Activity, str, set[tuple[str, str]], str | None]]:
    """LINK_RESOURCE_IDENTITY rows -> (activity, resource_id, identifiers, category_hint)."""
    out = []
    for a in acts:
        if a.event_type != "LINK_RESOURCE_IDENTITY":
            continue
        ids: set[tuple[str, str]] = set()
        for m in EMAIL_RE.findall(a.details):
            i = ident("email", m)
            if i:
                ids.add(i)
        for m in USER_REF_RE.findall(a.details):
            i = ident("user_ref", m)
            if i:
                ids.add(i)
        hint = "SUPPORT_ATTACHMENT" if "attachment catalog" in a.details.lower() and "belongs only to" in a.details.lower() else None
        out.append((a, a.resource_id, ids, hint))
    return out


# Which remediation a deletion-worker row attempted, keyed by resource. Used only as a
# category hint (e.g. an analytics contact that the workflow tried to DELETE rather than
# ANONYMIZE is a marketing-type record), never as proof of state.
def attempted_actions(acts: list[Activity]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for a in acts:
        if a.source_system == "deletion_worker" and a.event_type in {"DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD"}:
            out.setdefault(a.resource_id, set()).add(a.event_type)
    return out
