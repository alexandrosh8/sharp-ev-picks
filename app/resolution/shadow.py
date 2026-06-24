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

from collections.abc import Mapping, Sequence
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


# --- Betfair Exchange coverage (EXACT match -> pure presence/absence) ---------
# The Betfair consumption path (CLV_USE_BETFAIR_EXCHANGE) attaches a captured
# Betfair BACK close via an EXACT external_ref lookup ("betfair:"+pick_ref) — no
# alias table, no kickoff-window fuzz — so its readiness is a single yes/no per
# pick, NOT a match rate with diagnostic gaps. The two buckets that DO matter:
#   - has a "betfair:"-namespaced event at all (was the page ever captured?), and
#   - of those, does that event carry a USABLE BACK close inside the kickoff
#     window (the same SNAPSHOT_CLOSE_MAX_GAP gate the consumption path uses).


@dataclass(frozen=True)
class BetfairCoverageOutcome:
    """One pick's EXACT-match Betfair Exchange close coverage.

    ``has_betfair_event``: a ``"betfair:"+ref`` event exists for the fixture.
    ``has_usable_close``: that event carries an anchorable BACK close inside the
    kickoff window (implies ``has_betfair_event``). Both False = no Betfair page
    was ever captured for this fixture.
    """

    pick_id: int
    sport: str
    league: str | None
    has_betfair_event: bool
    has_usable_close: bool


@dataclass(frozen=True)
class BetfairCoverageReport:
    """Overall + per-sport/league Betfair Exchange close coverage. ``with_event``
    counts picks whose fixture has ANY captured Betfair event; ``with_close`` the
    subset whose event carries a usable BACK close (what the consumption path can
    actually attach).

    ``event_by_sport`` (GroupRate.matched = per-sport ``with_event`` count) keeps
    the report HONEST about a zero: a sport bucket with ``with_close`` 0 but
    ``with_event`` > 0 had Betfair pages captured but none usable this window (a
    thin-slate gap), whereas ``with_event`` 0 means NO Betfair page was ever
    captured for the sport (capture off / unwired) — a structural 0, not a thin
    slate. Defaults to ``()`` so older positional construction stays valid."""

    total: int
    with_event: int
    with_close: int
    by_sport: tuple[GroupRate, ...]
    by_league: tuple[GroupRate, ...]
    event_by_sport: tuple[GroupRate, ...] = ()

    @property
    def close_rate(self) -> float | None:
        return self.with_close / self.total if self.total else None

    def as_dict(self) -> dict[str, object]:
        def grp(g: GroupRate) -> dict[str, object]:
            return {
                "key": g.key,
                "total": g.total,
                "with_close": g.matched,
                "close_rate": g.match_rate,
            }

        def egrp(g: GroupRate) -> dict[str, object]:
            return {
                "key": g.key,
                "total": g.total,
                "with_event": g.matched,
                "event_rate": g.match_rate,
            }

        return {
            "total": self.total,
            "with_event": self.with_event,
            "with_close": self.with_close,
            "close_rate": self.close_rate,
            "by_sport": [grp(g) for g in self.by_sport],
            "by_league": [grp(g) for g in self.by_league],
            # Per-sport with_event: distinguishes a structural 0 (no Betfair page
            # captured for the sport) from a thin-slate 0 (pages captured, none
            # usable this window).
            "event_by_sport": [egrp(g) for g in self.event_by_sport],
        }


def summarize_betfair_coverage(
    outcomes: Sequence[BetfairCoverageOutcome],
) -> BetfairCoverageReport:
    """Aggregate Betfair coverage outcomes into overall + per-sport/league close
    rates. ``GroupRate.matched`` carries the ``with_close`` count per bucket.
    Empty input -> a zero report (rates ``None``, never a division by zero)."""
    with_event = sum(1 for o in outcomes if o.has_betfair_event)
    with_close = sum(1 for o in outcomes if o.has_usable_close)
    by_sport = _grouped([(o.sport, o.has_usable_close) for o in outcomes])
    by_league = _grouped([(o.league, o.has_usable_close) for o in outcomes if o.league is not None])
    # Per-sport with_event (matched = captured-event count) — the structural-vs-
    # thin-slate signal: with_event 0 for a sport = capture off/unwired for it.
    event_by_sport = _grouped([(o.sport, o.has_betfair_event) for o in outcomes])
    return BetfairCoverageReport(
        total=len(outcomes),
        with_event=with_event,
        with_close=with_close,
        by_sport=by_sport,
        by_league=by_league,
        event_by_sport=event_by_sport,
    )


# --- sharp-anchor coverage headline ------------------------------------------
# The "Sharp-anchor coverage (Pinnacle + Betfair)" panel showed a bare "—"
# because its only number came from the lazy panel body (fetched on first
# expand). This headline summarises the SAME per-sport capture lists the panel
# already serves — scraped-weighted across sports — into one always-populated
# "Betfair X% · Pinnacle Y%" string so the header carries real numbers up front.
#
#   Betfair rate = sum(captured) / sum(scraped)  — of our upcoming scraped
#                  fixtures, the share that also carry a captured Betfair
#                  Exchange archive event;
#   Pinnacle rate = sum(matched) / sum(scraped)  — of our upcoming scraped
#                  fixtures, the share that strict-match a captured Pinnacle
#                  close.
#
# Both rates are ``None`` only when nothing was scraped at all (rendered "n/a",
# never a misleading 0%).


def _sum_field(rows: Sequence[Mapping[str, object]], field: str) -> int:
    """Sum an integer ``field`` across capture rows, coercing missing/None/blank
    to 0 (repo rows are dicts; a thin window can omit a sport entirely)."""
    total = 0
    for row in rows:
        value = row.get(field)
        if not isinstance(value, int | float | str):
            continue  # None / unexpected type -> treat as 0
        try:
            total += int(value)
        except (TypeError, ValueError):
            continue
    return total


@dataclass(frozen=True)
class AnchorCoverage:
    """Scraped-weighted Betfair + Pinnacle coverage headline for the dashboard.

    ``*_scraped`` are the denominators (our upcoming scraped fixtures, summed
    across sports); ``betfair_captured`` / ``pinnacle_matched`` the numerators.
    Rates are ``None`` only when nothing was scraped (no false 0%)."""

    betfair_scraped: int
    betfair_captured: int
    pinnacle_scraped: int
    pinnacle_matched: int

    @property
    def betfair_rate(self) -> float | None:
        return self.betfair_captured / self.betfair_scraped if self.betfair_scraped else None

    @property
    def pinnacle_rate(self) -> float | None:
        return self.pinnacle_matched / self.pinnacle_scraped if self.pinnacle_scraped else None

    @staticmethod
    def _label(rate: float | None) -> str:
        return "n/a" if rate is None else f"{round(rate * 100)}%"

    def headline(self) -> str:
        """One-line "Betfair X% · Pinnacle Y%" — the panel-header summary."""
        return (
            f"Betfair {self._label(self.betfair_rate)} · Pinnacle {self._label(self.pinnacle_rate)}"
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "betfair_scraped": self.betfair_scraped,
            "betfair_captured": self.betfair_captured,
            "betfair_rate": self.betfair_rate,
            "pinnacle_scraped": self.pinnacle_scraped,
            "pinnacle_matched": self.pinnacle_matched,
            "pinnacle_rate": self.pinnacle_rate,
            "headline": self.headline(),
        }


def summarize_anchor_coverage(
    *,
    betfair_capture: Sequence[Mapping[str, object]],
    pinnacle_capture: Sequence[Mapping[str, object]],
) -> AnchorCoverage:
    """Aggregate the per-sport capture lists into the dashboard's scraped-weighted
    sharp-anchor coverage headline. Pure: numbers in, numbers out — no DB/IO.

    ``betfair_capture`` rows carry ``scraped``/``captured``; ``pinnacle_capture``
    rows carry ``scraped``/``matched`` (the shapes
    ``repositories.betfair_archive_capture_by_sport`` /
    ``pinnacle_archive_capture_by_sport`` already return)."""
    return AnchorCoverage(
        betfair_scraped=_sum_field(betfair_capture, "scraped"),
        betfair_captured=_sum_field(betfair_capture, "captured"),
        pinnacle_scraped=_sum_field(pinnacle_capture, "scraped"),
        pinnacle_matched=_sum_field(pinnacle_capture, "matched"),
    )
