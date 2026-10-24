"""Data Investigator CLI. Presentation only: every screen renders an already-computed result."""
from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer

from . import __version__
from .config import REPO_ROOT, config_path, default_yaml, load_settings
from .engine import STAGES, CaseResult, StageEvent
from .ingestion import IngestionError
from .reporting import FINDING_TEXT, SEVERITY, case_to_dict, save_case
from .ui.console import UIState, banner, make_console, status_text, table
from .ui.masking import mask_email, mask_identifier, mask_text

app = typer.Typer(add_completion=False, no_args_is_help=False, rich_markup_mode=None,
                  help="Data Investigator — evidence-backed deletion auditing (read-only).")
sources_app = typer.Typer(help="Inspect configured evidence sources.")
app.add_typer(sources_app, name="sources")

UI = UIState()
CFG_PATH: Optional[str] = None
NO_LLM_GLOBAL = False


def _con():
    return make_console(UI)


def _err():
    return make_console(UI, stderr=True)


def _fail(msg: str, code: int):
    _err().print(f"ERROR: {msg}")
    raise typer.Exit(code)


def _settings():
    try:
        s, _ = load_settings(CFG_PATH)
        return s
    except FileNotFoundError as e:
        _fail(str(e), 2)
    except Exception as e:  # pydantic validation error
        _fail(f"invalid configuration: {e}", 2)


def _session(no_llm: bool = False, quiet_ai: bool = False):
    from .app import open_session

    s = _settings()
    try:
        sess = open_session(s, use_llm=False if (no_llm or NO_LLM_GLOBAL) else None)
    except IngestionError as e:
        _fail(str(e), 2)
    if sess.ai_status.startswith("UNAVAILABLE") and not quiet_ai and not UI.quiet:
        _err().print("AI ASSISTANCE: UNAVAILABLE — the deterministic investigation engine will continue where possible.")
        if UI.verbose:
            _err().print(f"  reason: {sess.ai_status}")
    return sess


def _find_case(sess, case_id: str):
    from .app import requests_for

    for sample in (False, True):
        for q in requests_for(sess, sample=sample):
            if q.case_id == case_id:
                return q
    _fail(f"Case {case_id} was not found in the configured dataset.", 2)


def _investigate(sess, case_id: str, show_stages: bool) -> CaseResult:
    q = _find_case(sess, case_id)
    con = _con()

    def on_stage(e: StageEvent):
        if show_stages and not UI.quiet and UI.fmt == "text":
            i = STAGES.index(e.stage) + 1 if e.stage in STAGES else 0
            label = f"[{i}/{len(STAGES)}] {e.stage} "
            con.print(f"{label}{'.' * max(2, 44 - len(label))} {e.status}" + (f"  ({mask_text(e.detail)})" if UI.verbose and e.detail else ""))

    return sess.investigator.investigate(q, on_stage)


def _version(value: bool):
    if value:
        typer.echo(f"data-investigator {__version__}")
        raise typer.Exit(0)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show stage details, evidence references and rule ids."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress progress output."),
    config: Optional[str] = typer.Option(None, "--config", help="Path to data-investigator.yaml."),
    no_color: bool = typer.Option(False, "--no-color", help="Disable colours (NO_COLOR is also honoured)."),
    fmt: str = typer.Option("text", "--format", help="text or json (machine-readable output where supported)."),
    no_llm: bool = typer.Option(False, "--no-llm", help="Deterministic engine only for any command."),
    version: bool = typer.Option(False, "--version", callback=_version, is_eager=True, help="Show version."),
):
    global CFG_PATH, NO_LLM_GLOBAL
    NO_LLM_GLOBAL = no_llm
    UI.verbose, UI.quiet, UI.no_color = verbose, quiet, no_color
    if fmt not in ("text", "json"):
        _fail("--format must be text or json", 1)
    UI.fmt = fmt
    CFG_PATH = config
    if ctx.invoked_subcommand is None:
        con = _con()
        con.print(banner("DATA INVESTIGATOR", "AI-Powered Data Deletion Auditor\nInvestigate. Trace. Verify. Report."))
        con.print(f"Version: {__version__}\nMode: Local Evidence Audit\nSecurity: Read-only by default\n")
        if not config_path(config).is_file():
            con.print("Using built-in defaults (dataset: ./dataset). Optional: run 'data-investigator init'.\n")
        con.print("GETTING STARTED\n  init          Configure your project\n  sources list  View available evidence sources\n"
                  "  scan          Build the evidence index\n  audit         Investigate a deletion request (--case ID | --all)\n"
                  "  report        View an investigation report\n  plan          Generate a remediation plan\n"
                  "  verify        Verify deletion evidence\n  validate-output  Check output.csv against the contract\n\n"
                  "Run 'data-investigator --help' for all commands.")


# ---------------------------------------------------------------------- #
@app.command()
def init(force: bool = typer.Option(False, "--force", help="Overwrite an existing configuration."),
         dataset: Optional[str] = typer.Option(None, "--dataset", help="Dataset directory (default ./dataset).")):
    """Create data-investigator.yaml, check the dataset, prepare reports/."""
    con = _con()
    con.print("PROJECT SETUP\n")
    failures = 0

    def line(i, name, status):
        label = f"[{i}/5] {name} "
        con.print(f"{label}{'.' * max(2, 44 - len(label))} {status}")

    ok_py = sys.version_info >= (3, 10)
    line(1, "Checking Python environment", "OK" if ok_py else "FAIL (need >= 3.10)")
    failures += not ok_py
    p = config_path(CFG_PATH)
    if p.is_file() and not force:
        if sys.stdin.isatty() and typer.confirm(f"{p.name} exists. Overwrite?", default=False):
            force = True
        else:
            line(2, "Loading configuration", "OK (kept existing)")
    if not p.is_file() or force:
        text = default_yaml()
        if dataset:
            text = text.replace("root: dataset", f"root: {dataset}")
        p.write_text(text, encoding="utf-8")
        p_env = REPO_ROOT / ".env.example"
        if not p_env.exists():
            p_env.write_text("# Optional. Ollama host if not localhost:11434\nOLLAMA_HOST=http://localhost:11434\n")
        line(2, "Loading configuration", "OK (written)")
    s = _settings()
    root = s.path(s.dataset.root)
    line(3, "Checking evidence directory", "OK" if (root / "evidence").is_dir() else "FAIL")
    failures += not (root / "evidence").is_dir()
    from .ingestion import REQUIRED, _read

    missing = []
    for name, cols in REQUIRED.items():
        try:
            _read(root / name, cols)
        except IngestionError as e:
            missing.append(str(e))
    line(4, "Validating input files", "OK" if not missing else "FAIL")
    for m in missing:
        con.print(f"   - {m}")
    failures += bool(missing)
    rep = s.path(s.reports.directory)
    try:
        rep.mkdir(parents=True, exist_ok=True)
        line(5, "Preparing report directory", "OK")
    except OSError as e:
        line(5, "Preparing report directory", f"FAIL ({e})")
        failures += 1
    if failures:
        con.print("\nSetup incomplete. Fix the items marked FAIL.")
        raise typer.Exit(2)
    con.print(f"\nProject initialized.\n\nConfiguration: {p}\nEvidence:      {root}\nReports:       {rep}\n\nNext step:\n  data-investigator sources list")


@sources_app.command("list")
def sources_list():
    """Show which evidence sources are available (read-only)."""
    from .ingestion import REQUIRED, _read

    s = _settings()
    root = s.path(s.dataset.root)
    t = table("Source", "Type", "Status", "Access")
    counts = Counter()
    for name, cols in REQUIRED.items():
        try:
            _read(root / name, cols)
            st = "Available"
        except IngestionError:
            st = "Invalid" if (root / name).is_file() else "Unavailable"
        counts[st] += 1
        t.add_row(name, "CSV", st, "Read-only")
    try:
        from .ingestion import load_dataset

        ds = load_dataset(root)
        groups = Counter(Path(e.file_path).suffix.lstrip(".") for e in ds.manifest.values())
        missing = Counter(Path(e.file_path).suffix.lstrip(".") for e in ds.manifest.values() if not ds.evidence_path(e).is_file())
        for ext, n in sorted(groups.items()):
            st = "Available" if not missing[ext] else f"Unavailable ({missing[ext]} missing)"
            if ext == "png" and s.agent.enabled == "off":
                st = "Unsupported (needs AI extraction)"
            counts[st.split(" ")[0]] += 1
            t.add_row(f"evidence/*.{ext} ({n} files)", ext.upper(), st, "Read-only")
    except IngestionError:
        pass
    con = _con()
    con.print("DATA SOURCES")
    con.print(t)
    con.print("\n" + "  ".join(f"{k}: {v}" for k, v in sorted(counts.items())) + "\nAll listed sources are read-only.\n\nNext step:\n  data-investigator scan")


@app.command()
def scan(no_llm: bool = typer.Option(False, "--no-llm", help="Skip AI extraction of screenshots.")):
    """Parse every evidence file once and report coverage and parse errors."""
    con = _con()
    if UI.fmt == "text":
        con.print("EVIDENCE SCAN\n")
    sess = _session(no_llm)
    st = Counter(f.status for f in sess.index.files.values())
    rows = sum(f.observations for f in sess.index.files.values())
    if UI.fmt == "json":
        con.print_json(json.dumps({"files": {k: v.__dict__ for k, v in sess.index.files.items()}, "statuses": st}))
        return
    t = table("File", "Kind", "Status", "Observations")
    for f in sess.index.files.values():
        if UI.verbose or f.status not in ("PARSED", "AI_EXTRACTED"):
            t.add_row(f.file_id, f.kind, f.status, str(f.observations))
    if t.row_count:
        con.print(t)
    con.print(f"SCAN SUMMARY\n\nCore CSV sources:      6\nActivity rows:         {len(sess.ds.activities)}\n"
              f"Evidence files:        {len(sess.index.files)}\n" + "".join(f"  {k:<20} {v}\n" for k, v in sorted(st.items())) +
              f"Observations indexed:  {rows}\nAI assistance:         {sess.ai_status}\n")
    for f in sess.index.files.values():
        for e in f.errors:
            con.print(f"  error {f.file_id}: {e}")
    con.print("Note:\nA successful scan means the configured sources were processed.\nIt does not mean every deletion request is complete.")


# ---------------------------------------------------------------------- #
@app.command()
def audit(
    case: Optional[str] = typer.Option(None, "--case", help="Investigate one case."),
    all_: bool = typer.Option(False, "--all", help="Investigate every request and write output.csv."),
    sample: bool = typer.Option(False, "--sample", help="With --all: run the 32 public sample requests instead."),
    no_llm: bool = typer.Option(False, "--no-llm", help="Deterministic engine only."),
    output: Optional[str] = typer.Option(None, "--output", help="Output path for --all (default ./output.csv)."),
    save_reports: bool = typer.Option(False, "--save-reports", help="With --all: write reports/<case>.json for every case."),
):
    """Investigate deletion requests (read-only)."""
    if bool(case) == bool(all_):
        _fail("use exactly one of --case ID or --all", 1)
    if case:
        return _audit_one(case, no_llm)
    _audit_all(sample, no_llm, output, save_reports)


def _audit_one(case_id: str, no_llm: bool):
    con = _con()
    sess = _session(no_llm)
    q = _find_case(sess, case_id)
    if UI.fmt == "text":
        con.print(banner("DATA INVESTIGATOR — DELETION AUDIT"))
        con.print(f"Case ID:       {q.case_id}\nRequester:     {mask_email(q.email)}\nAudit time:    {q.audit_at:%Y-%m-%d %H:%M:%S} UTC\nMode:          READ-ONLY\n\nINVESTIGATION\n")
    r = _investigate(sess, case_id, show_stages=True)
    s = _settings()
    path = save_case(r, s.path(s.reports.directory))
    if UI.fmt == "json":
        con.print_json(json.dumps(case_to_dict(r)))
        return
    con.print(f"\nRESULT: {status_text(r.deletion_status)}\n")
    findings = [x for x in r.steps if x.phase.value == "FINDING"]
    if findings:
        con.print("FINDINGS\n")
        for f in findings:
            con.print(f"[!] {f.operation.replace('REPORT_', '').replace('_', ' ').title()}\n    System:      {f.system_id}\n"
                      f"    Resource:    {f.resource_id}\n    Evidence:    {f.evidence_id}\n")
    ok = [o for o in r.obligations if o.satisfied]
    for o in ok[:5]:
        con.print(f"[✓] {o.resource_id} ({o.category}) is {o.state} as required" + (f" — {o.evidence.source_id}" if o.evidence else ""))
    if len(ok) > 5:
        con.print(f"    +{len(ok) - 5} more")
    for g in r.gaps:
        con.print(f"[?] {mask_text(g)}")
    if UI.verbose and r.identity:
        con.print(f"\nIdentity: {r.identity.user_id} via {', '.join(r.identity.path)}; intent {r.intent.intent} ({r.intent.source})")
    nxt = ["findings", "timeline", "plan"] if r.deletion_status == "INCOMPLETE" else ["report"]
    con.print(f"\nREPORT\n  {path}\n\nRun:\n" + "\n".join(f"  data-investigator {c} --case {case_id}" for c in nxt))


def _audit_all(sample: bool, no_llm: bool, output: Optional[str], save_reports: bool):
    from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn

    from .app import requests_for, run_batch

    con = _con()
    s = _settings()
    out = Path(output) if output else s.path(s.output.path)
    if UI.fmt == "text" and not UI.quiet:
        con.print("FULL DATASET AUDIT\n")
    sess = _session(no_llm)
    reqs = requests_for(sess, sample=sample)
    counts = Counter()
    results_holder = []
    with Progress(TextColumn("Evaluating cases"), BarColumn(), MofNCompleteColumn(), console=con,
                  disable=UI.quiet or UI.fmt == "json") as prog:
        task = prog.add_task("cases", total=len(reqs))

        def on_case(r, q, e):
            counts["FAILED" if e else r.deletion_status] += 1
            if r is not None and save_reports:
                save_case(r, s.path(s.reports.directory))
            prog.advance(task)

        results, rep, failed = run_batch(sess, reqs, out, on_case)
    results_holder.extend(results)
    _write_run_artifacts(sess, len(reqs), sample, out)
    summary = {"cases_processed": len(reqs), "COMPLETE": counts["COMPLETE"], "INCOMPLETE": counts["INCOMPLETE"],
               "NOT_APPLICABLE": counts["NOT_APPLICABLE"], "failed_processing": len(failed),
               "validation": "PASSED" if rep.ok else "FAILED", "output": str(out), "ai_assistance": sess.ai_status}
    if UI.fmt == "json":
        con.print_json(json.dumps(summary))
    else:
        con.print("\nAUDIT SUMMARY\n")
        for k in ("cases_processed", "COMPLETE", "INCOMPLETE", "NOT_APPLICABLE", "failed_processing"):
            con.print(f"{k.replace('_', ' ').capitalize():<22} {summary[k]}")
        llm = sess.client.meter.summary() if sess.client else {}
        con.print(f"{'LLM calls':<22} {sum(v['calls'] for v in llm.values())} ({sum(v['live'] for v in llm.values())} live)")
        con.print(f"{'Tokens':<22} {sum(v['input_tokens'] + v['output_tokens'] for v in llm.values()):,}")
        con.print(f"\nOutput:\n  {out}\n\nValidation:\n  {summary['validation']}")
        for cid, why in failed:
            con.print(f"  failed {cid}: {why}")
    if not rep.ok:
        _print_issues(rep)
        raise typer.Exit(3)
    if failed:
        raise typer.Exit(5)


def _write_run_artifacts(sess, n_cases: int, sample: bool, out: Path):
    from .reporting import usage_markdown

    s = sess.settings
    rep_dir = s.path(s.reports.directory)
    rep_dir.mkdir(parents=True, exist_ok=True)
    sess.guard_log.dump(rep_dir / "llm_audit.jsonl")
    meter = sess.client.meter.summary() if sess.client else {}
    guard_counts = Counter(e["verdict"].split(":")[0] + ":" + e["task"] for e in sess.guard_log.entries)
    label = f"{'sample' if sample else 'full'} audit at {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} -> {out.name}"
    md = usage_markdown(meter, n_cases, s.agent.model if sess.client else "", getattr(sess.client, "digest", "") or "",
                        s.agent.provider if sess.client else "none", sess.ai_status, label, dict(guard_counts))
    (rep_dir / ("usage_sample.md" if sample else "usage_full.md")).write_text(md, encoding="utf-8")


def _print_issues(rep):
    con = _err()
    con.print("VALIDATION FAILED\n")
    for i in rep.issues[:20]:
        con.print(f"Case: {i.case_id}\nProblem: {i.problem}\n{i.detail}\n")
    if len(rep.issues) > 20:
        con.print(f"... {len(rep.issues) - 20} more issues")
    con.print("No successful validation result was produced.")


# ---------------------------------------------------------------------- #
@app.command()
def identity(case: str = typer.Option(..., "--case")):
    """Show which identifiers were associated with the request and why."""
    con = _con()
    sess = _session(quiet_ai=True)
    r = _investigate(sess, case, show_stages=False)
    if UI.fmt == "json":
        con.print_json(json.dumps(case_to_dict(r)["identity"]))
        return
    con.print(f"IDENTITY INVESTIGATION\n\nCase: {case}\n\nRequested identifier:\n  {mask_email(r.request.email)}")
    if r.identity is None:
        con.print("\nNot evaluated: the request does not authorize deletion.")
        return
    g = sess.investigator.graph
    t = table("Kind", "Value", "Relationship", "Source")
    uid = r.identity.user_id
    for kind, val in sorted(g.user_idents.get(uid, set())):
        srcs = g.owners[(kind, val)][uid]
        rel = "current" if any(s == "user_identities" for s, _ in srcs) else "historical"
        if kind == "email_hash" and all(s == "user_identities" for s, _ in srcs):
            rel = "derived / current"
        t.add_row(kind, mask_identifier(kind, val), rel, ", ".join(sorted({s for s, _ in srcs})))
    con.print(t)
    status = "Resolved" if uid and not r.identity.ambiguities else ("Ambiguous" if uid else "Unresolved")
    con.print(f"\nIDENTITY RESULT\n\nStatus: {status}\nuser_id: {uid} (via {', '.join(r.identity.path)})")
    for a in r.identity.ambiguities:
        con.print(f"  ! {a}")
    con.print("\nNote:\nNames are shown masked and are never used alone to link records.")


@app.command()
def timeline(case: str = typer.Option(..., "--case"), from_: Optional[str] = typer.Option(None, "--from"),
             to: Optional[str] = typer.Option(None, "--to"), system: Optional[str] = typer.Option(None, "--system")):
    """Chronological evidence for the case's resources, bounded by audit_at."""
    from .normalize import parse_ts

    con = _con()
    sess = _session(quiet_ai=True)
    r = _investigate(sess, case, show_stages=False)
    events = [(r.request.requested_at, "REQUEST", "deletion requested", "", ""),
              (r.request.audit_at, "AUDIT_AT", "audit boundary", "", "")]
    for o in r.obligations:
        for h in o.history:
            events.append((h.observed_at, "CLAIM" if h.source_kind == "activity" else "OBSERVED",
                           f"{h.resource_id} {h.state or 'seen'}", h.system_id, h.source_id))
    for a in sess.ds.activities:
        if any(a.resource_id == o.resource_id for o in r.obligations) and a.event_time <= r.request.audit_at:
            events.append((a.event_time, "CLAIM", f"{a.event_type} {a.resource_id} reported {a.reported_result}", a.target_system, a.activity_id))
    lo, hi = parse_ts(from_), parse_ts(to)
    clamped = hi is not None and hi > r.request.audit_at
    hi = min(hi, r.request.audit_at) if hi else r.request.audit_at
    events = sorted({e for e in events if (lo is None or e[0] >= lo) and e[0] <= hi and (not system or e[3] in ("", system))})
    if UI.fmt == "json":
        con.print_json(json.dumps([{"at": e[0].isoformat(), "kind": e[1], "event": e[2], "system": e[3], "source": e[4]} for e in events]))
        return
    con.print(f"EVENT TIMELINE — {case}\n")
    if clamped:
        con.print("(--to was later than audit_at; clamped to audit_at)\n")
    for at, kind, what, sys_, src in events:
        con.print(f"{at:%Y-%m-%d %H:%M}  [{kind:<8}] {what}" + (f"  · {src}" if src else ""))
    reappeared = any(k == "OBSERVED" and "PRESENT" in w for _, k, w, _, _ in events) and any(
        k == "CLAIM" and "DELETE_RECORD" in w and "SUCCESS" in w for _, k, w, _, _ in events)
    con.print("\nSUMMARY\n")
    if reappeared:
        con.print("A record was observed PRESENT although a deletion was reported as successful.\n"
                  "Activity results are claims; the observed state decides.")
    if any("SYNC" in w or "repopulated" in w for _, _, w, _, _ in events):
        con.print("Synchronization activity appears in the timeline. Causation requires supporting evidence.")
    if not reappeared:
        con.print("State at audit_at for each resource is the latest observation before the boundary.")


@app.command()
def findings(case: str = typer.Option(..., "--case")):
    """Confirmed findings, separate from unresolved questions."""
    con = _con()
    sess = _session(quiet_ai=True)
    r = _investigate(sess, case, show_stages=False)
    fs = [s for s in r.steps if s.phase.value == "FINDING"]
    if UI.fmt == "json":
        con.print_json(json.dumps({"findings": [s.to_json() for s in fs], "unresolved": r.gaps}))
        return
    con.print(f"FINDINGS — {case}\n")
    if not fs:
        con.print("No confirmed findings.\n")
    ob = {(o.system_id, o.resource_id): o for o in r.obligations}
    for n, s in enumerate(fs, 1):
        o = ob.get((s.system_id, s.resource_id))
        con.print(f"F-{n:03d}  {s.operation.replace('REPORT_', '')}\n       Severity: {SEVERITY[s.operation]}\n"
                  f"       System: {s.system_id}\n       Resource: {s.resource_id}\n       Evidence: {s.evidence_id}\n"
                  f"       Rule: {o.treatment.rule_id if o else '-'}\n\n       Explanation:\n       {FINDING_TEXT[s.operation]}\n")
    if r.gaps:
        con.print("UNRESOLVED QUESTIONS\n")
        for g in r.gaps:
            con.print(f"- {mask_text(g)}")
    con.print(f"\nRun:\n  data-investigator report --case {case}")


@app.command()
def evidence(case: str = typer.Option(..., "--case")):
    """Every evidence item used for the case, plus items excluded because they post-date audit_at."""
    con = _con()
    sess = _session(quiet_ai=True)
    r = _investigate(sess, case, show_stages=False)
    t = table("Source", "Kind", "Observed", "System", "Resource", "State", "File")
    for o in r.obligations:
        for h in sorted(o.history, key=lambda x: x.observed_at):
            path = sess.ds.manifest[h.source_id].file_path if h.source_id in sess.ds.manifest else "system_activity.csv"
            t.add_row(h.source_id, h.source_kind, f"{h.observed_at:%Y-%m-%d %H:%M}", h.system_id, h.resource_id, h.state or "-", path)
    if UI.fmt == "json":
        con.print_json(json.dumps(case_to_dict(r)["timeline"]))
        return
    con.print(f"EVIDENCE — {case}\n")
    con.print(t)
    if r.excluded_after_audit:
        con.print("\n[dim]Excluded (after audit_at): " + ", ".join(sorted({o.source_id for o in r.excluded_after_audit})) + "[/dim]")


@app.command()
def plan(case: str = typer.Option(..., "--case")):
    """The ordered plan — identical to the case's steps_json row. Nothing is executed."""
    con = _con()
    sess = _session(quiet_ai=True)
    r = _investigate(sess, case, show_stages=False)
    s = _settings()
    d = s.path(s.reports.directory)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{case}-plan.json").write_text(json.dumps([x.to_json() for x in r.steps], indent=2), encoding="utf-8")
    if UI.fmt == "json":
        con.print_json(json.dumps([x.to_json() for x in r.steps]))
        return
    con.print(f"REMEDIATION PLAN — {case}\n\nMode: PLAN ONLY\nNo data has been changed.\n")
    if r.deletion_status == "NOT_APPLICABLE":
        con.print("No deletion authorized by this request.")
        return
    if r.deletion_status == "COMPLETE":
        con.print("No remediation required. Verification checks backed by evidence:\n")
    for phase, title in (("FINDING", "PHASE 1 — FINDINGS"), ("REMEDIATION", "PHASE 2 — REMEDIATION"), ("VERIFICATION", "PHASE 3 — VERIFICATION")):
        steps = [x for x in r.steps if x.phase.value == phase]
        if not steps:
            continue
        con.print(title + "\n")
        for x in steps:
            extra = "   Execution: Not performed" if phase == "REMEDIATION" else (f"   Evidence: {x.evidence_id}" if x.evidence_id else "")
            con.print(f"{x.step_no}. {x.operation} → {x.state}\n   System: {x.system_id}  Resource: {x.resource_id}\n{extra}\n")
    con.print(f"PLAN STATUS: READY FOR REVIEW\n\nPlan saved:\n  {d / f'{case}-plan.json'}")


@app.command()
def verify(case: str = typer.Option(..., "--case"), at: Optional[str] = typer.Option(None, "--at", help="Re-evaluate with evidence up to this time (never affects output.csv).")):
    """Check each obligation: VERIFIED / FAILED / UNVERIFIABLE."""
    from dataclasses import replace

    from .normalize import parse_ts

    con = _con()
    sess = _session(quiet_ai=True)
    q = _find_case(sess, case)
    if at:
        ts = parse_ts(at)
        if ts is None:
            _fail(f"could not parse --at {at!r}", 1)
        q = replace(q, audit_at=ts)
        if UI.fmt == "text":
            con.print(f"Evaluating with evidence up to {ts:%Y-%m-%dT%H:%M:%SZ}; this does not affect output.csv.\n")
    r = sess.investigator.investigate(q)
    rows = []
    for o in r.obligations:
        res = "VERIFIED" if o.satisfied else ("FAILED" if o.satisfied is False else "UNVERIFIABLE")
        rows.append({"system_id": o.system_id, "resource_id": o.resource_id, "expected": o.treatment.required_state,
                     "observed": o.state, "result": res, "evidence_id": o.evidence.source_id if o.evidence else None})
    overall = "NOT_APPLICABLE" if r.deletion_status == "NOT_APPLICABLE" else (
        "VERIFIED" if rows and all(x["result"] == "VERIFIED" for x in rows) else
        "VERIFICATION FAILED" if any(x["result"] == "FAILED" for x in rows) else "INCOMPLETE VERIFICATION")
    s = _settings()
    d = s.path(s.reports.directory)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{case}-verification.json").write_text(json.dumps({"result": overall, "checks": rows}, indent=2), encoding="utf-8")
    if UI.fmt == "json":
        con.print_json(json.dumps({"result": overall, "checks": rows}))
        return
    con.print(f"VERIFICATION — {case}\n")
    for x in rows:
        con.print(f"{x['resource_id']:<22} expected {x['expected']:<10} observed {str(x['observed']):<11} {x['result']}"
                  + (f"  ({x['evidence_id']})" if x["evidence_id"] else ""))
    con.print(f"\nRESULT: {overall}")
    if overall == "VERIFICATION FAILED":
        con.print(f"\nThe deletion request remains incomplete under the evaluated rules.\n\nNext step:\n  data-investigator findings --case {case}")
    if overall == "INCOMPLETE VERIFICATION":
        con.print("\nSome checks could not be verified because evidence was unavailable.")


@app.command()
def report(case: str = typer.Option(..., "--case"), fmt: str = typer.Option("json", "--export", help="json | markdown | csv")):
    """Summarize the investigation and export a report file."""
    con = _con()
    sess = _session(quiet_ai=True)
    r = _investigate(sess, case, show_stages=False)
    s = _settings()
    if fmt not in ("json", "markdown", "csv"):
        _fail("--export must be json, markdown or csv", 1)
    p = save_case(r, s.path(s.reports.directory), fmt)
    if UI.fmt == "json":
        con.print_json(json.dumps(case_to_dict(r)))
        return
    d = case_to_dict(r)
    fs = sum(1 for x in r.steps if x.phase.value == "FINDING")
    rem = sum(1 for x in r.steps if x.phase.value == "REMEDIATION")
    con.print(f"AUDIT REPORT\n\nCase: {case}\nStatus: {status_text(r.deletion_status)}\n\nIDENTITY\n  Resolved: "
              f"{'Yes' if r.identity and r.identity.user_id else 'No'}\n  Identifier kinds: "
              f"{', '.join(d['identity']['identifier_kinds']) if d['identity'] else '-'}\n\nEVIDENCE\n  Obligations evaluated: {len(r.obligations)}\n"
              f"  Findings: {fs}\n  Unresolved questions: {len(r.gaps)}\n\nREMEDIATION\n  Proposed steps: {rem}\n  Executed steps: 0\n  Verified steps: 0\n\n"
              f"OUTPUT FILES\n  {fmt.upper()}: {p}")


@app.command()
def apply(case: str = typer.Option(..., "--case"), dry_run: bool = typer.Option(False, "--dry-run")):
    """Preview remediation. Execution is not available in this release."""
    if not dry_run:
        _fail("The requested operation is not permitted in the current mode (approval-gated execution is a 1.0 feature; use --dry-run).", 4)
    con = _con()
    sess = _session(quiet_ai=True)
    r = _investigate(sess, case, show_stages=False)
    con.print(f"REMEDIATION PREVIEW\n\nCase: {case}\n")
    for n, x in enumerate([x for x in r.steps if x.phase.value == "REMEDIATION"], 1):
        con.print(f"Action {n}: {x.operation} on {x.system_id}/{x.resource_id}")
    con.print("\nMode: DRY RUN\n\nNo changes were made.\n\nBefore execution:\n  - Verify permissions.\n  - Review applicable retention rules.\n"
              "  - Confirm the affected resources.\n  - Obtain the required approval.")


@app.command("validate-output")
def validate_output(file: Optional[str] = typer.Option(None, "--file", help="Default ./output.csv"),
                    sample: bool = typer.Option(False, "--sample", help="Validate against the sample requests.")):
    """Check output.csv against the challenge contract."""
    from .app import evidence_times
    from .ingestion import case_sort_key, load_dataset, load_requests
    from .validation import read_output, validate_rows

    con = _con()
    s = _settings()
    p = Path(file) if file else s.path(s.output.path)
    if not p.is_file():
        _fail(f"Required input file is missing: {p}", 2)
    ds = load_dataset(s.path(s.dataset.root))
    reqs = load_requests(ds.root / ("sample_deletion_requests.csv" if sample else "deletion_requests.csv"))

    class _S:  # minimal stand-in for evidence_times()
        pass

    st = _S()
    st.ds = ds
    header, rows = read_output(p)
    rep = validate_rows(header, rows, [q.case_id for q in sorted(reqs, key=lambda q: case_sort_key(q.case_id))],
                        evidence_times(st), {q.case_id: q.audit_at for q in reqs})
    if UI.fmt == "json":
        con.print_json(json.dumps({"ok": rep.ok, "checks": rep.checks, "issues": [i.__dict__ for i in rep.issues]}))
    else:
        con.print("OUTPUT VALIDATION\n")
        for name, ok in rep.checks:
            con.print(f"[{'✓' if ok else '✗'}] {name}")
        con.print(f"\nRESULT: {'PASSED' if rep.ok else 'FAILED'}\n\nValidated file: {p}")
    if not rep.ok:
        _print_issues(rep)
        raise typer.Exit(3)


def run():
    app()
