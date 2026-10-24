"""Per-case reports (JSON / Markdown / CSV) and the usage report. Identifiers are masked."""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from pathlib import Path

from .engine import CaseResult
from .ui.masking import mask_email, mask_text

SEVERITY = {"REPORT_REMAINING_RECORD": "High", "REPORT_EXPOSED_PII": "High",
            "REPORT_ACTIVE_DATA_FLOW": "High", "REPORT_RESTORE_RISK": "High"}
FINDING_TEXT = {
    "REPORT_REMAINING_RECORD": "A record that policy requires to be deleted was still present at audit time.",
    "REPORT_EXPOSED_PII": "A retained record still exposed direct identifiers that policy requires to be removed.",
    "REPORT_ACTIVE_DATA_FLOW": "A data flow that can copy or recreate the subject's data was still enabled.",
    "REPORT_RESTORE_RISK": "A backup containing the subject's data could still be restored.",
}


def _ts(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def case_to_dict(r: CaseResult) -> dict:
    q = r.request
    return {
        "case_id": r.case_id,
        "request": {"email": mask_email(q.email), "requested_at": _ts(q.requested_at), "reported_status": q.reported_status,
                    "reported_at": _ts(q.reported_at), "audit_at": _ts(q.audit_at)},
        "intent": None if r.intent is None else {"label": r.intent.intent, "source": r.intent.source, "note": r.intent.note},
        "identity": None if r.identity is None else {
            "user_id": r.identity.user_id, "path": r.identity.path, "ambiguities": r.identity.ambiguities,
            "identifier_kinds": sorted({k for k, _ in r.identity.identifiers})},
        "internal_status": r.internal_status,
        "deletion_status": r.deletion_status,
        "obligations": [
            {"system_id": o.system_id, "resource_id": o.resource_id, "category": o.category, "rule_id": o.treatment.rule_id,
             "required_state": o.treatment.required_state, "state_at_audit": o.state, "exposed_identifier_kinds": sorted(o.pii),
             "satisfied": o.satisfied, "evidence_id": o.evidence.source_id if o.evidence else None,
             "evidence_observed_at": _ts(o.evidence.observed_at) if o.evidence else None,
             "linked_by": o.link_sources}
            for o in r.obligations],
        "timeline": sorted(
            ({"at": _ts(h.observed_at), "source_id": h.source_id, "kind": "CLAIM" if h.source_kind == "activity" else "OBSERVED",
              "system_id": h.system_id, "resource_id": h.resource_id, "state": h.state}
             for o in r.obligations for h in o.history),
            key=lambda e: (e["at"] or "", e["source_id"])),
        "steps": [s.to_json() for s in r.steps],
        "gaps": [mask_text(g) for g in r.gaps],
        "excluded_after_audit": sorted({o.source_id for o in r.excluded_after_audit}),
    }


def case_markdown(r: CaseResult) -> str:
    d = case_to_dict(r)
    lines = [f"# Audit report — {r.case_id}", "", f"**Status:** {r.deletion_status}  ",
             f"**Audit time:** {d['request']['audit_at']}  ", f"**Requester:** {d['request']['email']}  ",
             f"**Reported status (claim):** {d['request']['reported_status']}", ""]
    if d["intent"]:
        lines += [f"**Request intent:** {d['intent']['label']} ({d['intent']['source']})", ""]
    if d["identity"]:
        lines += ["## Identity", f"- user_id: `{d['identity']['user_id']}` via {', '.join(d['identity']['path'])}",
                  f"- identifier kinds: {', '.join(d['identity']['identifier_kinds'])}", ""]
    if d["obligations"]:
        lines += ["## Obligations", "| System | Resource | Category | Required | At audit | OK | Evidence |", "|---|---|---|---|---|---|---|"]
        for o in d["obligations"]:
            ok = {True: "yes", False: "NO", None: "?"}[o["satisfied"]]
            lines.append(f"| {o['system_id']} | {o['resource_id']} | {o['category']} | {o['required_state']} | "
                         f"{o['state_at_audit']} | {ok} | {o['evidence_id'] or ''} |")
        lines.append("")
    if d["steps"]:
        lines += ["## Steps", "| # | Phase | Operation | System | Resource | State | Evidence |", "|---|---|---|---|---|---|---|"]
        for s in d["steps"]:
            lines.append(f"| {s['step_no']} | {s['phase']} | {s['operation']} | {s['system_id']} | {s['resource_id']} | "
                         f"{s['state']} | {s['evidence_id']} |")
        lines.append("")
    if d["gaps"]:
        lines += ["## Gaps"] + [f"- {g}" for g in d["gaps"]] + [""]
    return "\n".join(lines)


def case_csv(r: CaseResult) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["case_id", "step_no", "phase", "operation", "system_id", "resource_id", "state", "evidence_id"])
    for s in r.steps:
        w.writerow([r.case_id, s.step_no, s.phase.value, s.operation, s.system_id, s.resource_id, s.state, s.evidence_id])
    return buf.getvalue()


def save_case(r: CaseResult, directory: Path, fmt: str = "json") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    if fmt == "markdown":
        p = directory / f"{r.case_id}.md"
        p.write_text(case_markdown(r), encoding="utf-8")
    elif fmt == "csv":
        p = directory / f"{r.case_id}.csv"
        p.write_text(case_csv(r), encoding="utf-8")
    else:
        p = directory / f"{r.case_id}.json"
        p.write_text(json.dumps(case_to_dict(r), indent=2, sort_keys=True), encoding="utf-8")
    return p


def usage_markdown(meter_summary: dict, cases: int, model: str, digest: str, provider: str, ai_status: str,
                   run_label: str, guard_counts: dict) -> str:
    tot = {"calls": 0, "cached": 0, "live": 0, "input_tokens": 0, "output_tokens": 0, "latency_s": 0.0}
    rows = []
    for (m, task), v in sorted(meter_summary.items()):
        for k in tot:
            tot[k] += v[k]
        rows.append(f"| {provider} | {m} | {task} | {v['calls']} | {v['live']} | {v['cached']} | {v['input_tokens']:,} | "
                    f"{v['output_tokens']:,} | {v['input_tokens'] + v['output_tokens']:,} | {v['latency_s']:.1f} | "
                    f"{v['accepted']} | {v['rejected']} |")
    total_tokens = tot["input_tokens"] + tot["output_tokens"]
    per_req = total_tokens / cases if cases else 0
    out = [
        "# Usage report — final full-dataset run", "",
        f"- Run: {run_label}",
        f"- Requests processed: {cases}",
        f"- AI assistance: {ai_status}",
        f"- Provider / model: {provider} / `{model}` (digest `{digest[:19]}`)" if model else "- Provider / model: none",
        "- Token counts are the provider's own counters (Ollama `prompt_eval_count` / `eval_count`), recorded per call; "
        "cached replays report the tokens of the original live call and add no new cost.",
        "",
        "## Per model and task", "",
        "| Provider | Model | Task | Calls | Live | Cached | Input tokens | Output tokens | Total tokens | Live latency (s) | Guard accepted | Guard rejected |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
        *rows,
        f"| **all** | | | **{tot['calls']}** | {tot['live']} | {tot['cached']} | **{tot['input_tokens']:,}** | "
        f"**{tot['output_tokens']:,}** | **{total_tokens:,}** | {tot['latency_s']:.1f} | | |",
        "",
        "## Totals", "",
        f"- Total tokens: **{total_tokens:,}** (input {tot['input_tokens']:,}, output {tot['output_tokens']:,})",
        f"- Average tokens per request: **{per_req:,.1f}** over {cases} requests",
        "- Estimated cost: **$0.00 total, $0.00 per request** — the model runs locally through Ollama on CPU, so there is "
        "no per-token charge. Marginal cost is local compute only (wall-clock latency shown above).",
        "",
        "## Guard outcomes", "",
        *[f"- {k}: {v}" for k, v in sorted(guard_counts.items())],
        "",
    ]
    return "\n".join(out)
