"""Request-intent classification: does request_text authorize deletion?

The heuristic classifier is deterministic and multilingual. An LLM classifier can be
layered on top (see agent/), but its label is only accepted through the guard.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

INTENTS = ("DELETION", "ACCESS", "EXPORT", "CORRECTION", "OPT_OUT", "OTHER")


@dataclass(frozen=True)
class IntentLabel:
    intent: str
    source: str  # heuristic | llm | llm+heuristic
    quote: str = ""
    note: str = ""

    @property
    def is_deletion(self) -> bool:
        return self.intent == "DELETION"


# Explicit statements that the person is *not* asking for deletion, or wants the account kept.
_KEEP = [
    r"\bnot asking for deletion\b", r"\bnot a (?:deletion )?request to (?:delete|erase|remove|close)\b",
    r"\bnot (?:asking|requesting) (?:you )?to (?:delete|erase|remove|close)\b", r"\bno (?:deletion|erasure) (?:is )?(?:requested|needed)\b", r"\bdo not (?:delete|close|remove)\b", r"\bdon'?t (?:delete|close|remove)\b",
    r"\bplease don'?t delete\b", r"\bkeep the account\b", r"\bleave (?:the|my) (?:account|profile)\b",
    r"\bcontinue using\b", r"\bwithout removing\b", r"\bstill (?:use|want|need) (?:it|the account|receipts)\b",
    r"\bi still use it\b", r"\baccount active\b",
    r"\bno (?:borr|elimin)\w*\b", r"\bnão (?:apag|exclu)\w*\b", r"\bne (?:supprimez|fermez) pas\b",
    r"\bnicht (?:löschen|schließen)\b", r"削除しないで", r"삭제하지 마",
]
_NON_DELETION = {
    "EXPORT": [r"\bdownload\b", r"\bportable\b", r"\bcopy of\b", r"\bexport\b(?! file)", r"\bdata portability\b"],
    "ACCESS": [r"\bwhat (?:personal )?(?:data|information) (?:do )?you (?:hold|have|store|keep)\b",
               r"\binformation request\b", r"\btell me what\b", r"\bsend me (?:a copy|the information)\b", r"\bwhat (?:data|information) (?:do )?you hold\b", r"\bcan'?t access\b", r"\breset(?:ting)? the login\b", r"\binvoices?\b"],
    "CORRECTION": [r"\bmisspelled\b", r"\bcorrect (?:it|my)\b", r"\bout of date\b", r"\breplace it\b", r"\bchange the email\b", r"\bupdate (?:my|the) (?:email|phone|address|name)\b"],
    "OPT_OUT": [r"\bmarketing lists?\b", r"\bpromotional emails?\b", r"\bunsubscribe\b", r"\bnewsletter\b", r"\bstop (?:the )?(?:promotional|marketing)\b"],
}
# Deletion verbs/nouns across the languages present in the requests.
_DELETE = [
    r"\bdelet\w*", r"\berase\w*|\berasure\b", r"\bremov\w*", r"\bclose (?:my|this|the) account\b", r"\bclosed my account\b",
    r"\bclose\w* (?:it|completely)\b", r"\bpermanently\b", r"\bdisconnect\w*",
    r"\bsupprim\w*|\bsuppression\b", r"\beffac\w*", r"\belimin\w*", r"\bborra\w*|\bborrado\b", r"\bexclus[aã]o\b|\bexclu[ií]\w*",
    r"\bapag\w*", r"\blösch\w*|\bloesch\w*", r"\bentfern\w*", r"\bcancell\w*|\bcancela\w*", r"\bverwijder\w*",
    r"削除", r"消去", r"삭제", r"탈퇴", r"删除", r"注销", r"удал\w*", r"\busu[nń]\w*", r"\bsil\w*\b",
    r"\b(?:profile|account) (?:is|was) (?:gone|deleted|removed)\b", r"\bfully removed\b", r"\balready been removed\b",
    r"\baudit the deletion\b", r"\bcerrar\b|\bfermer\b|\bchiudere\b", r"\bwyczy\w*", r"\bhapus\w*", r"\bxóa\b", r"\bmaz\w*",
]


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text).replace("’", "'").casefold()


class HeuristicIntentClassifier:
    name = "heuristic"

    def classify(self, text: str) -> IntentLabel:
        t = _norm(text)
        for pat in _KEEP:
            m = re.search(pat, t)
            if m:
                kind = self._non_deletion_kind(t) or "OTHER"
                return IntentLabel(kind, "heuristic", m.group(0), "explicit keep/do-not-delete statement")
        nd = self._non_deletion_kind(t)
        dm = None
        for pat in _DELETE:
            dm = re.search(pat, t)
            if dm:
                break
        if dm and not nd:
            return IntentLabel("DELETION", "heuristic", dm.group(0))
        if dm and nd:
            # Both present: deletion wins only if the deletion verb targets the account/data,
            # e.g. "delete that export file as well" after closing the account.
            if re.search(r"\b(account|profile|personal (?:data|details|information))\b", t):
                return IntentLabel("DELETION", "heuristic", dm.group(0), f"also mentions {nd}")
            return IntentLabel(nd, "heuristic", "", "non-deletion cue without account deletion")
        if nd:
            return IntentLabel(nd, "heuristic")
        return IntentLabel("OTHER", "heuristic", "", "no deletion cue found")

    @staticmethod
    def _non_deletion_kind(t: str) -> str | None:
        for kind, pats in _NON_DELETION.items():
            for p in pats:
                if re.search(p, t):
                    return kind
        return None


_EN_DELETE = re.compile(r"\b(delete|deletion|deleted|deleting|erase|erased|erasure|remove|removal|close (?:my|this) account)\b", re.I)


def heuristic_confident(text: str, label: IntentLabel) -> bool:
    """Unambiguous English deletion requests skip the model (call budget); every non-deletion
    label, every non-English request and every mixed-cue request is sent for a second opinion."""
    return label.is_deletion and not label.note and bool(_EN_DELETE.search(text))


class IntentClassifier:
    """Facade used by the engine: heuristic, optionally confirmed/overridden by a guarded LLM."""

    def __init__(self, llm=None):
        self.heuristic = HeuristicIntentClassifier()
        self.llm = llm
        self.cache: dict[str, IntentLabel] = {}

    def classify(self, text: str) -> IntentLabel:
        if text in self.cache:
            return self.cache[text]
        h = self.heuristic.classify(text)
        label = h
        if self.llm is not None and not heuristic_confident(text, h):
            label = self.llm.classify(text, h)
        self.cache[text] = label
        return label
