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


def case_html(r: CaseResult) -> str:
    d = case_to_dict(r)
    status = r.deletion_status
    status_color = "#22c55e" if status == "COMPLETE" else ("#ef4444" if status == "INCOMPLETE" else "#94a3b8")
    status_bg = "rgba(34, 197, 94, 0.15)" if status == "COMPLETE" else ("rgba(239, 68, 68, 0.15)" if status == "INCOMPLETE" else "rgba(148, 163, 184, 0.15)")

    obligations_html = ""
    if d["obligations"]:
        rows = []
        for o in d["obligations"]:
            is_ok = o["satisfied"]
            badge_color = "#22c55e" if is_ok else ("#ef4444" if is_ok is False else "#eab308")
            badge_text = "PASSED" if is_ok else ("VIOLATION" if is_ok is False else "UNVERIFIED")
            rows.append(f"""
            <tr>
              <td><code>{o['system_id']}</code></td>
              <td><code>{o['resource_id']}</code></td>
              <td>{o['category']}</td>
              <td><code>{o['required_state']}</code></td>
              <td><code>{o['state_at_audit'] or 'None'}</code></td>
              <td><span class="badge" style="color: {badge_color}; border: 1px solid {badge_color};">{badge_text}</span></td>
              <td><code>{o['evidence_id'] or '-'}</code></td>
            </tr>
            """)
        obligations_html = f"""
        <div class="card">
          <h2>Obligations & Evaluated Treatments</h2>
          <table>
            <thead>
              <tr>
                <th>System</th>
                <th>Resource</th>
                <th>Category</th>
                <th>Required</th>
                <th>Observed At Audit</th>
                <th>Verdict</th>
                <th>Evidence ID</th>
              </tr>
            </thead>
            <tbody>
              {"".join(rows)}
            </tbody>
          </table>
        </div>
        """

    steps_html = ""
    if d["steps"]:
        rows = []
        for s in d["steps"]:
            phase_color = "#ef4444" if s["phase"] == "FINDING" else ("#38bdf8" if s["phase"] == "REMEDIATION" else "#22c55e")
            rows.append(f"""
            <tr>
              <td><strong>{s['step_no']}</strong></td>
              <td><span class="badge" style="color: {phase_color}; border: 1px solid {phase_color};">{s['phase']}</span></td>
              <td><code>{s['operation']}</code></td>
              <td><code>{s['system_id']}</code></td>
              <td><code>{s['resource_id']}</code></td>
              <td><code>{s['state']}</code></td>
              <td><code>{s['evidence_id'] or '-'}</code></td>
            </tr>
            """)
        steps_html = f"""
        <div class="card">
          <h2>Certified Action Plan (steps_json)</h2>
          <table>
            <thead>
              <tr>
                <th>#</th>
                <th>Phase</th>
                <th>Operation</th>
                <th>System</th>
                <th>Resource</th>
                <th>Required State</th>
                <th>Evidence</th>
              </tr>
            </thead>
            <tbody>
              {"".join(rows)}
            </tbody>
          </table>
        </div>
        """

    timeline_html = ""
    if d["timeline"]:
        items = []
        for t in d["timeline"]:
            is_claim = t["kind"] == "CLAIM"
            tag_color = "#38bdf8" if is_claim else "#ec4899"
            items.append(f"""
            <div class="timeline-item">
              <span class="timeline-dot" style="background: {tag_color};"></span>
              <div class="timeline-content">
                <span class="timeline-time">{t['at'] or '-'}</span>
                <span class="badge" style="color: {tag_color}; border: 1px solid {tag_color};">{t['kind']}</span>
                <code>{t['system_id']}/{t['resource_id']}</code> - State: <code>{t['state']}</code>
                <span class="muted">via {t['source_id']}</span>
              </div>
            </div>
            """)
        timeline_html = f"""
        <div class="card">
          <h2>Forensic Evidence Timeline</h2>
          <div class="timeline">
            {"".join(items)}
          </div>
        </div>
        """

    identity_info = ""
    if d["identity"]:
        identity_info = f"""
        <div class="card">
          <h2>Subject Identity Resolution</h2>
          <div class="grid">
            <div>
              <span class="muted">Internal User ID</span>
              <p><code>{d['identity']['user_id']}</code></p>
            </div>
            <div>
              <span class="muted">Resolution Path</span>
              <p>{', '.join(d['identity']['path'])}</p>
            </div>
            <div>
              <span class="muted">Identifier Kinds</span>
              <p>{', '.join(d['identity']['identifier_kinds'])}</p>
            </div>
          </div>
        </div>
        """

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Data Investigator Audit Report — {r.case_id}</title>
  <style>
    :root {{
      --bg: #0b0f19;
      --card: #111827;
      --border: #1f2937;
      --text: #f9fafb;
      --muted: #9ca3af;
      --accent: #38bdf8;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      padding: 2.5rem 1rem;
      line-height: 1.5;
    }}
    .container {{ max-width: 1000px; margin: 0 auto; }}
    header {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 1.75rem 2rem;
      margin-bottom: 2rem;
      display: flex;
      justify-content: space-between;
      align-items: center;
      box-shadow: 0 4px 20px rgba(0, 0, 0, 0.4);
    }}
    .title-group h1 {{ font-size: 1.5rem; font-weight: 700; color: var(--accent); }}
    .title-group p {{ color: var(--muted); font-size: 0.875rem; margin-top: 0.25rem; }}
    .status-stamp {{
      padding: 0.6rem 1.4rem;
      border-radius: 9999px;
      font-weight: 700;
      font-size: 1rem;
      letter-spacing: 0.05em;
      color: {status_color};
      background: {status_bg};
      border: 1.5px solid {status_color};
    }}
    .card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 1.5rem;
      margin-bottom: 1.5rem;
      box-shadow: 0 2px 8px rgba(0, 0, 0, 0.2);
    }}
    .card h2 {{
      font-size: 1.15rem;
      margin-bottom: 1rem;
      color: var(--accent);
      border-bottom: 1px solid var(--border);
      padding-bottom: 0.5rem;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 1rem;
    }}
    .muted {{ color: var(--muted); font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.05em; }}
    code {{
      background: rgba(255, 255, 255, 0.07);
      padding: 0.2rem 0.4rem;
      border-radius: 4px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 0.85em;
      color: #e0f2fe;
    }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; margin-top: 0.5rem; }}
    th, td {{ padding: 0.75rem 0.6rem; text-align: left; border-bottom: 1px solid var(--border); }}
    th {{ color: var(--muted); font-size: 0.75rem; text-transform: uppercase; font-weight: 600; }}
    .badge {{
      display: inline-block;
      padding: 0.15rem 0.5rem;
      border-radius: 9999px;
      font-size: 0.75rem;
      font-weight: 600;
    }}
    .timeline {{ position: relative; padding-left: 1.5rem; border-left: 2px solid var(--border); margin-left: 0.5rem; }}
    .timeline-item {{ position: relative; margin-bottom: 1.25rem; }}
    .timeline-dot {{
      position: absolute;
      left: -1.85rem;
      top: 0.35rem;
      width: 10px;
      height: 10px;
      border-radius: 50%;
    }}
    .timeline-time {{ font-size: 0.8rem; color: var(--muted); margin-right: 0.5rem; }}
    footer {{
      text-align: center;
      color: var(--muted);
      font-size: 0.8rem;
      margin-top: 2.5rem;
    }}
    @media print {{
      body {{ background: #fff; color: #000; padding: 0; }}
      .card, header {{ background: #fff; border: 1px solid #ccc; box-shadow: none; color: #000; }}
      code {{ background: #eee; color: #000; }}
    }}
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div class="title-group">
        <h1>Deletion Audit Certificate: {r.case_id}</h1>
        <p>Grounded Deletion Verification Report & Forensic Audit Trail</p>
      </div>
      <div class="status-stamp">{status}</div>
    </header>

    <div class="card">
      <h2>Audit Parameters</h2>
      <div class="grid">
        <div>
          <span class="muted">Requester</span>
          <p><code>{d['request']['email']}</code></p>
        </div>
        <div>
          <span class="muted">Requested At</span>
          <p>{d['request']['requested_at'] or '-'}</p>
        </div>
        <div>
          <span class="muted">Reported Status</span>
          <p><code>{d['request']['reported_status']}</code></p>
        </div>
        <div>
          <span class="muted">Audit Cutoff Boundary</span>
          <p>{d['request']['audit_at'] or '-'}</p>
        </div>
      </div>
    </div>

    {identity_info}
    {obligations_html}
    {steps_html}
    {timeline_html}

    <footer>
      Generated by Data Investigator (Audit Engine v0.1.0) &bull; Mode: READ-ONLY &bull; Cutoff strictly bounded by audit_at
    </footer>
  </div>
</body>
</html>"""


def save_case(r: CaseResult, directory: Path, fmt: str = "json") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    if fmt == "markdown":
        p = directory / f"{r.case_id}.md"
        p.write_text(case_markdown(r), encoding="utf-8")
    elif fmt == "csv":
        p = directory / f"{r.case_id}.csv"
        p.write_text(case_csv(r), encoding="utf-8")
    elif fmt == "html":
        p = directory / f"{r.case_id}.html"
        p.write_text(case_html(r), encoding="utf-8")
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
