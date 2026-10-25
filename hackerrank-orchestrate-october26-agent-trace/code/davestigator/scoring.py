"""Compare predicted rows with labelled rows (sample_output.csv or custom cases)."""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field

FIELDS = ("phase", "system_id", "resource_id", "operation", "state", "evidence_id")


@dataclass
class Score:
    cases: int = 0
    status_correct: int = 0
    exact_rows: int = 0
    gold_steps: int = 0
    pred_steps: int = 0
    matched_steps: int = 0  # (operation, system, resource) present in both
    matched_with_evidence: int = 0
    field_hits: Counter = field(default_factory=Counter)
    field_total: int = 0
    sequence_correct: int = 0
    confusion: Counter = field(default_factory=Counter)
    failures: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        p = self.matched_steps / self.pred_steps if self.pred_steps else (1.0 if not self.gold_steps else 0.0)
        r = self.matched_steps / self.gold_steps if self.gold_steps else (1.0 if not self.pred_steps else 0.0)
        return {
            "cases": self.cases,
            "status_accuracy": self.status_correct / self.cases if self.cases else 0.0,
            "exact_row_accuracy": self.exact_rows / self.cases if self.cases else 0.0,
            "step_precision": p,
            "step_recall": r,
            "step_f1": 2 * p * r / (p + r) if p + r else 0.0,
            "step_evidence_accuracy": self.matched_with_evidence / self.matched_steps if self.matched_steps else 0.0,
            "field_accuracy": {f: self.field_hits[f] / self.field_total for f in FIELDS} if self.field_total else {},
            "sequence_accuracy": self.sequence_correct / self.cases if self.cases else 0.0,
            "confusion": {f"{g}->{p}": n for (g, p), n in sorted(self.confusion.items())},
        }


def _key(s: dict) -> tuple:
    return (s["operation"], s["system_id"], s["resource_id"])


def score(gold: dict[str, tuple[str, list]], pred: dict[str, tuple[str, list]]) -> Score:
    sc = Score()
    for cid, (gstatus, gsteps) in sorted(gold.items()):
        sc.cases += 1
        pstatus, psteps = pred.get(cid, ("MISSING", []))
        sc.confusion[(gstatus, pstatus)] += 1
        sc.status_correct += gstatus == pstatus
        norm = lambda steps: [{k: s[k] for k in ("step_no",) + FIELDS} for s in steps]
        exact = gstatus == pstatus and norm(gsteps) == norm(psteps)
        sc.exact_rows += exact
        sc.gold_steps += len(gsteps)
        sc.pred_steps += len(psteps)
        pk = {_key(s): s for s in psteps}
        for g in gsteps:
            p = pk.get(_key(g))
            if p:
                sc.matched_steps += 1
                sc.matched_with_evidence += p["evidence_id"] == g["evidence_id"]
        # field accuracy: positional comparison
        for i, g in enumerate(gsteps):
            sc.field_total += 1
            p = psteps[i] if i < len(psteps) else {}
            for f in FIELDS:
                sc.field_hits[f] += p.get(f) == g[f]
        sc.sequence_correct += [_key(s) for s in gsteps] == [_key(s) for s in psteps]
        if not exact:
            gset, pset = {_key(s) for s in gsteps}, {_key(s) for s in psteps}
            sc.failures.append({
                "case_id": cid, "gold_status": gstatus, "pred_status": pstatus,
                "missing": sorted(gset - pset), "extra": sorted(pset - gset),
                "evidence_diff": sorted(
                    (k, pk[k]["evidence_id"], g["evidence_id"]) for g in gsteps
                    for k in [_key(g)] if k in pk and pk[k]["evidence_id"] != g["evidence_id"]
                ),
            })
    return sc


def load_rows(path) -> dict[str, tuple[str, list]]:
    import csv

    with open(path, newline="", encoding="utf-8") as fh:
        return {r["case_id"]: (r["deletion_status"], json.loads(r["steps_json"])) for r in csv.DictReader(fh)}
