"""Pure shadow-resolution aggregation — match-rate diagnostics, NO IO.

Answers ADR-0014's open question BEFORE ``CLV_USE_PINNACLE_ARCHIVE`` is flipped
on: of the picks we could attach a Pinnacle archive close to, what fraction does
the STRICT matcher actually resolve? A low rate is diagnosed, never guessed:

- ``no_archive_candidates`` — the ``pinnacle_<sport>`` archive has no event in
  the pick's kickoff window: a COVERAGE gap (capture more; enable
  ``ARCADIA_ENABLED``);
- ``unmatched_with_candidates`` — archive events exist in the window but the
  strict matcher rejected them all: an ALIAS/ambiguity gap (extend the alias
  table in ``aliases_seed.json``).

The DB read that produces ``ShadowOutcome`` rows lives in
``app.storage.repositories.shadow_match_rate_outcomes`` (the impure half); this
module only aggregates, so it stays inside the project's pure-math boundary
(numpy/stdlib only — no env/DB/HTTP).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

# Sport keys whose ``pinnacle_<sport>`` archive namespace IS the key itself;
# every other key (e.g. "basketball_nba") takes its first underscore segment.
# Single source of truth shared with app.clv_trueup._pinnacle_archive_close.
ARCADIA_SPORTS = frozenset({"soccer", "tennis", "basketball", "american_football"})


def arcadia_base_sport(sport_key: str) -> str:
    """The ``pinnacle_<base>`` base for a sport key: the key itself when already
    a base arcadia sport, else its first underscore segment
    ("basketball_nba" -> "basketball")."""
    return sport_key if sport_key in ARCADIA_SPORTS else sport_key.split("_", 1)[0]


@dataclass(frozen=True)
class ShadowOutcome:
    """One pick's shadow attempt to attach a Pinnacle archive close.

    ``candidates_in_window``: ``pinnacle_<sport>`` archive events inside the
    pick's kickoff window (0 = no coverage). ``matched``: the STRICT matcher
    found a UNIQUE archive event (a real, attachable close).
    """

    pick_id: int
    sport: str
    league: str | None
    candidates_in_window: int
    matched: bool


@dataclass(frozen=True)
class GroupRate:
    """Match rate for one sport or league bucket."""

    key: str
    total: int
    matched: int

    @property
    def match_rate(self) -> float | None:
        return self.matched / self.total if self.total else None


@dataclass(frozen=True)
class MatchRateReport:
    """Overall + per-sport + per-league strict match rates, with the two
    diagnostic buckets that explain a low rate (coverage vs alias gap)."""

    total: int
    matched: int
    no_archive_candidates: int
    unmatched_with_candidates: int
    by_sport: tuple[GroupRate, ...]
    by_league: tuple[GroupRate, ...]

    @property
    def match_rate(self) -> float | None:
        return self.matched / self.total if self.total else None

    def as_dict(self) -> dict[str, object]:
        def grp(g: GroupRate) -> dict[str, object]:
            return {
                "key": g.key,
                "total": g.total,
                "matched": g.matched,
                "match_rate": g.match_rate,
            }

        return {
            "total": self.total,
            "matched": self.matched,
            "match_rate": self.match_rate,
            "no_archive_candidates": self.no_archive_candidates,
            "unmatched_with_candidates": self.unmatched_with_candidates,
            "by_sport": [grp(g) for g in self.by_sport],
            "by_league": [grp(g) for g in self.by_league],
        }


def _grouped(pairs: Sequence[tuple[str, bool]]) -> tuple[GroupRate, ...]:
    """(key, matched) pairs -> per-key GroupRate tuple, sorted by key for a
    deterministic report ordering."""
    agg: dict[str, list[int]] = {}
    for key, matched in pairs:
        bucket = agg.setdefault(key, [0, 0])
        bucket[0] += 1
        if matched:
            bucket[1] += 1
    return tuple(GroupRate(key=k, total=t, matched=m) for k, (t, m) in sorted(agg.items()))


def summarize_match_rate(outcomes: Sequence[ShadowOutcome]) -> MatchRateReport:
    """Aggregate shadow outcomes into overall + per-sport/league rates and the
    coverage-vs-alias diagnostic split. Empty input -> a zero report (rates
    ``None``, never a division by zero)."""
    matched = sum(1 for o in outcomes if o.matched)
    no_archive = sum(1 for o in outcomes if not o.matched and o.candidates_in_window == 0)
    unmatched_with = sum(1 for o in outcomes if not o.matched and o.candidates_in_window > 0)
    by_sport = _grouped([(o.sport, o.matched) for o in outcomes])
    by_league = _grouped([(o.league, o.matched) for o in outcomes if o.league is not None])
    return MatchRateReport(
        total=len(outcomes),
        matched=matched,
        no_archive_candidates=no_archive,
        unmatched_with_candidates=unmatched_with,
        by_sport=by_sport,
        by_league=by_league,
    )
