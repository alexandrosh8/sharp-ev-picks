"""H6 agreement predicate — Pinnacle-AND-consensus agreement gate (ADR-0019 H6).

PURE module (stdlib only — no env/DB/HTTP/log side effects). Used ONLY by
offline evaluation code (scripts/value_backtest.py ``--frozen-eval`` H6 row and
the read-only research replay). Nothing in live pick minting (app/pipeline.py,
app/edge/value.py) may import this module — a test enforces that. This system
places no bets.

Semantics (frozen for the pre-registered H6 row):

- ``anchor_probs`` is the fair-probability mapping (selection -> prob) from the
  SHARP ANCHOR (e.g. devig(Pinnacle)); ``reference_probs`` is the mapping from a
  SECOND, independent source (soft-consensus median or Betfair).
- CALLER CONTRACT: the reference source must NEVER be the anchor source itself
  (an anchor trivially agrees with itself and the gate becomes a no-op). This
  cannot be verified from the probability values alone, so it is a documented
  caller obligation, mirrored where the callers build the reference.
- ``passes`` is TOLERANCE-ONLY: the two sources agree iff
  ``|anchor[sel] - reference[sel]| <= tolerance`` (absolute probability space).
- Reasons form a CLOSED vocabulary (``REASON_VOCABULARY``); callers must not
  invent new strings:
    * ``agree``                   — within tolerance; passes=True.
    * ``reference_missing``       — no reference mapping at all (None/empty).
    * ``selection_missing``       — a mapping exists but lacks the selection
                                    (on either side).
    * ``delta_exceeds_tolerance`` — |delta| > tolerance.
    * ``direction_conflict``      — |delta| > tolerance AND the two sources sit
                                    on STRICTLY opposite sides of the uniform
                                    prior 1/n (they disagree whether the
                                    selection is favoured relative to a uniform
                                    market). A severity sub-label of
                                    disagreement — it never changes pass/fail,
                                    which stays tolerance-only. n = the anchor
                                    outcome count when the anchor carries a full
                                    outcome set (>=2), else the reference's,
                                    else 2 (both single-entry; documented
                                    heuristic).

FAIL-CLOSED semantics for the H2 backtest H6 row: ``reference_missing`` means
the row is EXCLUDED from the H6 variant entirely — it is neither a pass nor a
fail (counting it as a fail would let reference coverage, not agreement, drive
the variant), and the exclusion count MUST be reported alongside the retained
n. ``selection_missing`` is likewise an exclusion in that context.

TOLERANCE PROVENANCE: ADR-0019 freezes the H6 tolerance "at the value recorded
in the research log", but no numeric value was ever recorded in
docs/research/2026-06-30-sharp-vs-soft-calibrate-optimize.md (verified
2026-07-04). ``H6_TOLERANCE = 0.02`` (absolute probability) is a
PROPOSAL, explicitly labelled as such by every consumer — it is NOT a frozen
pre-registered value and must be recorded/signed before any acceptance run.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

# Proposed (NOT pre-registered — see module docstring) absolute-probability
# tolerance for the H6 agreement gate.
# SIGNED 2026-07-04 (operator session directive; ADR-0019 sign-off record).
H6_TOLERANCE = 0.02

REASON_AGREE = "agree"
REASON_REFERENCE_MISSING = "reference_missing"
REASON_DIRECTION_CONFLICT = "direction_conflict"
REASON_DELTA_EXCEEDS_TOLERANCE = "delta_exceeds_tolerance"
REASON_SELECTION_MISSING = "selection_missing"

# The closed reason vocabulary — every verdict's reason is one of these.
REASON_VOCABULARY: frozenset[str] = frozenset(
    {
        REASON_AGREE,
        REASON_REFERENCE_MISSING,
        REASON_DIRECTION_CONFLICT,
        REASON_DELTA_EXCEEDS_TOLERANCE,
        REASON_SELECTION_MISSING,
    }
)


@dataclass(frozen=True)
class AgreementVerdict:
    """Outcome of the H6 agreement predicate for one selection.

    ``delta`` = anchor_prob - reference_prob (signed, absolute-probability
    space); None when it cannot be computed (missing reference/selection)."""

    passes: bool
    reason: str
    delta: float | None


def agreement_verdict(
    anchor_probs: Mapping[str, float],
    reference_probs: Mapping[str, float] | None,
    selection: str,
    tolerance: float,
) -> AgreementVerdict:
    """H6 agreement predicate — do two independent fair sources agree on
    ``selection`` within ``tolerance`` (absolute probability)?

    See the module docstring for the full frozen semantics, the closed reason
    vocabulary, the caller contract (reference is never the anchor source
    itself) and the fail-closed exclusion rule for ``reference_missing``.
    """
    if not (tolerance >= 0.0):  # also rejects NaN
        raise ValueError(f"tolerance must be a non-negative number, got {tolerance!r}")
    if not reference_probs:
        return AgreementVerdict(passes=False, reason=REASON_REFERENCE_MISSING, delta=None)
    if selection not in anchor_probs or selection not in reference_probs:
        return AgreementVerdict(passes=False, reason=REASON_SELECTION_MISSING, delta=None)

    anchor_p = float(anchor_probs[selection])
    reference_p = float(reference_probs[selection])
    delta = anchor_p - reference_p
    if abs(delta) <= tolerance:
        return AgreementVerdict(passes=True, reason=REASON_AGREE, delta=delta)

    # Disagreement: sub-label a strict favoured-vs-unfavoured flip relative to
    # the uniform prior 1/n (severity only — pass/fail already decided above).
    if len(anchor_probs) >= 2:
        n_outcomes = len(anchor_probs)
    elif len(reference_probs) >= 2:
        n_outcomes = len(reference_probs)
    else:
        n_outcomes = 2  # both single-entry: documented heuristic fallback
    uniform = 1.0 / n_outcomes
    anchor_side = anchor_p - uniform
    reference_side = reference_p - uniform
    if anchor_side * reference_side < 0.0:
        return AgreementVerdict(passes=False, reason=REASON_DIRECTION_CONFLICT, delta=delta)
    return AgreementVerdict(passes=False, reason=REASON_DELTA_EXCEEDS_TOLERANCE, delta=delta)
