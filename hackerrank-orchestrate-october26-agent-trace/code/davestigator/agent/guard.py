"""LLM output guard: the only path from model text to facts the engine may use.

raw JSON -> schema/enum check -> quote grounding -> task-specific acceptance -> typed result.
Every response is recorded in an audit list (accepted or rejected, with the reason).
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from ..intent import INTENTS
from ..normalize import RESOURCE_RE, classify_value, ident

OBJECT_RE = re.compile(r"\bobject_\d+\b")


def _canon(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).replace("’", "'").casefold()
    return re.sub(r"\s+", " ", s).strip()


def quote_grounded(quote: str, source: str) -> bool:
    q = _canon(quote or "")
    return bool(q) and len(q) >= 3 and q in _canon(source)


@dataclass
class GuardLog:
    entries: list[dict] = field(default_factory=list)

    def record(self, task: str, verdict: str, detail: dict):
        self.entries.append({"task": task, "verdict": verdict, **detail})

    def dump(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            for e in self.entries:
                fh.write(json.dumps(e, sort_keys=True, ensure_ascii=False) + "\n")


def check_intent(out: dict | None, text: str) -> tuple[str | None, str, str]:
    """Return (intent, quote, verdict)."""
    if not isinstance(out, dict) or set(out) - {"intent", "quote"}:
        return None, "", "rejected:schema"
    intent = str(out.get("intent", "")).upper()
    if intent not in INTENTS:
        return None, "", "rejected:enum"
    quote = str(out.get("quote", ""))
    if not quote_grounded(quote, text):
        return None, quote, "rejected:ungrounded_quote"
    return intent, quote, "accepted"


@dataclass
class ExtractedRow:
    resource_id: str
    identifiers: set[tuple[str, str]]
    names: set[str]
    status_text: str
    quote: str


def check_artifact_rows(out: dict | None, grounded_resources: set[str], known_identifiers: set[tuple[str, str]],
                        log: GuardLog, file_id: str, aliases: dict[str, str] | None = None) -> list[ExtractedRow]:
    """Accept a row only if its resource id is grounded elsewhere in the dataset.

    Identifiers are kept only when they exactly match an identifier known to the identity graph
    (emails/phones/hashes/user refs); anything else the model read is dropped, never invented into
    a link. Person names are kept separately as exposure markers only.
    """
    if not isinstance(out, dict) or not isinstance(out.get("rows"), list):
        log.record("extract_artifact", "rejected:schema", {"file_id": file_id})
        return []
    rows: list[ExtractedRow] = []
    for raw in out["rows"]:
        if not isinstance(raw, dict):
            continue
        rid = str(raw.get("resource_id", "")).strip().lower().replace(" ", "_")
        # Consoles often show a catalog alias (file name, statement number, customer key) instead
        # of the resource id; map it through the data catalog's own alias, never by guessing.
        for alias, target in (aliases or {}).items():
            a = alias.lower()
            if rid.replace(".", "_") == a.replace(".", "_"):
                rid = target
                break
            in_quote = re.search(rf"(?<![\w.-]){re.escape(a)}(?![\w-])", str(raw.get("quote", "")).lower())
            if rid == a or rid == a.replace(" ", "_") or in_quote:
                rid = target
                break
        if not (RESOURCE_RE.fullmatch(rid) or OBJECT_RE.fullmatch(rid)):
            # Labels like "user_20 · analytics_7438" sometimes come back fused into one token;
            # accept only when exactly one resource id is embedded.
            embedded = set(re.findall(r"(?:account|search|analytics|support|export|data_flow|backup|audit_record|"
                                      r"tombstone|legal_hold|cache|marketing|email_delivery|object)_\d+(?!\d)", rid))
            if len(embedded) == 1:
                rid = embedded.pop()
        if not (RESOURCE_RE.fullmatch(rid) or OBJECT_RE.fullmatch(rid)):
            log.record("extract_artifact", "rejected:bad_resource_id", {"file_id": file_id, "resource_id": rid})
            continue
        if rid not in grounded_resources:
            log.record("extract_artifact", "rejected:ungrounded_resource", {"file_id": file_id, "resource_id": rid})
            continue
        ids: set[tuple[str, str]] = set()
        names: set[str] = set()
        dropped = 0
        for v in raw.get("identifiers") or []:
            i = classify_value(str(v))
            if i and i in known_identifiers:
                ids.add(i)
            elif i is None and re.fullmatch(r"[^\W\d_][\w'\-. ]{2,60}", str(v).strip()):
                n = ident("name", str(v))
                if n:
                    names.add(n[1])
            else:
                dropped += 1
        rows.append(ExtractedRow(rid, ids, names, str(raw.get("status_text", ""))[:80], str(raw.get("quote", ""))[:160]))
        log.record("extract_artifact", "accepted", {"file_id": file_id, "resource_id": rid,
                                                    "identifier_kinds": sorted(k for k, _ in ids),
                                                    "dropped_unknown_identifiers": dropped})
    return rows
