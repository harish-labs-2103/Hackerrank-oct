"""Turn obligations into ordered challenge steps.

Ordering is fixed by the contract: phase, then operation order from the problem statement,
then system_id, then resource_id. Never sort operation names alphabetically.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import OP_RANK, OPERATIONS, PHASE_RANK, Step


@dataclass(frozen=True)
class PlannedStep:
    operation: str
    system_id: str
    resource_id: str
    evidence_id: str

    def sort_key(self):
        phase, _ = OPERATIONS[self.operation]
        return (PHASE_RANK[phase], OP_RANK[self.operation], self.system_id, self.resource_id)


def finalize(planned: list[PlannedStep]) -> list[Step]:
    seen: set[tuple[str, str, str]] = set()
    uniq: list[PlannedStep] = []
    for p in sorted(planned, key=PlannedStep.sort_key):
        k = (p.operation, p.system_id, p.resource_id)
        if k in seen:
            continue
        seen.add(k)
        uniq.append(p)
    steps = []
    for n, p in enumerate(uniq, 1):
        phase, state = OPERATIONS[p.operation]
        steps.append(Step(step_no=n, phase=phase, system_id=p.system_id, resource_id=p.resource_id,
                          operation=p.operation, state=state, evidence_id=p.evidence_id))
    return steps
