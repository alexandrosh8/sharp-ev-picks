"""Legacy EH/AH product-mismatch cohort exclusion from trusted CLV (2026-08-02).

Commit b061309 (deployed 2026-08-02 ~21:30 UTC) fail-closed OddsChecker's
3-way European handicap out of the integer ``spreads_*`` soccer vocabulary.
Picks MINTED BEFORE that deploy on (soccer, spreads, INTEGER-line or NULL
market_detail) may carry EH fills; when they reach kickoff,
``finalize_closing_from_snapshots`` stamps a close from the now-AH-only group
— a PRODUCT-MISMATCHED close (AH close vs EH fill, ~15pp structural implied
gap) that must never enter the trusted-CLV subset.

The exclusion lives in the READ-time trusted gate (mechanism (a)): the close
writer OVERWRITES ``close_exclusion_reason`` at close-stamp time
(app/clv_trueup.py finalize path), so a pre-stamped reason would be clobbered
when the cohort's open picks finalize — only a deterministic read-time rule
survives. The derived label ``legacy_product_mismatch`` is surfaced through
the same ``close_exclusion_reasons`` provenance counts the dashboard renders.

Pure tests: the predicate, the aggregate gate, the standalone trust predicate,
the live-evidence row property, and the promotion-distance cells.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.backtesting.live_evidence import SettledPickRow
from app.edge.value import (
    CLOSE_EXCLUSION_REASONS,
    CLOSE_REASON_LEGACY_PRODUCT_MISMATCH,
    LEGACY_EH_SPREADS_FIX_DEPLOYED_AT,
    is_legacy_product_mismatch,
)
from app.storage.repositories import (
    MIN_HEADLINE_N,
    _aggregate_settled,
    _settled_close_is_trusted,
    promotion_distance_cells,
)

PRE_FIX = LEGACY_EH_SPREADS_FIX_DEPLOYED_AT - timedelta(hours=1)
POST_FIX = LEGACY_EH_SPREADS_FIX_DEPLOYED_AT + timedelta(hours=1)


# ---------------------------------------------------------------- predicate


def test_cutoff_is_the_b061309_deploy_time() -> None:
    # The fix COMMIT is 2026-08-02T21:19:07Z but the running pipeline minted
    # EH-contaminated picks until the DEPLOY (~21:30 UTC) — the cutoff must be
    # the deploy time, and must be UTC-aware.
    assert datetime(2026, 8, 2, 21, 30, tzinfo=UTC) == LEGACY_EH_SPREADS_FIX_DEPLOYED_AT


def test_integer_and_null_detail_pre_fix_soccer_spreads_are_cohort() -> None:
    for detail in ("spreads_minus_1", "spreads_minus_2", "spreads_plus_3", "spreads_plus_0", None):
        assert is_legacy_product_mismatch(
            sport="soccer", market="spreads", market_detail=detail, minted_at=PRE_FIX
        ), detail


def test_fractional_details_are_not_cohort() -> None:
    # Half/quarter lines were byte-identical through the b061309 fix — the AH
    # product is unambiguous there, so their closes are NOT product-mismatched.
    for detail in ("spreads_minus_0_5", "spreads_minus_0_25", "spreads_minus_1_75"):
        assert not is_legacy_product_mismatch(
            sport="soccer", market="spreads", market_detail=detail, minted_at=PRE_FIX
        ), detail


def test_sets_namespace_other_sports_markets_and_post_fix_are_not_cohort() -> None:
    assert not is_legacy_product_mismatch(
        sport="soccer", market="spreads", market_detail="spreads_sets_2", minted_at=PRE_FIX
    )
    assert not is_legacy_product_mismatch(
        sport="tennis", market="spreads", market_detail="spreads_minus_1", minted_at=PRE_FIX
    )
    assert not is_legacy_product_mismatch(
        sport="soccer", market="totals", market_detail=None, minted_at=PRE_FIX
    )
    assert not is_legacy_product_mismatch(
        sport="soccer", market="spreads", market_detail="spreads_minus_1", minted_at=POST_FIX
    )


def test_unassignable_rows_are_not_cohort() -> None:
    # Pure-test constructions may lack the dimensions; an unprovable cohort
    # membership never excludes (mirrors the mint-echo convention).
    assert not is_legacy_product_mismatch(
        sport=None, market="spreads", market_detail=None, minted_at=PRE_FIX
    )
    assert not is_legacy_product_mismatch(
        sport="soccer", market=None, market_detail=None, minted_at=PRE_FIX
    )
    assert not is_legacy_product_mismatch(
        sport="soccer", market="spreads", market_detail=None, minted_at=None
    )


def test_label_is_in_the_closed_reason_vocabulary() -> None:
    assert CLOSE_REASON_LEGACY_PRODUCT_MISMATCH == "legacy_product_mismatch"
    assert CLOSE_REASON_LEGACY_PRODUCT_MISMATCH in CLOSE_EXCLUSION_REASONS
    assert len(CLOSE_REASON_LEGACY_PRODUCT_MISMATCH) <= 32  # String(32) column


# ------------------------------------------------------- _aggregate_settled


def _row(
    *,
    legacy_product_mismatch: bool | None = False,
    close_reason: str | None = None,
) -> tuple[object, ...]:
    """A fully-trusted settled row (same shape performance_report builds),
    with the trailing legacy-cohort verdict slot."""
    return (
        "won",
        Decimal("1.0"),
        Decimal("10.0"),
        Decimal("0.02"),
        True,
        Decimal("2.0"),  # closing_odds (soft display price)
        "pinnacle",
        True,  # close_independent
        True,  # has_snapshot_close
        Decimal("2.0"),  # decimal_odds
        None,  # closing_fair_probability
        None,  # model_probability
        None,  # mint_devig_fell_back
        None,  # close_devig_fell_back
        None,  # close_snapshot_captured_at
        None,  # kickoff
        close_reason,
        None,  # fill bookmaker
        legacy_product_mismatch,
    )


def test_cohort_rows_never_enter_trusted_subset() -> None:
    clean = [_row() for _ in range(MIN_HEADLINE_N)]
    cohort = [_row(legacy_product_mismatch=True) for _ in range(MIN_HEADLINE_N)]
    assert _aggregate_settled(clean)["n_sharp_close"] == MIN_HEADLINE_N
    assert _aggregate_settled(cohort)["n_sharp_close"] == 0
    mixed = _aggregate_settled(clean + cohort)
    assert mixed["n_sharp_close"] == MIN_HEADLINE_N


def test_short_tuple_rows_without_verdict_slot_stay_trusted() -> None:
    # Feature-detected trailing slot: 18-tuple callers (pre-change shape)
    # behave exactly as before.
    rows = [_row()[:18] for _ in range(MIN_HEADLINE_N)]
    assert _aggregate_settled(rows)["n_sharp_close"] == MIN_HEADLINE_N


def test_cohort_reason_overrides_persisted_reason_in_provenance_counts() -> None:
    # The close writer stamped 'trusted' before/when the mismatched close was
    # finalized; the read-time gate relabels the cohort so the dashboard
    # provenance shows WHY the row is out — and the split stays queryable.
    rows = [_row(close_reason="trusted") for _ in range(3)] + [
        _row(close_reason="trusted", legacy_product_mismatch=True) for _ in range(2)
    ]
    reasons = _aggregate_settled(rows)["clv_quality"]["close_exclusion_reasons"]
    assert reasons == {"trusted": 3, CLOSE_REASON_LEGACY_PRODUCT_MISMATCH: 2}


def test_cohort_rows_without_persisted_reason_still_surface_the_label() -> None:
    # An open-cohort pick settling on a pre-reason-column path has reason NULL;
    # the derived label still surfaces so the exclusion is never invisible.
    rows = [_row(close_reason=None, legacy_product_mismatch=True)]
    reasons = _aggregate_settled(rows)["clv_quality"]["close_exclusion_reasons"]
    assert reasons == {CLOSE_REASON_LEGACY_PRODUCT_MISMATCH: 1}


def test_blended_headline_unchanged_by_cohort_flag() -> None:
    # Scope: the TRUSTED subset only. The blended headline is labelled
    # indicative (blended_clv_evidential=False) and keeps mixing every close.
    rows = [_row(legacy_product_mismatch=True) for _ in range(MIN_HEADLINE_N)]
    agg = _aggregate_settled(rows)
    assert agg["stake_weighted_clv_log"] is not None


# ------------------------------------------- standalone headline predicate


def _trusted_kwargs() -> dict[str, Any]:
    return {
        "clv_log": Decimal("0.02"),
        "closing_anchor": "pinnacle",
        "close_independent": True,
        "has_snapshot_close": True,
        "decimal_odds": Decimal("2.0"),
        "closing_fair_probability": None,
        "model_probability": None,
        "mint_devig_fell_back": None,
        "close_devig_fell_back": None,
    }


def test_settled_close_is_trusted_rejects_cohort() -> None:
    assert _settled_close_is_trusted(**_trusted_kwargs()) is True
    assert _settled_close_is_trusted(**_trusted_kwargs(), legacy_product_mismatch=True) is False


# ------------------------------------------------------ live-evidence rows


def _settled_pick_row(**overrides: object) -> SettledPickRow:
    base: dict[str, object] = {
        "tier": "premium",
        "value_filter_score": None,
        "clv_log": 0.02,
        "beat_close": True,
        "stake": 10.0,
        "pnl": 1.0,
        "sport": "soccer",
        "market": "spreads",
        "closing_anchor_type": "pinnacle",
        "has_snapshot_close": True,
        "close_independent_of_fill": True,
        "market_detail": "spreads_minus_1",
        "minted_at": PRE_FIX,
    }
    base.update(overrides)
    return SettledPickRow(**base)  # type: ignore[arg-type]


def test_settled_pick_row_cohort_is_not_sharp_close() -> None:
    assert _settled_pick_row().sharp_close is False
    assert _settled_pick_row(minted_at=POST_FIX).sharp_close is True
    assert _settled_pick_row(market_detail="spreads_minus_0_25").sharp_close is True
    assert _settled_pick_row(market_detail=None).sharp_close is False


# ----------------------------------------------- promotion-distance cells


def _trust_row(
    *,
    market_detail: str | None,
    minted_at: datetime | None,
) -> tuple[object, ...]:
    return (
        "soccer",
        "spreads",
        datetime(2026, 8, 1, tzinfo=UTC),  # settled_at
        0.02,  # clv_log
        "pinnacle",
        True,  # close_independent
        True,  # has_snapshot_close
        2.0,  # decimal_odds
        None,  # closing_fair_probability
        None,  # model_probability
        None,  # mint_devig_fell_back
        None,  # close_devig_fell_back
        None,  # bookmaker
        market_detail,
        minted_at,
    )


def test_promotion_cells_exclude_cohort_from_trusted_count() -> None:
    now = datetime(2026, 8, 2, 22, 0, tzinfo=UTC)
    cohort = [_trust_row(market_detail="spreads_minus_1", minted_at=PRE_FIX) for _ in range(35)]
    cells = promotion_distance_cells(cohort, now=now)
    assert len(cells) == 1
    assert cells[0]["n_settled"] == 35
    assert cells[0]["n_trusted"] == 0
    post = [_trust_row(market_detail="spreads_minus_1", minted_at=POST_FIX) for _ in range(35)]
    assert promotion_distance_cells(post, now=now)[0]["n_trusted"] == 35


def test_promotion_cells_without_trailing_slots_unchanged() -> None:
    # 13-tuple callers (pre-change shape) keep today's behavior — the cohort
    # is unprovable without the detail/mint dimensions.
    now = datetime(2026, 8, 2, 22, 0, tzinfo=UTC)
    rows = [_trust_row(market_detail=None, minted_at=None)[:13] for _ in range(35)]
    assert promotion_distance_cells(rows, now=now)[0]["n_trusted"] == 35
