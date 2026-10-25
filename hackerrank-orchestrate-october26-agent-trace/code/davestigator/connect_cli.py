"""`Davestigator connect ...` commands and `audit --live`.

Read-only by construction: credentials come only from environment variables, writable
credentials are refused (exit 4), and no audit runs on a collection without an approved mapping.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer
import yaml

from .config import ConnectorCfg, config_path, load_settings
from .connectors import FAMILIES, ConnectorError, CredentialsMissing, DriverMissing, NotReadOnly, open_connector
from .connectors.base import COVERAGE_FULL
from .connectors.mapping import (ConnectorMapping, approval_problems, load_mapping, mapping_hash,
                                 save_mapping)
from .ui.console import UIState, banner, make_console, status_text, table
from .ui.masking import mask_email, mask_text

connect_app = typer.Typer(help="Connect live data stores (read-only): add, test, discover, map, snapshot.")


def _ui() -> UIState:
    from .cli import UI as GLOBAL_UI

    return GLOBAL_UI


def _con():
    from .cli import UI as GLOBAL_UI

    return make_console(GLOBAL_UI)


def _fail(msg: str, code: int):
    from .cli import UI as GLOBAL_UI

    make_console(GLOBAL_UI, stderr=True).print(f"ERROR: {msg}")
    raise typer.Exit(code)


def _cfg_path() -> Path:
    from .cli import CFG_PATH

    return config_path(CFG_PATH)


def _settings():
    from .cli import CFG_PATH

    try:
        s, _ = load_settings(CFG_PATH)
        return s
    except Exception as e:  # noqa: BLE001
        _fail(f"invalid configuration: {e}", 2)


def _connector_cfg(s, name: str) -> ConnectorCfg:
    for c in s.connectors:
        if c.name == name:
            return c
    _fail(f"no connector named {name!r} in {_cfg_path().name}; add it with: Davestigator connect add", 2)


def _open(s, name: str):
    cfg = _connector_cfg(s, name)
    try:
        return open_connector(cfg)
    except (CredentialsMissing, DriverMissing) as e:
        _fail(str(e), 2)
    except ConnectorError as e:
        _fail(str(e), 2)


def _known_categories(s) -> set[str]:
    from .connectors.live import load_rules

    p = s.path(s.live.rules)
    try:
        return {r.data_category for r in load_rules(p)} | {"DATA_FLOW"}
    except Exception:  # noqa: BLE001
        _fail(f"policy file not readable: {p}", 2)


# ---------------------------------------------------------------------- #
@connect_app.command("add")
def add(name: str = typer.Option(..., "--name"), kind: str = typer.Option(..., "--kind", help="sql | search | document | kv | object"),
        url_env: str = typer.Option(..., "--url-env", help="Environment variable holding the connection URL."),
        url: Optional[str] = typer.Option(None, "--url", hidden=True),
        row_limit: int = typer.Option(500, "--row-limit"), timeout_s: int = typer.Option(15, "--timeout")):
    """Register a data store. No data is read."""
    if url:
        _fail("Pass credentials through an environment variable (--url-env), not the command line.", 1)
    if kind not in FAMILIES:
        _fail(f"unknown kind {kind!r}. Supported:\n" + "\n".join(f"  {k:<9} {v}" for k, v in FAMILIES.items()), 1)
    p = _cfg_path()
    data = yaml.safe_load(p.read_text()) if p.is_file() else {}
    data = data or {}
    conns = [c for c in data.get("connectors", []) if c.get("name") != name]
    conns.append({"name": name, "kind": kind, "url_env": url_env, "options": {"row_limit": row_limit, "statement_timeout_s": timeout_s,
                                                                              "timeout_s": timeout_s}})
    data["connectors"] = conns
    p.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    con = _con()
    con.print(f"CONNECTOR ADDED\n\nName:          {name}\nKind:          {kind} ({FAMILIES[kind]})\n"
              f"Credentials:   from environment variable {url_env}   (value not stored, not shown)\n"
              f"Saved to:      {p.name}  → connectors \\[{name}]\n\nNext step:\n  Davestigator connect test {name}")


@connect_app.command("list")
def list_connectors():
    """Configured connectors, credential presence, and mapping approval state."""
    import os

    s = _settings()
    m = load_mapping(s.path(s.live.mappings))
    t = table("Connector", "Kind", "Credentials", "Approved collections", "Access")
    for c in s.connectors:
        cm = m.connectors.get(c.name)
        n = sum(1 for x in cm.collections if x.review == "approved") if cm else 0
        t.add_row(c.name, c.kind, f"${c.url_env} {'set' if os.environ.get(c.url_env) else 'NOT SET'}", str(n), "Read-only")
    con = _con()
    con.print("CONNECTORS")
    con.print(t)
    if not s.connectors:
        con.print("No connectors configured. Add one with: Davestigator connect add --name NAME --kind sql --url-env VAR")


@connect_app.command("test")
def test(name: str = typer.Argument(...)):
    """Connectivity plus proof the credentials cannot modify data."""
    s = _settings()
    con = _con()
    con.print(f"CONNECTOR TEST — {name}\n")
    cfg = _connector_cfg(s, name)
    con.print(f"[1/3] Resolving credentials ({cfg.url_env}) ....... ", end="")
    try:
        conn = open_connector(cfg)
        con.print("OK")
    except (CredentialsMissing, DriverMissing, ConnectorError) as e:
        con.print("FAILED")
        _fail(str(e), 2)
    con.print("[2/3] Connecting + read-only check ........... ", end="")
    try:
        rep = conn.test()
    except NotReadOnly as e:
        con.print("FAILED")
        con.print(f"      {mask_text(str(e))}\n\nRESULT: NOT PERMITTED\nUse a read-only role. Davestigator will not run against writable credentials.")
        raise typer.Exit(4)
    except ConnectorError as e:
        con.print("FAILED")
        _fail(str(e), 2)
    finally:
        conn.close()
    con.print(f"OK  ({rep.server}; {rep.detail})")
    o = cfg.options
    con.print(f"[3/3] Limits ................................. OK  (timeout {o.get('statement_timeout_s', o.get('timeout_s', 15))} s / "
              f"{o.get('row_limit', 500)} rows per collection)\n\nRESULT: READY (read-only)")


@connect_app.command("discover")
def discover(name: str = typer.Argument(...), sample: int = typer.Option(50, "--sample", help="Values sampled per candidate column (counted only).")):
    """Schema and personal-data candidate columns. Sampled values are never shown or stored."""
    from .connectors.discovery import analyze

    s = _settings()
    conn = _open(s, name)
    try:
        conn.test()
        infos = conn.discover()
        findings = [analyze(conn, i, sample) for i in infos]
    except NotReadOnly as e:
        _fail(f"Role can modify data ({mask_text(str(e))}). Use a read-only role.", 4)
    except ConnectorError as e:
        _fail(str(e), 2)
    finally:
        conn.close()
    con = _con()
    if _ui().fmt == "json":
        con.print_json(json.dumps([{"collection": f.info.source, "rows": f.info.rows, "readable": f.info.readable,
                                    "columns": [{"column": c.column, "kind": c.kind or "text", "rate": c.rate, "by": c.by} for c in f.columns],
                                    "confidence": f.confidence} for f in findings]))
        return
    con.print(f"DISCOVERY — {name}   (schema only; sampled values are counted, never shown or stored)\n")
    t = table("Collection", "Rows", "Personal-data candidates (match rate)", "Confidence")
    for f in findings:
        if not f.info.readable:
            t.add_row(f.info.source, "-", f"NO ACCESS ({f.info.note})", "-")
            continue
        cand = " · ".join(f"{c.column} ({c.kind or 'text'}{f', {c.rate:.0%}' if c.rate is not None else ''})" for c in f.columns) or "—"
        t.add_row(f.info.source, "?" if f.info.rows is None else f"{f.info.rows:,}", cand, f.confidence)
    con.print(t)
    pd = sum(f.has_personal_data for f in findings)
    na = sum(not f.info.readable for f in findings)
    con.print(f"\nCollections: {len(findings)}   With personal data: {pd}   Without: {len(findings) - pd - na}   No access: {na}\n\n"
              f"Next step:\n  Davestigator connect map {name} --draft")


@connect_app.command("map")
def map_(name: str = typer.Argument(...), draft: bool = typer.Option(False, "--draft"), approve: bool = typer.Option(False, "--approve"),
         ai: bool = typer.Option(False, "--ai", help="With --draft: ask the local model to suggest categories (schema only).")):
    """--draft writes mappings.draft.yaml for review; --approve validates it into mappings.yaml."""
    if draft == approve:
        _fail("use exactly one of --draft or --approve", 1)
    s = _settings()
    draft_p, final_p = s.path(s.live.draft), s.path(s.live.mappings)
    con = _con()
    known = _known_categories(s)
    if draft:
        from .connectors.discovery import analyze, draft_collection

        conn = _open(s, name)
        try:
            conn.test()
            findings = [analyze(conn, i) for i in conn.discover()]
        except NotReadOnly as e:
            _fail(f"Role can modify data ({mask_text(str(e))}). Use a read-only role.", 4)
        finally:
            conn.close()
        cols = [c for c in (draft_collection(f) for f in findings) if c]
        ai_n = 0
        if ai:
            ai_n = _ai_suggest(s, cols, findings, known)
        m = load_mapping(draft_p)
        m.connectors[name] = ConnectorMapping(collections=cols)
        save_mapping(draft_p, m)
        unmapped = sum(1 for c in cols if c.category not in known and c.category != "IDENTITY_SOURCE")
        con.print(f"MAPPING DRAFT — {name}\n\nRule-based mappings:          {len(cols) - ai_n}\n"
                  f"AI-suggested (schema only):   {ai_n}\nNeeding a category decision:  {unmapped}\n\n"
                  f"Written: {draft_p.name}   (every entry marked  review: pending)\n\n"
                  "Nothing will be audited until the draft is approved:\n"
                  f"  edit {draft_p.name} (set review: approved, fix categories, add role: identity for the user table), then\n"
                  f"  Davestigator connect map {name} --approve")
        return
    m = load_mapping(draft_p)
    problems = approval_problems(m, name, known)
    con.print(f"MAPPING APPROVAL — {name}\n")
    cm = m.connectors.get(name)
    if cm:
        con.print(f"[{'✓' if not problems else '·'}] {len(cm.collections)} collections in the draft")
    for p in problems:
        con.print(f"[✗] {p}")
    if problems:
        con.print(f"\nRESULT: NOT APPROVED ({len(problems)} problems)")
        raise typer.Exit(2)
    final = load_mapping(final_p)
    final.connectors[name] = cm
    save_mapping(final_p, final)
    con.print(f"\nRESULT: APPROVED → {final_p.name} ({len(cm.collections)} collections, sha256 {mapping_hash(final_p)[:12]}…)")


def _ai_suggest(s, cols, findings, known) -> int:
    """Schema-only model suggestions for collections whose category the rules could not decide."""
    from .agent.client import ModelUnavailable, OllamaClient

    todo = [c for c in cols if c.category not in known]
    if not todo:
        return 0
    try:
        client = OllamaClient(model=s.agent.model, cache_dir=s.path(s.agent.cache_dir))
    except ModelUnavailable as e:
        make_console(_ui(), stderr=True).print(f"AI ASSISTANCE: UNAVAILABLE ({e}); rule-based draft only.")
        return 0
    schema = {"type": "object", "properties": {"category": {"type": "string", "enum": sorted(known)},
                                               "reason": {"type": "string"}}, "required": ["category", "reason"]}
    by_src = {f.info.source: f for f in findings}
    n = 0
    for c in todo:
        f = by_src[c.source]
        user = json.dumps({"collection": c.source, "columns": [{"name": x.name, "type": x.type} for x in f.info.columns],
                           "personal_data_columns": {k: v for k, v in c.identifiers.items()}})
        out, _rec = client.generate_json(task="map_schema", prompt_version="map_schema/v1",
                                         system="Pick the data category for this database table from the enum. You see the "
                                                "schema only. Answer JSON {category, reason}.", user=user, schema=schema)
        if isinstance(out, dict) and out.get("category") in known:
            from .connectors.discovery import CATEGORY_SYSTEM

            c.category = out["category"]
            c.system = CATEGORY_SYSTEM.get(c.category, c.system)
            c.suggested_by = f"model:{s.agent.model}"
            c.note = f"model suggestion (schema only): {str(out.get('reason', ''))[:120]}"
            n += 1
    return n


# ---------------------------------------------------------------------- #
def _auditor(s):
    from .connectors.live import LiveAuditor, load_rules

    mp = s.path(s.live.mappings)
    m = load_mapping(mp)
    if not any(c.review == "approved" for cm in m.connectors.values() for c in cm.collections):
        _fail(f"no approved mapping in {mp.name}. Run connect map NAME --draft, review it, then connect map NAME --approve.", 2)
    return LiveAuditor(s, m, mapping_hash(mp), load_rules(s.path(s.live.rules)))


def _read_requests(path: str):
    from .ingestion import load_requests

    return load_requests(Path(path))


@connect_app.command("snapshot")
def snapshot(name: Optional[str] = typer.Argument(None, help="Limit to one connector (default: all approved)."),
             subjects: str = typer.Option(..., "--subjects", help="CSV in deletion_requests.csv format."),
             out: str = typer.Option(..., "--out", help="Dataset folder to write (evidence/, manifest, identities).")):
    """Export the subjects' records into a dataset folder the offline audit reads."""
    from .connectors.live import LiveRun, export_snapshot

    s = _settings()
    if name:
        s.connectors = [c for c in s.connectors if c.name == name]
    aud = _auditor(s)
    for n, err in aud.open_errors.items():
        make_console(_ui(), stderr=True).print(f"WARNING: {n}: {err} (its collections are NOT CHECKED)")
    run = LiveRun(run_id=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"), started_at=datetime.now(timezone.utc))
    try:
        info = export_snapshot(aud, _read_requests(subjects), Path(out), run)
    finally:
        aud.close()
    gaps = [c for c in run.coverage if c.status != COVERAGE_FULL]
    con = _con()
    con.print(f"SNAPSHOT\n\nSubjects:              {info['subjects']}\nSystems in scope:      {info['systems_in_scope']}\n"
              f"Collections with hits:  {info['collections']}\nRecords exported:      {info['records']:,} (only records matching the subjects)\n"
              f"File:                  {info['file']}\nManifest updated:      {Path(out) / 'evidence_files.csv'} ({info['file_id']})")
    if info["dropped_refs"]:
        con.print(f"Note: {info['dropped_refs']} non-numeric user references were not representable offline; linking uses emails/phones/hashes.")
    for g in gaps:
        con.print(f"[?] {g.connector}:{g.source} {g.status} ({g.reason})")
    cfg_hint = f"--config {Path(out).name}.yaml"
    con.print(f"\nNext step:\n  write a config with dataset.root: {out} and run\n  Davestigator {cfg_hint} audit --all")


# ---------------------------------------------------------------------- #
def live_audit(email: str | None, requests: str | None, output: str | None, ui: UIState, cfg_path):
    from .connectors.live import LiveRun, live_request, write_journal
    from .reporting import save_case
    from .validation import read_output, validate_rows, write_output

    s = _settings()
    aud = _auditor(s)
    con = make_console(ui)
    run = LiveRun(run_id=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"), started_at=datetime.now(timezone.utc))
    for n, err in aud.open_errors.items():
        make_console(ui, stderr=True).print(f"WARNING: {n}: {err}; its collections are NOT CHECKED.")
    try:
        if email:
            q = live_request(email)
            if ui.fmt == "text":
                con.print(banner("DAVESTIGATOR — LIVE DELETION AUDIT"))
                con.print(f"Subject:     {mask_email(email)}\nAudit time:  {q.audit_at:%Y-%m-%d %H:%M:%S} UTC (now)\n"
                          f"Mode:        READ-ONLY · LIVE\nMapping:     {s.live.mappings} (sha256 {aud.mapping_sha[:12]}…)\n")
            r, cov = aud.audit(q, run)
            jpath = s.path(s.live.journal_dir) / f"run_{run.run_id}.jsonl"
            write_journal(jpath, run)
            rep = save_case(r, s.path(s.reports.directory) / "live")
            if ui.fmt == "json":
                from .reporting import case_to_dict

                con.print_json(json.dumps({**case_to_dict(r), "coverage": [c.__dict__ for c in cov], "journal": str(jpath)}))
                return
            by = {}
            for c in cov:
                by.setdefault(c.connector, []).append(c)
            for i, (n, cs) in enumerate(sorted(by.items()), 1):
                summary = " · ".join(f"{sum(1 for c in cs if c.status == st)} {st}" for st in ("FULL", "PARTIAL", "NOT_CHECKED")
                                     if any(c.status == st for c in cs))
                con.print(f"[{i}] Query {n} ({len({c.source for c in cs})} collections) .... {summary}")
            con.print(f"\nRESULT: {status_text(r.deletion_status)}\n")
            fs = [x for x in r.steps if x.phase.value == "FINDING"]
            if fs:
                con.print("FINDINGS")
                for f in fs:
                    con.print(f"[!] {f.operation.replace('REPORT_', '').replace('_', ' ').title():<18} {f.system_id:<20} {f.resource_id}   {f.evidence_id}")
            ok = [o for o in r.obligations if o.satisfied]
            for o in ok:
                con.print(f"[✓] {o.resource_id} ({o.category}) is {o.state} as required")
            gaps = [c for c in cov if c.status != COVERAGE_FULL]
            if gaps:
                con.print("\nCOVERAGE GAPS")
                for g in gaps:
                    con.print(f"[?] {g.connector}:{g.source:<28} {g.status} ({g.reason})")
            if r.identity and r.identity.ambiguities:
                for a in r.identity.ambiguities:
                    con.print(f"[?] identity: {mask_text(a)}")
            con.print(f"\nEvidence journal: {jpath}\nReport:           {rep}")
            return
        reqs = _read_requests(requests)
        rows, results = [], []
        for q in reqs:
            r, _ = aud.audit(q, run)
            results.append(r)
            rows.append((q.case_id, r.deletion_status, [x.to_json() for x in r.steps]))
        out = Path(output) if output else s.path("live_output.csv")
        tmp = out.with_suffix(out.suffix + ".tmp")
        write_output(tmp, rows)
        header, parsed = read_output(tmp)
        rep = validate_rows(header, parsed, [q.case_id for q in reqs], run.evidence_times,
                            {r.case_id: r.request.audit_at for r in results}, live=True)
        jpath = s.path(s.live.journal_dir) / f"run_{run.run_id}.jsonl"
        write_journal(jpath, run)
        if not rep.ok:
            for i in rep.issues[:20]:
                make_console(ui, stderr=True).print(f"Case: {i.case_id}\nProblem: {i.problem}\n{i.detail}\n")
            raise typer.Exit(3)
        tmp.replace(out)
        counts = {k: sum(1 for r in results if r.deletion_status == k) for k in ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE")}
        gaps = len({(c.connector, c.source) for c in run.coverage if c.status != COVERAGE_FULL})
        con.print(f"LIVE AUDIT SUMMARY\n\nRequests: {len(reqs)}  " + "  ".join(f"{k}: {v}" for k, v in counts.items()) +
                  f"\nCoverage gaps: {gaps}\nOutput: {out}\nEvidence journal: {jpath}\nValidation: PASSED")
        if gaps:
            raise typer.Exit(5)
    finally:
        aud.close()
