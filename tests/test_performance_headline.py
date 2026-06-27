"""P2-1: headline min-n suppression in _aggregate_settled (pure helper).

The blended headline roi / beat_close_rate / stake-weighted CLV had no min-n
guard — a 10-pick -8.7% read as signal. Below MIN_HEADLINE_N the point
estimates are nulled at the source and flagged roi_status="insufficient"; the
honest denominators (n_settled, counts, totals) survive. The trusted sharp
subset is gated independently on its own n (n_sharp_close).

Pure: _aggregate_settled takes plain row tuples, so no DB is needed.
"""

from decimal import Decimal

from app.storage.repositories import (
    MIN_HEADLINE_N,
    _aggregate_settled,
    _aggregate_settled_by_sport,
)


def _row(
    outcome: str = "won",
    pnl: float = 1.0,
    stake: float = 10.0,
    clv_log: float | None = 0.02,
    beat_close: bool | None = True,
    closing_odds: float | None = 2.0,
    closing_anchor: str | None = "pinnacle",
    close_independent: bool | None = True,
    has_snapshot_close: bool | None = True,
    decimal_odds: float | None = 2.0,
    closing_fair_probability: float | None = None,
) -> tuple[object, ...]:
    # (outcome, pnl, stake, clv_log, beat_close, closing_odds, closing_anchor,
    #  close_independent, has_snapshot_close, decimal_odds,
    #  closing_fair_probability) — the tuple shape performance_report._tier_rows
    #  builds. decimal_odds + closing_fair_probability feed the CLV-1 implausible
    #  close-implied-edge guard (defaults to a benign small edge / unknown).
    return (
        outcome,
        Decimal(str(pnl)),
        Decimal(str(stake)),
        Decimal(str(clv_log)) if clv_log is not None else None,
        beat_close,
        Decimal(str(closing_odds)) if closing_odds is not None else None,
        closing_anchor,
        close_independent,
        has_snapshot_close,
        Decimal(str(decimal_odds)) if decimal_odds is not None else None,
        Decimal(str(closing_fair_probability)) if closing_fair_probability is not None else None,
    )


def test_headline_suppressed_below_min_n() -> None:
    # 10 settled picks (< MIN_HEADLINE_N=50): a -8.7% ROI here is NOISE. The
    # numeric roi/beat_close_rate/CLV are nulled and flagged insufficient.
    agg = _aggregate_settled([_row(outcome="lost", pnl=-0.87) for _ in range(10)])
    assert agg["n_settled"] == 10
    assert agg["roi_status"] == "insufficient"
    assert agg["roi"] is None  # numeric headline suppressed at the source
    assert agg["stake_weighted_clv_log"] is None
    assert agg["beat_close_rate"] is None
    # honest denominators survive so the dashboard can render the "n too small" state
    assert agg["lost"] == 10
    assert Decimal(agg["total_staked"]) == Decimal("100")  # 10 * 10.0 stake
    assert agg["min_headline_n"] == MIN_HEADLINE_N


def test_headline_reported_at_or_above_min_n() -> None:
    # Exactly MIN_HEADLINE_N settled picks: the headline is now trustworthy
    # enough to report — roi_status flips to "ok" and the numeric roi appears.
    agg = _aggregate_settled([_row(outcome="won", pnl=1.0) for _ in range(MIN_HEADLINE_N)])
    assert agg["n_settled"] == MIN_HEADLINE_N
    assert agg["roi_status"] == "ok"
    assert agg["roi"] is not None
    assert agg["roi"] == "0.1"  # 50 * 1.0 pnl / (50 * 10.0 staked)
    assert agg["stake_weighted_clv_log"] is not None


def test_sharp_only_close_with_no_soft_price_enters_trusted_subset() -> None:
    # clv-1: a close anchored by a sharp book that NO soft book quoted has
    # closing_odds=None (there is no soft display price), yet it is a GENUINE
    # snapshot close (has_snapshot_close=True). Gating on closing_odds wrongly
    # excluded it; gating on has_snapshot_close admits it to the trusted subset.
    rows = [
        _row(closing_odds=None, has_snapshot_close=True, clv_log=0.04, close_independent=True)
        for _ in range(MIN_HEADLINE_N)
    ]
    agg = _aggregate_settled(rows)
    assert agg["n_sharp_close"] == MIN_HEADLINE_N  # sharp-only closes counted
    assert agg["sharp_status"] == "ok"
    assert agg["sharp_stake_weighted_clv_log"] == "0.04"


def test_revalidation_fallback_close_excluded_even_with_soft_price() -> None:
    # clv-1 converse: a poll-time revalidation FALLBACK close (no snapshot close,
    # has_snapshot_close=False) is NOT trusted even though a soft closing_odds
    # exists — closing_odds is now purely a display price, not the gate.
    rows = [_row(closing_odds=2.0, has_snapshot_close=False) for _ in range(MIN_HEADLINE_N)]
    agg = _aggregate_settled(rows)
    assert agg["n_sharp_close"] == 0


def test_sharp_tagged_but_self_priced_close_excluded_from_trusted_subset() -> None:
    # WELWALO REGRESSION PIN (data-integrity, lever-2 safe subset): a close that
    # is anchor_type='sharp' but was priced by the pick's OWN source
    # (close_independent_of_fill=False — circular, closing==fill, |clv_log|~0) is
    # FAKE CLV. It must NEVER enter the trusted sharp subset even though it is a
    # genuine snapshot close (has_snapshot_close=True) carrying a sharp anchor tag.
    # This is exactly the obscure-league self-priced 'sharp' case (e.g. an
    # Ethiopian-league pick with no real Pinnacle/Betfair close): the tag exists
    # but the close cannot be trusted. Existing behavior is correct; this pins it
    # so a future refactor of the gate cannot silently re-admit the fake and
    # inflate n_sharp / the proof-of-edge headline.
    fakes = [
        _row(closing_anchor="sharp", has_snapshot_close=True, close_independent=False)
        for _ in range(MIN_HEADLINE_N)
    ]
    agg_fakes = _aggregate_settled(fakes)
    assert agg_fakes["n_sharp_close"] == 0  # circular self-priced closes never counted
    assert agg_fakes["sharp_status"] == "insufficient"

    # CONVERSE (lever-2 test 3): a GENUINE independent sharp close IS counted, so
    # the exclusion is targeted at circularity, not at the 'sharp' tag itself.
    genuine = [
        _row(closing_anchor="sharp", has_snapshot_close=True, close_independent=True)
        for _ in range(MIN_HEADLINE_N)
    ]
    agg_genuine = _aggregate_settled(genuine)
    assert agg_genuine["n_sharp_close"] == MIN_HEADLINE_N
    assert agg_genuine["sharp_status"] == "ok"


def test_blended_clv_excludes_implausible_close_implied_edge() -> None:
    # CLV-1: a settled pick whose close-implied edge is physically impossible
    # (closing_fair_probability - 1/decimal_odds = 0.891433 - 1/6.50 = 0.737 >>
    # value_max_edge=0.20) is the favorite-prob-on-underdog-leg residue of the
    # since-fixed double-chance orientation bug. Its outcome/pnl stay counted (it
    # IS a real settled pick) but its fabricated clv_log (1.756877) and beat_close
    # must NOT inflate the blended stake_weighted_clv_log / beat_close_rate.
    honest = [
        _row(
            outcome="lost",
            pnl=0.0,
            clv_log=0.0,
            beat_close=False,
            decimal_odds=2.0,
            closing_fair_probability=0.50,
        )
        for _ in range(MIN_HEADLINE_N)
    ]
    poison = _row(
        outcome="won",
        pnl=5.5,
        stake=10.0,
        clv_log=1.756877,
        beat_close=True,
        decimal_odds=6.50,
        closing_fair_probability=0.891433,
    )
    agg = _aggregate_settled(honest + [poison])
    # The poison row survives as a real settled pick (honest denominator).
    assert agg["n_settled"] == MIN_HEADLINE_N + 1
    assert agg["won"] == 1
    assert Decimal(agg["total_pnl"]) == Decimal("5.5")
    # ... but its fabricated CLV/beat_close are dropped: only the 50 honest zeros
    # remain, so the blended headline reads 0, not a flattered positive.
    assert agg["stake_weighted_clv_log"] == "0"
    assert agg["beat_close_rate"] == "0"


def test_implausible_clv_excluded_from_trusted_sharp_subset() -> None:
    # Defense-in-depth: even a sharp-anchored, independent, genuine-snapshot close
    # cannot enter the trusted sharp subset if its close-implied edge is impossible
    # — fabricated CLV must never reach the proof-of-edge headline by any path.
    honest = [
        _row(
            closing_anchor="pinnacle",
            clv_log=0.03,
            decimal_odds=2.0,
            closing_fair_probability=0.51,
            close_independent=True,
            has_snapshot_close=True,
        )
        for _ in range(MIN_HEADLINE_N)
    ]
    poison = _row(
        closing_anchor="pinnacle",
        clv_log=1.5,
        decimal_odds=6.50,
        closing_fair_probability=0.891433,
        close_independent=True,
        has_snapshot_close=True,
    )
    agg = _aggregate_settled(honest + [poison])
    # 50 honest sharp closes counted; the impossible one excluded.
    assert agg["n_sharp_close"] == MIN_HEADLINE_N
    assert agg["sharp_stake_weighted_clv_log"] == "0.03"


def test_clv_log_fallback_excludes_when_close_edge_uncomputable() -> None:
    # Fallback path: when closing_fair_probability is absent the close-implied edge
    # cannot be computed, so an implausibly large |clv_log| (1.76) is itself the
    # tripwire. A genuinely small clv_log with no fair prob is kept.
    big = _aggregate_settled(
        [_row(clv_log=1.76, closing_fair_probability=None) for _ in range(MIN_HEADLINE_N)]
    )
    assert big["stake_weighted_clv_log"] == "0" or big["stake_weighted_clv_log"] is None
    assert big["n_sharp_close"] == 0  # all fabricated -> none trusted

    ok = _aggregate_settled(
        [_row(clv_log=0.05, closing_fair_probability=None) for _ in range(MIN_HEADLINE_N)]
    )
    assert ok["stake_weighted_clv_log"] == "0.05"
    assert ok["n_sharp_close"] == MIN_HEADLINE_N


def test_plausible_large_clv_is_not_excluded() -> None:
    # Converse guard: a row with a real, in-bounds close-implied edge (0.55 - 1/2.0
    # = 0.05 <= 0.20) and a normal clv_log is NOT touched — the guard targets
    # impossibility, not merely positive CLV.
    rows = [
        _row(clv_log=0.10, decimal_odds=2.0, closing_fair_probability=0.55)
        for _ in range(MIN_HEADLINE_N)
    ]
    agg = _aggregate_settled(rows)
    assert agg["stake_weighted_clv_log"] == "0.1"


def test_by_sport_split_aggregates_and_suppresses_per_sport() -> None:
    # PER-SPORT split: soccer has enough settled picks to report a headline ROI;
    # basketball has only 3 — gated on its OWN n (MIN_HEADLINE_N), so it reads
    # insufficient. A thin/experimental sport can never borrow soccer's n.
    soccer = [("soccer", _row(outcome="won", pnl=1.0)) for _ in range(MIN_HEADLINE_N)]
    basketball = [("basketball", _row(outcome="lost", pnl=-0.5)) for _ in range(3)]
    by_sport = _aggregate_settled_by_sport(soccer + basketball)
    assert set(by_sport) == {"soccer", "basketball"}
    assert by_sport["soccer"]["roi_status"] == "ok"
    assert by_sport["soccer"]["roi"] is not None
    assert by_sport["basketball"]["roi_status"] == "insufficient"
    assert by_sport["basketball"]["roi"] is None  # nulled at the source
    assert by_sport["basketball"]["n_settled"] == 3  # honest denominator survives


def test_by_sport_split_empty_when_no_rows() -> None:
    assert _aggregate_settled_by_sport([]) == {}


def test_sharp_subset_gated_on_its_own_n_not_n_settled() -> None:
    # A big settled population (headline OK) but only a FEW genuine sharp closes:
    # the sharp metrics stay suppressed on their own n_sharp_close floor — a
    # thin trusted subset must not borrow the headline's sufficiency.
    rows = [_row(closing_anchor="consensus", closing_odds=2.0) for _ in range(MIN_HEADLINE_N)]
    rows += [_row(closing_anchor="pinnacle") for _ in range(3)]  # only 3 sharp closes
    agg = _aggregate_settled(rows)
    assert agg["roi_status"] == "ok"  # headline has enough n
    assert agg["n_sharp_close"] == 3
    assert agg["sharp_status"] == "insufficient"
    assert agg["sharp_stake_weighted_clv_log"] is None
    assert agg["sharp_beat_close_rate"] is None
