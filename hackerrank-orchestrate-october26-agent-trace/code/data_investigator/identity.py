"""Deterministic identity resolution: request email -> user_id -> every known identifier."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from .models import AuditEvent, ResolvedIdentity
from .normalize import email_hash, ident

FIELD_KIND = {"email": "email", "phone": "phone", "email_hash": "email_hash", "name": "name"}


class IdentityGraph:
    """Identifier nodes connected to user_ids by snapshot rows and audit-log changes."""

    def __init__(self, identities: list[dict[str, str]], audit_log: list[AuditEvent]):
        # identifier -> {user_id: [(source_id, valid_from)]}
        self.owners: dict[tuple[str, str], dict[str, list[tuple[str, datetime | None]]]] = defaultdict(lambda: defaultdict(list))
        self.user_idents: dict[str, set[tuple[str, str]]] = defaultdict(set)
        self.names: dict[str, str] = {}
        for row in identities:
            uid = row["user_id"]
            self.names[uid] = row["name"]
            self._add(uid, ("user_ref", uid.lower()), "user_identities", None)
            for col, kind in FIELD_KIND.items():
                i = ident(kind, row.get(col, ""))
                if i:
                    self._add(uid, i, "user_identities", None)
            e = ident("email", row.get("email", ""))
            if e:
                self._add(uid, ("email_hash", email_hash(e[1])), "user_identities", None)
        for a in audit_log:
            kind = FIELD_KIND.get(a.field_name)
            if not kind:
                continue
            for v, when in ((a.old_value, None), (a.new_value, a.event_time)):
                i = ident(kind, v)
                if i:
                    self._add(a.user_id, i, a.audit_id, when)
                    if kind == "email":
                        self._add(a.user_id, ("email_hash", email_hash(i[1])), a.audit_id, when)

    def _add(self, uid: str, i: tuple[str, str], source: str, when: datetime | None):
        self.owners[i][uid].append((source, when))
        self.user_idents[uid].add(i)

    def users_for(self, i: tuple[str, str]) -> set[str]:
        return set(self.owners.get(i, {}))

    def resolve(self, email: str) -> ResolvedIdentity:
        e = ident("email", email)
        if not e:
            return ResolvedIdentity(None, ambiguities=["unparseable request email"])
        owners = self.owners.get(e, {})
        if not owners:
            return ResolvedIdentity(None, ambiguities=["request email matches no known user"])
        uids = sorted(owners)
        res = ResolvedIdentity(uids[0])
        if len(uids) > 1:
            # Prefer the user for whom this is the *current* email (snapshot row).
            current = [u for u in uids if any(src == "user_identities" for src, _ in owners[u])]
            res.user_id = current[0] if len(current) == 1 else uids[0]
            res.ambiguities.append(f"email maps to multiple users {uids}; chose {res.user_id}")
        sources = owners[res.user_id]
        res.path.append(
            "current email" if any(s == "user_identities" for s, _ in sources) else f"historical email ({sources[0][0]})"
        )
        # Linking identifiers: exclude names (not unique) — they only corroborate.
        res.identifiers = {i for i in self.user_idents[res.user_id] if i[0] != "name"}
        return res

    def name_of(self, uid: str) -> str | None:
        return self.names.get(uid)
