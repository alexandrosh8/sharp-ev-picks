"""Edge detection over a batch of candidates. Pure module."""

from collections.abc import Iterable

from app.edge.gates import GateDecision, GatePolicy, PickCandidate, evaluate


def detect_edges(
    candidates: Iterable[PickCandidate],
    policy: GatePolicy,
) -> list[tuple[PickCandidate, GateDecision]]:
    """Evaluate every candidate; returns all decisions (accepted and rejected)."""
    return [(candidate, evaluate(candidate, policy)) for candidate in candidates]


def accepted_picks(
    candidates: Iterable[PickCandidate],
    policy: GatePolicy,
) -> list[tuple[PickCandidate, GateDecision]]:
    """Only the candidates that passed every gate."""
    return [(c, d) for c, d in detect_edges(candidates, policy) if d.accepted]
