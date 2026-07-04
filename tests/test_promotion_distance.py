"""B1 promotion-distance cells + B4 weekly steam counts (pure helpers — no DB).

``promotion_distance_cells`` reuses the trusted sharp-close gate
(``_settled_close_is_trusted`` — the same guards as ``_aggregate_settled``'s
trusted subset) and must NEVER emit a CLV point estimate below the
``SPORT_MARKET_OK_N`` floor: sub-floor cells carry denominators + status only.
The days-to-threshold figure is a linear extrapolation of the trailing
cadence window and reads None (dashboard "—") when no cadence exists —
never a guess.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.storage.repositories import (
    PROMOTION_CADENCE_WINDOW_DAYS,
    SPORT_MARKET_OK_N,
    _weekly_steam_counts,
    promotion_distance_cells,
)

NOW = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)


def _row(
    sport: str = "soccer",
    market: str = "h2h",
    settled_at: datetime | None = NOW - timedelta(days=1),
    clv_log: float | None = 0.03,
    closing_anchor: str | None = "pinnacle",
    close_independent: bool | None = True,
    has_snapshot_close: bool | None = True,
    decimal_odds: float | None = 2.0,
    closing_fair: float | None = 0.52,
    model_prob: float | None = 0.46,
    mint_fb: bool | None = None,
    close_fb: bool | None = None,
) -> tuple[Any, ...]:
    # (sport, market, settled_at, clv_log, closing_anchor, close_independent,
    #  has_snapshot_close, decimal_odds, closing_fair_probability,
    #  model_probability, mint_devig_fell_back, close_devig_fell_back)
    return (
        sport,
        market,
        settled_at,
        clv_log,
        closing_anchor,
        close_independent,
        has_snapshot_close,
        decimal_odds,
        closing_fair,
        model_prob,
        mint_fb,
        close_fb,
    )


def test_point_estimates_nulled_below_ok_n() -> None:
    cells = promotion_distance_cells([_row() for _ in range(SPORT_MARKET_OK_N - 1)], now=NOW)
    (cell,) = cells
    assert cell["n_trusted"] == SPORT_MARKET_OK_N - 1
    assert cell["status"] == "accruing"
    # sub-floor point estimates are NULLED at the source — no consumer can
    # read a noise-level CLV for an accruing cell.
    assert cell["mean_clv_log"] is None
    assert cell["se_clv_log"] is None


def test_point_estimates_reported_at_ok_n() -> None:
    cells = promotion_distance_cells([_row() for _ in range(SPORT_MARKET_OK_N)], now=NOW)
    (cell,) = cells
    assert cell["status"] == "ok"
    assert cell["ok_n"] == SPORT_MARKET_OK_N
    assert cell["mean_clv_log"] == pytest.approx(0.03)
    assert cell["se_clv_log"] is not None
    assert cell["est_days_to_threshold"] is None  # threshold met -> no estimate


def test_untrusted_rows_count_settled_but_never_trusted() -> None:
    rows = [
        _row(closing_anchor="consensus"),  # not a sharp close anchor
        _row(close_independent=False),  # circular self-priced close
        _row(close_independent=None),  # unproven independence is not trusted
        _row(clv_log=None),  # no measured CLV
        _row(has_snapshot_close=False),  # poll-time fallback close
        _row(model_prob=0.52),  # tautological (close fair == pick fair)
        _row(clv_log=1.7, closing_fair=0.9, decimal_odds=6.5),  # fabricated
        _row(mint_fb=True, close_fb=False),  # asymmetric devig fallback
    ]
    (cell,) = promotion_distance_cells(rows, now=NOW)
    assert cell["n_settled"] == len(rows)
    assert cell["n_trusted"] == 0
    # no trusted cadence -> None (dashboard renders "—"), never a guess
    assert cell["est_days_to_threshold"] is None


def test_days_to_threshold_is_linear_from_recent_cadence() -> None:
    # 3 old + 7 recent trusted closes: cadence = 7 per window; the remaining
    # (ok_n - 10) closes extrapolate linearly at that cadence.
    old = [_row(settled_at=NOW - timedelta(days=30)) for _ in range(3)]
    recent = [_row(settled_at=NOW - timedelta(days=2)) for _ in range(7)]
    (cell,) = promotion_distance_cells(old + recent, now=NOW)
    assert cell["n_trusted"] == 10
    assert cell["n_recent_trusted"] == 7
    expected = (SPORT_MARKET_OK_N - 10) * PROMOTION_CADENCE_WINDOW_DAYS / 7
    assert cell["est_days_to_threshold"] == pytest.approx(expected)


def test_cells_grouped_per_sport_market_and_sorted_by_progress() -> None:
    rows = (
        [_row(market="h2h") for _ in range(5)]
        + [_row(market="totals") for _ in range(2)]
        + [_row(sport="basketball", market="h2h")]
    )
    cells = promotion_distance_cells(rows, now=NOW)
    keys = [(c["sport"], c["market"]) for c in cells]
    assert keys[0] == ("soccer", "h2h")  # most accrued evidence first
    assert set(keys) == {("soccer", "h2h"), ("soccer", "totals"), ("basketball", "h2h")}


def test_empty_rows_yield_no_cells() -> None:
    assert promotion_distance_cells([], now=NOW) == []


def test_weekly_steam_counts_bucket_by_iso_week_and_lose_nothing() -> None:
    mon = datetime(2026, 6, 29, tzinfo=UTC)  # a Monday
    rows: list[tuple[bool | None, datetime | None]] = [
        (True, mon),
        (False, mon + timedelta(days=2)),
        (None, mon + timedelta(days=6)),  # NULL verdict counts as unevaluated
        (True, mon + timedelta(days=7)),  # next ISO week
        (None, None),  # no created_at -> skipped (cannot be bucketed)
    ]
    weekly = _weekly_steam_counts(rows)
    assert weekly == [
        {"week_start": "2026-06-29", "would_demote": 1, "clear": 1, "unevaluated": 1},
        {"week_start": "2026-07-06", "would_demote": 1, "clear": 0, "unevaluated": 0},
    ]
