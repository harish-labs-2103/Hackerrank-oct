"""Application service: wires dataset, evidence index, model, and investigator together.

The CLI and the evaluation harness both go through here so they see identical behaviour.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .agent.client import ModelUnavailable, OllamaClient
from .agent.guard import GuardLog
from .agent.tasks import ImageExtractor, LLMIntentClassifier
from .config import Settings
from .engine import CaseResult, Investigator
from .evidence.registry import EvidenceIndex, FileStatus, build_index
from .identity import IdentityGraph
from .ingestion import Dataset, case_sort_key, load_dataset, load_requests
from .intent import IntentClassifier
from .models import DeletionRequest
from .validation import ValidationReport, read_output, validate_rows, write_output


@dataclass
class Session:
    settings: Settings
    ds: Dataset
    index: EvidenceIndex
    investigator: Investigator
    client: object | None
    guard_log: GuardLog
    ai_status: str  # ON | OFF | UNAVAILABLE:<reason>
    notices: list[str] = field(default_factory=list)


def open_session(settings: Settings, use_llm: bool | None = None,
                 on_file: Callable[[FileStatus], None] | None = None) -> Session:
    ds = load_dataset(settings.path(settings.dataset.root))
    log = GuardLog()
    client = None
    ai_status = "OFF"
    want = settings.agent.enabled != "off" if use_llm is None else use_llm
    if want:
        try:
            client = OllamaClient(model=settings.agent.model, cache_dir=settings.path(settings.agent.cache_dir),
                                  options={"num_ctx": settings.agent.num_ctx}, offline=settings.agent.offline)
            ai_status = "ON"
        except ModelUnavailable as e:
            ai_status = f"UNAVAILABLE: {e}"
    graph = IdentityGraph(ds.identities, ds.audit_log)
    known = {i for ids in graph.user_idents.values() for i in ids if i[0] != "name"}
    extractor = ImageExtractor(client, log) if client else None
    index = build_index(ds, image_extractor=extractor, known_identifiers=known, on_file=on_file)
    classifier = IntentClassifier(LLMIntentClassifier(client, log) if client else None)
    inv = Investigator(ds, index, classifier)
    return Session(settings, ds, index, inv, client, log, ai_status)


def requests_for(session: Session, sample: bool = False) -> list[DeletionRequest]:
    name = "sample_deletion_requests.csv" if sample else "deletion_requests.csv"
    return load_requests(session.ds.root / name)


def evidence_times(session: Session) -> dict:
    t = {fid: e.captured_at for fid, e in session.ds.manifest.items()}
    t.update({a.activity_id: a.event_time for a in session.ds.activities})
    t.update({a.audit_id: a.event_time for a in session.ds.audit_log})
    return t


def run_batch(session: Session, reqs: list[DeletionRequest], out_path: Path,
              on_case: Callable[[CaseResult | None, DeletionRequest, Exception | None], None] | None = None
              ) -> tuple[list[CaseResult], ValidationReport, list[tuple[str, str]]]:
    results: list[CaseResult] = []
    failed: list[tuple[str, str]] = []
    rows = []
    for q in sorted(reqs, key=lambda r: case_sort_key(r.case_id)):
        try:
            r = session.investigator.investigate(q)
            results.append(r)
            rows.append((q.case_id, r.deletion_status, [s.to_json() for s in r.steps]))
            if on_case:
                on_case(r, q, None)
        except Exception as e:  # noqa: BLE001 - a failed case is recorded, never dropped or marked COMPLETE
            failed.append((q.case_id, f"{type(e).__name__}: {e}"))
            rows.append((q.case_id, "INCOMPLETE", []))
            if on_case:
                on_case(None, q, e)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    write_output(tmp, rows)
    header, parsed = read_output(tmp)
    rep = validate_rows(header, parsed, [q.case_id for q in sorted(reqs, key=lambda r: case_sort_key(r.case_id))],
                        evidence_times(session), {q.case_id: q.audit_at for q in reqs})
    if rep.ok:
        tmp.replace(out_path)
    return results, rep, failed
