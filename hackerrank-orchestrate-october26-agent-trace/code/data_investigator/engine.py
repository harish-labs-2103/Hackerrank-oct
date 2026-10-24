"""Investigation engine: one DeletionRequest -> CaseResult.

Pipeline stages (each emits a StageEvent for the CLI):
  load request -> classify intent -> resolve identity -> collect evidence ->
  build timeline/state at audit_at -> evaluate policy -> plan steps -> validate
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from .activity import activity_observations, attempted_actions, identity_links
from .evidence.interpret import PREFIX_CATEGORY, resource_prefix, system_for
from .evidence.registry import EvidenceIndex
from .identity import IdentityGraph
from .ingestion import Dataset
from .intent import IntentClassifier, IntentLabel
from .models import DeletionRequest, Observation, ResolvedIdentity, Step
from .planner import PlannedStep, finalize
from .policy import PolicyEngine, Treatment

LINK_SLACK = timedelta(days=1)

STAGES = ["Load request", "Classify request", "Resolve identity", "Collect evidence",
          "Reconstruct timeline", "Evaluate rules", "Plan steps", "Validate findings"]

# More specific category wins when sources disagree about a resource.
CATEGORY_PRIORITY = ["SUPPORT_ATTACHMENT", "MARKETING_PROFILE", "ACCOUNT_TOMBSTONE", "LEGAL_HOLD_RECORD",
                     "SECURITY_AUDIT_LOG", "BACKUP_ARCHIVE", "DATA_FLOW", "USER_EXPORT", "SEARCH_INDEX_ENTRY",
                     "CACHE_ENTRY", "EMAIL_DELIVERY_LOG", "SUPPORT_MESSAGE", "ANALYTICS_PROFILE",
                     "PRODUCT_TELEMETRY", "USER_PROFILE", "SECONDARY_IDENTIFIER"]


@dataclass
class StageEvent:
    stage: str
    status: str  # DONE | FAILED | SKIPPED
    detail: str = ""


@dataclass
class Obligation:
    system_id: str
    resource_id: str
    category: str
    treatment: Treatment
    state: str | None  # state at audit_at, None = unknown
    pii: frozenset[str]
    satisfied: bool | None
    evidence: Observation | None
    link_sources: list[str] = field(default_factory=list)
    history: list[Observation] = field(default_factory=list)


@dataclass
class CaseResult:
    case_id: str
    request: DeletionRequest
    intent: IntentLabel | None
    identity: ResolvedIdentity | None
    internal_status: str
    deletion_status: str
    steps: list[Step]
    obligations: list[Obligation] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    excluded_after_audit: list[Observation] = field(default_factory=list)


class Investigator:
    def __init__(self, ds: Dataset, index: EvidenceIndex, classifier: IntentClassifier):
        self.ds = ds
        self.idx = index
        self.graph = IdentityGraph(ds.identities, ds.audit_log)
        self.policy = PolicyEngine(ds.rules)
        self.classifier = classifier
        for o in activity_observations(ds.activities):
            index.add(o)
        self.links = identity_links(ds.activities)
        self.attempted = attempted_actions(ds.activities)
        self.coverage_by_system: dict[str, list] = defaultdict(list)
        for c in index.coverage:
            self.coverage_by_system[c.system_id].append(c)
        self.activities_by_resource: dict[str, list] = defaultdict(list)
        for a in ds.activities:
            self.activities_by_resource[a.resource_id].append(a)

    # ------------------------------------------------------------------ #
    def investigate(self, req: DeletionRequest, on_stage: Callable[[StageEvent], None] | None = None) -> CaseResult:
        emit = on_stage or (lambda e: None)
        emit(StageEvent("Load request", "DONE", req.case_id))

        intent = self.classifier.classify(req.request_text)
        emit(StageEvent("Classify request", "DONE", intent.intent))
        if not intent.is_deletion:
            for s in STAGES[2:]:
                emit(StageEvent(s, "SKIPPED", "not a deletion request"))
            return CaseResult(req.case_id, req, intent, None, "NOT_APPLICABLE", "NOT_APPLICABLE", [])

        identity = self.graph.resolve(req.email)
        emit(StageEvent("Resolve identity", "DONE" if identity.user_id else "FAILED", identity.user_id or "unresolved"))
        gaps = list(identity.ambiguities)
        if not identity.user_id:
            for s in STAGES[3:]:
                emit(StageEvent(s, "SKIPPED", "identity unresolved"))
            return CaseResult(req.case_id, req, intent, identity, "NEEDS_REVIEW", "INCOMPLETE", [], gaps=gaps)

        linked = self._linked_resources(identity, req)
        for key in list(linked):
            conflict = self._identity_conflict(key, identity, req)
            if conflict:
                gaps.append(f"{key[1]}: excluded, {conflict}")
                del linked[key]
        emit(StageEvent("Collect evidence", "DONE", f"{len(linked)} resources"))

        obligations: list[Obligation] = []
        excluded: list[Observation] = []
        case_files = {src for srcs in linked.values() for src in srcs}
        for key, sources in sorted(linked.items()):
            ob, later = self._evaluate(key, sources, req.audit_at, case_files)
            excluded.extend(later)
            if ob is None:
                gaps.append(f"{key[1]}: no treatment rule for its category")
                continue
            obligations.append(ob)
            if ob.state is None:
                gaps.append(f"{key[1]}: state at audit_at not established by evidence")
        emit(StageEvent("Reconstruct timeline", "DONE", f"{len(excluded)} later observations excluded"))
        emit(StageEvent("Evaluate rules", "DONE", f"{sum(1 for o in obligations if o.satisfied is False)} violations"))

        steps, status = self._plan(obligations)
        emit(StageEvent("Plan steps", "DONE", f"{len(steps)} steps"))
        emit(StageEvent("Validate findings", "DONE"))
        return CaseResult(req.case_id, req, intent, identity, status, status, steps, obligations, gaps,
                          excluded_after_audit=excluded)

    # ------------------------------------------------------------------ #
    def _linked_resources(self, identity: ResolvedIdentity, req: DeletionRequest) -> dict[tuple[str, str], list[str]]:
        """Resources tied to the subject by evidence from this request's investigation window.

        A link must be observed between (requested_at - LINK_SLACK) and audit_at. Sightings long
        before the request belong to earlier episodes (e.g. a months-old lookup that returned a
        different profile) and say nothing about what this request had to remove.
        """
        start = req.requested_at - LINK_SLACK
        linked: dict[tuple[str, str], list[str]] = defaultdict(list)
        for i in identity.identifiers:
            for o in self.idx.by_identifier.get(i, []):
                # Identifiers read from screenshots by the vision model are unverified (they can be
                # attributed to the wrong row), so they never create a link on their own.
                if o.locator == "screenshot":
                    continue
                if start <= o.observed_at <= req.audit_at and system_for(o.resource_id):
                    linked[o.key()].append(o.source_id)
        for a, rid, ids, _ in self.links:
            if start <= a.event_time <= req.audit_at and ids & identity.identifiers and system_for(rid):
                linked[(system_for(rid), rid)].append(a.activity_id)
        return linked

    def _identity_conflict(self, key, identity: ResolvedIdentity, req: DeletionRequest) -> str | None:
        """A catalog link is contradicted when the record itself shows someone else.

        Only applies to resources linked solely by a catalog activity: if evidence of the record
        shows a person name, and that name is not one of the subject's names, the link is not
        trusted (never merge different people).
        """
        obs = [o for o in self.idx.by_resource.get(key, []) if o.observed_at <= req.audit_at]
        direct = any(i in identity.identifiers for o in obs for i in o.identifiers)
        if direct:
            return None
        subject_names = {v for k, v in self.graph.user_idents.get(identity.user_id, set()) if k == "name"}
        shown = {v for o in obs for k, v in o.identifiers if k == "name"}
        if shown and not (shown & subject_names):
            return "record shows a different person (" + ", ".join(sorted(shown)) + ") than the catalog link"
        return None

    def _category(self, obs: list[Observation], resource_id: str) -> str | None:
        cats = {o.category for o in obs if o.category}
        for a, rid, _, hint in self.links:
            if rid == resource_id and hint:
                cats.add(hint)
        if resource_id.startswith("analytics_") and self.attempted.get(resource_id) == {"DELETE_RECORD"}:
            cats.add("MARKETING_PROFILE")
        for c in CATEGORY_PRIORITY:
            if c in cats:
                return c
        p = resource_prefix(resource_id)
        return PREFIX_CATEGORY.get(p) if p else None

    def _evaluate(self, key, sources, audit_at: datetime, case_files: set[str] = frozenset()):
        system, rid = key
        all_obs = self.idx.by_resource.get(key, [])
        category = self._category(all_obs, rid)
        if category is None:
            return None, []
        t = self.policy.treatment(category)
        if t is None:
            return None, []
        # Admissible = observed and exported by audit_at. Lines stamped earlier inside a later
        # export can link a resource (see _linked_resources) but are not shown as evidence here.
        before = [o for o in all_obs if o.observed_at <= audit_at and o.captured_at <= audit_at]
        later = [o for o in all_obs if o.observed_at > audit_at or o.captured_at > audit_at]
        # State is decided only by sources that were available at audit_at (manifest captured_at).
        # A log line stamped before audit_at inside a later export may still *link* a resource,
        # but it cannot prove or be cited for the state at audit time.
        stateful = [o for o in before if o.state is not None and o.captured_at <= audit_at]
        if t.required_state == "ABSENT":
            stateful += self._absence_from_coverage(system, rid, category, audit_at, case_files)
        if not stateful:
            return Obligation(system, rid, category, t, None, frozenset(), None, None, sorted(set(sources)), before), later
        scored = sorted(stateful, key=lambda o: (o.observed_at, self._violates(t, o)))
        latest_at = scored[-1].observed_at
        tied = [o for o in scored if o.observed_at == latest_at]
        violating = [o for o in tied if self._violates(t, o)]
        chosen = sorted(violating or tied, key=lambda o: (o.captured_at > audit_at, o.source_id))[0]
        satisfied = not self._violates(t, chosen)
        return Obligation(system, rid, category, t, chosen.state, chosen.pii, satisfied, chosen,
                          sorted(set(sources)), before), later

    def _absence_from_coverage(self, system: str, rid: str, category: str, audit_at: datetime,
                               case_files: set[str]) -> list[Observation]:
        """A snapshot that declares it captured `system` in full, did capture this subject's other
        records, and does not contain `rid`, is positive evidence that the record was absent then.
        An empty or unrelated snapshot proves nothing about this subject."""
        out = []
        for c in self.coverage_by_system.get(system, []):
            if c.source_id not in case_files:
                continue
            if c.authoritative and c.captured_at <= audit_at and rid not in c.present:
                cap = self.ds.manifest[c.source_id].captured_at if c.source_id in self.ds.manifest else c.captured_at
                if cap > audit_at:
                    continue
                out.append(Observation(source_id=c.source_id, source_kind="file", observed_at=c.captured_at, captured_at=cap,
                                       system_id=system, resource_id=rid, category=category, state="ABSENT",
                                       locator=f"{c.copy} (not present in full snapshot)"))
        return out

    def _violates(self, t: Treatment, o: Observation) -> bool:
        s = o.state
        if t.required_state == "ABSENT":
            return s not in ("ABSENT",)
        if t.required_state == "DISABLED":
            return s == "ACTIVE"
        if t.required_state == "BLOCKED":
            return s == "RESTORABLE"
        if t.required_state == "PRESENT":
            return s == "ABSENT"
        if t.required_state in ("REDACTED", "ANONYMIZED"):
            if s in ("ABSENT", "REDACTED", "ANONYMIZED"):
                return False
            return self.policy.exposed(t.action, o.pii)
        return False

    # ------------------------------------------------------------------ #
    def _plan(self, obligations: list[Obligation]) -> tuple[list[Step], str]:
        violations = [o for o in obligations if o.satisfied is False]
        planned: list[PlannedStep] = []
        if violations:
            for o in violations:
                planned.append(PlannedStep(o.treatment.finding_op, o.system_id, o.resource_id, o.evidence.source_id))
                if o.treatment.remediation_op:
                    planned.append(PlannedStep(o.treatment.remediation_op, o.system_id, o.resource_id, ""))
            for o in obligations:
                planned.append(PlannedStep(o.treatment.verify_op, o.system_id, o.resource_id, ""))
            return finalize(planned), "INCOMPLETE"
        for o in obligations:
            if o.evidence is None:
                continue  # nothing admissible proves this state; reported as a gap, not a verification
            planned.append(PlannedStep(o.treatment.verify_op, o.system_id, o.resource_id, o.evidence.source_id))
        return finalize(planned), "COMPLETE"
