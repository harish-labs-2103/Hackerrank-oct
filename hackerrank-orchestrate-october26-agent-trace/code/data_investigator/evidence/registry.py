"""Global evidence index: parse every manifest file once, index observations.

Pass 1 parses structured sources (SQL, JSON, logs, text). Pass 2 handles artifacts (PDFs,
screenshots); artifact rows are only accepted when their resource ids are grounded in pass-1
evidence or the activity stream.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from ..ingestion import Dataset
from ..models import Observation
from .artifacts import image_observations, pdf_observations
from .interpret import system_for
from .parsers import Coverage, ParseResult, parse_json, parse_log, parse_pdf, parse_sql, parse_text

PARSERS = {".sql": parse_sql, ".json": parse_json, ".log": parse_log, ".txt": parse_text, ".pdf": parse_pdf}
IMAGE_EXT = {".png", ".jpg", ".jpeg"}
_ALIAS = re.compile(r"(?:linked|confirmed) (\S+) \(([^)]+)\)")


@dataclass
class FileStatus:
    file_id: str
    path: str
    kind: str
    status: str  # PARSED | FAILED | UNSUPPORTED | MISSING | NEEDS_AI | AI_EXTRACTED
    observations: int = 0
    errors: list[str] = field(default_factory=list)


class EvidenceIndex:
    def __init__(self):
        self.observations: list[Observation] = []
        self.by_resource: dict[tuple[str, str], list[Observation]] = defaultdict(list)
        self.by_identifier: dict[tuple[str, str], list[Observation]] = defaultdict(list)
        self.coverage: list[Coverage] = []
        self.files: dict[str, FileStatus] = {}
        self.texts: dict[str, str] = {}

    def add(self, obs: Observation):
        self.observations.append(obs)
        self.by_resource[obs.key()].append(obs)
        for i in obs.identifiers:
            self.by_identifier[i].append(obs)

    def resource_ids(self) -> set[str]:
        return {rid for _, rid in self.by_resource}


def build_index(ds: Dataset, image_extractor=None, known_identifiers: set | None = None,
                on_file=None) -> EvidenceIndex:
    idx = EvidenceIndex()
    artifacts = []
    for fid, entry in sorted(ds.manifest.items(), key=lambda kv: int(kv[0].rsplit("_", 1)[-1]) if kv[0].rsplit("_", 1)[-1].isdigit() else 0):
        path = ds.evidence_path(entry)
        ext = path.suffix.lower()
        st = FileStatus(fid, entry.file_path, ext.lstrip("."), "PARSED")
        idx.files[fid] = st
        if not path.is_file():
            st.status = "MISSING"
            continue
        if ext in IMAGE_EXT or ext == ".pdf":
            artifacts.append((fid, entry, path, ext))
            continue
        parser = PARSERS.get(ext)
        if parser is None:
            st.status = "UNSUPPORTED"
            continue
        res: ParseResult = parser(path, entry)
        _absorb(idx, st, res)
        if on_file:
            on_file(st)

    # Grounding for artifacts: ids seen in structured evidence or in the activity stream.
    grounded = idx.resource_ids() | {a.resource_id for a in ds.activities}
    aliases: dict[str, str] = {}
    link_hints: dict[str, tuple[str, str | None]] = {}
    for a in ds.activities:
        if a.event_type != "LINK_RESOURCE_IDENTITY":
            continue
        m = _ALIAS.search(a.details)
        if m and m.group(2) != m.group(1):
            aliases[m.group(2)] = m.group(1)
        hint = "SUPPORT_ATTACHMENT" if "belongs only to" in a.details else None
        link_hints[a.resource_id] = (system_for(a.resource_id) or a.target_system, hint)

    for fid, entry, path, ext in artifacts:
        st = idx.files[fid]
        if ext == ".pdf":
            res = parse_pdf(path, entry)
            art = pdf_observations(entry, res.text, aliases)
            res.observations.extend(art.observations)
            if res.text:
                idx.texts[fid] = res.text
            _absorb(idx, st, res)
            if art.status:
                st.status = art.status
        else:
            if image_extractor is None:
                st.status = "NEEDS_AI"
            else:
                out = image_extractor.extract(fid, path, grounded, known_identifiers or set(), aliases)
                if out is None:
                    st.status = "NEEDS_AI"
                else:
                    rows, _title = out
                    art = image_observations(entry, rows, link_hints)
                    for o in art.observations:
                        idx.add(o)
                    st.observations = len(art.observations)
                    st.status = "AI_EXTRACTED"
        if on_file:
            on_file(st)
    return idx


def _absorb(idx: EvidenceIndex, st: FileStatus, res: ParseResult):
    for o in res.observations:
        idx.add(o)
    idx.coverage.extend(res.coverage)
    st.observations = len(res.observations)
    st.errors = res.errors
    if res.errors and not res.observations:
        st.status = "FAILED"
