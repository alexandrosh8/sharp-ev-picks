"""CLOSE-EVIDENCE package (D2/D3/D4): echo gate, close provenance, clv_quality.

D2 — `_drop_stale_sharp_echoes` is a TIGHTENING-ONLY per-source gate inside
finalize_closing_from_snapshots: a sharp-archive source whose own last capture
is BOTH >max_gap stale at kickoff AND older than the pick's creation while the
MINT anchor was the same source is provably the mint-anchor row re-read at
settlement (an ECHO), so its rows are dropped and the close falls to the fresh
soft consensus. The gate may only DROP rows — never admit one.

D3 — both close writers stamp `close_anchor_book` + `close_snapshot_captured_at`
(the finalize writer is covered here via monkeypatched resolvers; the
revalidation writer in tests/test_clv_trueup.py).

D4 — `_aggregate_settled` emits the previously-discarded guard tallies under
"clv_quality" (diagnostics only; no estimate changes).

Pure/monkeypatched — no DB, no network (the DB reads finalize performs are
stubbed at app.clv_trueup's own module globals).
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.clv_trueup as clv_trueup
from app.clv_trueup import (
    SNAPSHOT_CLOSE_MAX_GAP,
    _anchor_capture_time,
    _drop_stale_sharp_echoes,
    finalize_closing_from_snapshots,
)
from app.edge.value import CONSENSUS_ANCHOR
from app.probabilities.devig import DevigMethod
from app.schemas.base import Market
from app.schemas.odds import OddsSnapshotIn
from app.storage.models import Pick
from app.storage.repositories import _aggregate_settled

KICKOFF = datetime(2026, 7, 1, 18, 0, tzinfo=UTC)
CREATED_AT = KICKOFF - timedelta(days=3)
SELECTIONS = ("Home FC", "Draw", "Away FC")
REF = "https://www.oddsportal.com/football/test/close-evidence"


def _snaps(
    bookmaker: str,
    odds: tuple[float, float, float],
    captured_at: datetime,
) -> list[OddsSnapshotIn]:
    return [
        OddsSnapshotIn(
            event_id=REF,
            bookmaker=bookmaker,
            market=Market.H2H,
            selection=sel,
            decimal_odds=o,
            captured_at=captured_at,
            ingested_at=captured_at,
        )
        for sel, o in zip(SELECTIONS, odds, strict=True)
    ]


def _pick(
    anchor_type: str | None = "sharp",
    anchor_book: str | None = "Betfair Exchange",
    created_at: datetime | None = CREATED_AT,
) -> Pick:
    pick = Pick(
        market="h2h",
        selection="Home FC",
        bookmaker="SoftBook",
        decimal_odds=Decimal("2.50"),
        model_probability=Decimal("0.45"),
    )
    pick.id = 1  # transient — never flushed; the writers log pick.id
    pick.anchor_type = anchor_type
    pick.anchor_book = anchor_book
    pick.created_at = created_at  # type: ignore[assignment]
    return pick


# Stale = last capture > max_gap (4h) before kickoff; echo = predates creation.
STALE_ECHO_AT = CREATED_AT - timedelta(hours=1)
FRESH_AT = KICKOFF - timedelta(minutes=30)
BETFAIR_ODDS = (2.20, 3.40, 3.30)


# --------------------------------------------------------------------------- #
# D2 pure gate: _drop_stale_sharp_echoes
# --------------------------------------------------------------------------- #
def test_stale_same_source_echo_dropped() -> None:
    rows = _snaps("Betfair Exchange", BETFAIR_ODDS, STALE_ECHO_AT)
    kept = _drop_stale_sharp_echoes(rows, _pick(), KICKOFF, SNAPSHOT_CLOSE_MAX_GAP)
    assert kept == []


def test_fresh_same_source_row_kept() -> None:
    rows = _snaps("Betfair Exchange", BETFAIR_ODDS, FRESH_AT)
    kept = _drop_stale_sharp_echoes(rows, _pick(), KICKOFF, SNAPSHOT_CLOSE_MAX_GAP)
    assert kept == rows


def test_stale_cross_source_rows_untouched() -> None:
    # Mint anchor was Pinnacle; a stale Betfair close is CROSS-source — genuine
    # independent evidence (audit: cross-source cells ~3% tautological), kept.
    rows = _snaps("Betfair Exchange", BETFAIR_ODDS, STALE_ECHO_AT)
    pick = _pick(anchor_type="pinnacle", anchor_book="Pinnacle")
    assert _drop_stale_sharp_echoes(rows, pick, KICKOFF, SNAPSHOT_CLOSE_MAX_GAP) == rows


def test_stale_but_post_creation_row_kept() -> None:
    # Same source and stale at kickoff, but captured AFTER the pick was minted —
    # not provably the mint row's echo, so it is kept (unprovable is not
    # droppable).
    captured = CREATED_AT + timedelta(hours=2)
    assert KICKOFF - captured > SNAPSHOT_CLOSE_MAX_GAP  # still stale
    rows = _snaps("Betfair Exchange", BETFAIR_ODDS, captured)
    assert _drop_stale_sharp_echoes(rows, _pick(), KICKOFF, SNAPSHOT_CLOSE_MAX_GAP) == rows


def test_consensus_minted_pick_never_triggers_the_gate() -> None:
    rows = _snaps("Betfair Exchange", BETFAIR_ODDS, STALE_ECHO_AT)
    pick = _pick(anchor_type="consensus", anchor_book=None)
    assert _drop_stale_sharp_echoes(rows, pick, KICKOFF, SNAPSHOT_CLOSE_MAX_GAP) == rows


def test_same_type_different_source_kept() -> None:
    # anchor_book precision: Smarkets is also type 'sharp' but a DIFFERENT
    # source than the Betfair mint anchor — its stale rows are not echoes.
    rows = _snaps("Smarkets", BETFAIR_ODDS, STALE_ECHO_AT)
    pick = _pick(anchor_type="sharp", anchor_book="Betfair Exchange")
    assert _drop_stale_sharp_echoes(rows, pick, KICKOFF, SNAPSHOT_CLOSE_MAX_GAP) == rows


def test_gate_output_is_always_a_subset_of_its_input() -> None:
    # TIGHTENING-ONLY invariant: across every scenario the gate returns only
    # rows it was given (identity subset) — it can never ADMIT a row the
    # ungated path rejected, only drop.
    scenarios: list[tuple[list[OddsSnapshotIn], Pick]] = [
        (_snaps("Betfair Exchange", BETFAIR_ODDS, STALE_ECHO_AT), _pick()),
        (_snaps("Betfair Exchange", BETFAIR_ODDS, FRESH_AT), _pick()),
        (
            _snaps("Pinnacle", BETFAIR_ODDS, STALE_ECHO_AT)
            + _snaps("Betfair Exchange", BETFAIR_ODDS, FRESH_AT),
            _pick(anchor_type="pinnacle", anchor_book="Pinnacle"),
        ),
        (_snaps("Smarkets", BETFAIR_ODDS, STALE_ECHO_AT), _pick(anchor_type=None)),
        ([], _pick()),
    ]
    for rows, pick in scenarios:
        kept = _drop_stale_sharp_echoes(rows, pick, KICKOFF, SNAPSHOT_CLOSE_MAX_GAP)
        assert len(kept) <= len(rows)
        assert all(any(k is r for r in rows) for k in kept)


def test_mixed_sources_only_the_echo_source_dropped() -> None:
    stale_echo = _snaps("Pinnacle", BETFAIR_ODDS, STALE_ECHO_AT)
    fresh_other = _snaps("Betfair Exchange", (2.25, 3.35, 3.25), FRESH_AT)
    pick = _pick(anchor_type="pinnacle", anchor_book="Pinnacle")
    kept = _drop_stale_sharp_echoes(stale_echo + fresh_other, pick, KICKOFF, SNAPSHOT_CLOSE_MAX_GAP)
    assert kept == fresh_other


# --------------------------------------------------------------------------- #
# D2/D3 finalize-level: echo -> honest consensus close; provenance stamped.
# The DB reads finalize performs are stubbed at app.clv_trueup module globals.
# --------------------------------------------------------------------------- #
SOFT_BOOKS = {
    "bet365": (2.30, 3.35, 3.20),
    "unibet": (2.35, 3.30, 3.25),
    "betsson": (2.28, 3.40, 3.22),
}


def _soft_close() -> list[OddsSnapshotIn]:
    rows: list[OddsSnapshotIn] = []
    for book, odds in SOFT_BOOKS.items():
        rows.extend(_snaps(book, odds, FRESH_AT))
    return rows


def _stub_reads(monkeypatch: pytest.MonkeyPatch, betfair_captured_at: datetime) -> None:
    async def fake_soft(session, event_id, external_ref, kickoff):  # type: ignore[no-untyped-def]
        return _soft_close(), FRESH_AT

    async def fake_betfair(session, external_ref, kickoff):  # type: ignore[no-untyped-def]
        return _snaps("Betfair Exchange", BETFAIR_ODDS, betfair_captured_at)

    monkeypatch.setattr(clv_trueup, "closing_odds_from_snapshots", fake_soft)
    monkeypatch.setattr(clv_trueup, "resolve_betfair_back_snaps", fake_betfair)


async def _finalize(pick: Pick, **kwargs) -> bool:  # type: ignore[no-untyped-def]
    pick.event_id = 1
    return await finalize_closing_from_snapshots(
        cast(AsyncSession, None),  # every session read is monkeypatched away
        pick,
        REF,
        KICKOFF,
        DevigMethod.SHIN,
        use_betfair_exchange=True,
        **kwargs,
    )


async def test_echo_gate_on_falls_to_honest_consensus_close(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _stub_reads(monkeypatch, STALE_ECHO_AT)
    pick = _pick()  # minted on the Betfair anchor — the stale rows are its echo
    assert await _finalize(pick) is True
    # The fake sharp close is gone; the close is the fresh soft consensus,
    # labelled honestly — and D3 provenance stamps the consensus capture time.
    assert pick.closing_anchor_type == "consensus"
    assert pick.close_anchor_book == CONSENSUS_ANCHOR
    assert pick.close_snapshot_captured_at == FRESH_AT
    assert pick.has_snapshot_close is True


async def test_echo_gate_off_reproduces_prefix_sharp_echo(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Escape hatch (CLV_SHARP_CLOSE_ECHO_GATE=false): pre-fix behavior — the
    # stale mint echo outranks the fresh consensus and mints a 'sharp' close.
    # This is exactly the pollution D2 removes; the assertion documents it.
    _stub_reads(monkeypatch, STALE_ECHO_AT)
    pick = _pick()
    assert await _finalize(pick, sharp_close_echo_gate=False) is True
    assert pick.closing_anchor_type == "sharp"
    assert pick.close_anchor_book == "Betfair Exchange"
    assert pick.close_snapshot_captured_at == STALE_ECHO_AT
    # D3 is precisely what makes this echo measurable: capture predates mint.
    assert pick.close_snapshot_captured_at <= pick.created_at


async def test_fresh_sharp_close_kept_with_gate_on(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _stub_reads(monkeypatch, FRESH_AT)
    pick = _pick()
    assert await _finalize(pick) is True
    assert pick.closing_anchor_type == "sharp"
    assert pick.close_anchor_book == "Betfair Exchange"
    assert pick.close_snapshot_captured_at == FRESH_AT


async def test_cross_source_stale_close_untouched_by_gate(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Consensus-minted pick + stale Betfair archive rows: cross-source close
    # evidence is admitted exactly as before (the gate targets only the
    # provable same-source echo).
    _stub_reads(monkeypatch, STALE_ECHO_AT)
    pick = _pick(anchor_type="consensus", anchor_book=None)
    assert await _finalize(pick) is True
    assert pick.closing_anchor_type == "sharp"
    assert pick.close_anchor_book == "Betfair Exchange"


# --------------------------------------------------------------------------- #
# D3 helper: _anchor_capture_time
# --------------------------------------------------------------------------- #
def test_anchor_capture_time_prefers_the_anchor_books_own_rows() -> None:
    captured = {
        ("Home FC", "Pinnacle"): KICKOFF - timedelta(hours=5),
        ("Draw", "Pinnacle"): KICKOFF - timedelta(hours=4),
        ("Home FC", "bet365"): FRESH_AT,
    }
    assert _anchor_capture_time(captured, "Pinnacle") == KICKOFF - timedelta(hours=4)
    # Consensus sentinel names no book -> max over every row that fed the median.
    assert _anchor_capture_time(captured, CONSENSUS_ANCHOR) == FRESH_AT
    assert _anchor_capture_time({}, "Pinnacle") is None


# --------------------------------------------------------------------------- #
# D4: clv_quality tallies emitted by _aggregate_settled (pure)
# --------------------------------------------------------------------------- #
def _row(
    clv_log: float | None = 0.02,
    closing_fair_probability: float | None = 0.45,
    model_probability: float | None = 0.40,
    close_independent: bool | None = True,
    has_snapshot_close: bool | None = True,
    close_snapshot_captured_at: datetime | None = None,
    kickoff_at: datetime | None = None,
) -> tuple[object, ...]:
    # The 16-tuple performance_report._settled_tuple builds (trailing D3 fields).
    return (
        "won",
        Decimal("1.0"),
        Decimal("10.0"),
        Decimal(str(clv_log)) if clv_log is not None else None,
        True if clv_log is not None else None,
        None,
        "pinnacle",
        close_independent,
        has_snapshot_close,
        Decimal("2.0"),
        Decimal(str(closing_fair_probability)) if closing_fair_probability is not None else None,
        Decimal(str(model_probability)) if model_probability is not None else None,
        None,
        None,
        close_snapshot_captured_at,
        kickoff_at,
    )


def test_clv_quality_tallies_each_guard() -> None:
    rows = [
        _row(),  # clean CLV
        _row(clv_log=None),  # no CLV -> missing
        _row(closing_fair_probability=0.40, model_probability=0.40),  # tautological
        _row(closing_fair_probability=0.95),  # fabricated (close edge 0.45 > 0.2)
        _row(close_independent=False),  # circular (and not otherwise excluded)
    ]
    q = _aggregate_settled(rows)["clv_quality"]
    assert q["n_settled"] == 5
    assert q["clv_missing"] == 1
    assert q["clv_excluded_tautological"] == 1
    assert q["clv_excluded_fabricated"] == 1
    assert q["clv_excluded_circular"] == 1
    assert q["tautological_rate"] == pytest.approx(1 / 4)  # of the 4 rows WITH CLV
    assert q["n_snapshot_close"] == 5


def test_clv_quality_snapshot_vs_fallback_split() -> None:
    rows = [
        _row(),  # snapshot close
        _row(has_snapshot_close=None),  # CLV present, no snapshot -> fallback
        _row(clv_log=None, has_snapshot_close=None),  # no CLV at all -> neither
    ]
    q = _aggregate_settled(rows)["clv_quality"]
    assert q["n_snapshot_close"] == 1
    assert q["n_fallback_close"] == 1
    assert q["clv_missing"] == 1


def test_clv_quality_close_age_percentiles_and_staleness() -> None:
    ages_minutes = [10, 30, 60, 300]  # one > 240 (stale)
    rows = [
        _row(
            close_snapshot_captured_at=KICKOFF - timedelta(minutes=m),
            kickoff_at=KICKOFF,
        )
        for m in ages_minutes
    ]
    q = _aggregate_settled(rows)["clv_quality"]
    assert q["n_close_age_known"] == 4
    assert q["close_age_p50_minutes"] == pytest.approx(30.0)  # nearest-rank
    assert q["close_age_p90_minutes"] == pytest.approx(300.0)
    assert q["n_stale_close"] == 1
    assert q["stale_close_max_gap_minutes"] == 240


def test_clv_quality_null_safe_on_empty_and_pre_provenance_rows() -> None:
    # Empty sample and 14-tuple pre-D3 rows (no trailing provenance) must both
    # yield an honest zero/None payload — never a crash or a fabricated age.
    empty = _aggregate_settled([])["clv_quality"]
    assert empty["n_settled"] == 0
    assert empty["tautological_rate"] is None
    assert empty["close_age_p50_minutes"] is None
    legacy = _aggregate_settled([_row()[:14]])["clv_quality"]
    assert legacy["n_close_age_known"] == 0
    assert legacy["close_age_p50_minutes"] is None
    assert legacy["n_stale_close"] == 0
