"""Pure helpers of scripts/research/sport_quality_report.py — bucketing,
anchor-age computation from synthetic rows, insufficient-sample labelling,
consensus reconstruction. NO DB, no network. Places no bets."""

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "research"


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sqr: Any = _load(_SCRIPTS / "sport_quality_report.py", "sport_quality_report_t")

T0 = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Bucketing
# --------------------------------------------------------------------------- #
def test_anchor_age_buckets() -> None:
    assert sqr.bucket_anchor_age(0) == "0-15m"
    assert sqr.bucket_anchor_age(15 * 60 - 1) == "0-15m"
    assert sqr.bucket_anchor_age(15 * 60) == "15-60m"
    assert sqr.bucket_anchor_age(3600 - 1) == "15-60m"
    assert sqr.bucket_anchor_age(3600) == "1-4h"
    assert sqr.bucket_anchor_age(4 * 3600 - 1) == "1-4h"
    assert sqr.bucket_anchor_age(4 * 3600) == "4h+"
    with pytest.raises(ValueError):
        sqr.bucket_anchor_age(-1)


def test_mint_to_kickoff_buckets() -> None:
    assert sqr.bucket_mint_to_kickoff(0) == "0-2h"
    assert sqr.bucket_mint_to_kickoff(2 * 3600 - 1) == "0-2h"
    assert sqr.bucket_mint_to_kickoff(2 * 3600) == "2-12h"
    assert sqr.bucket_mint_to_kickoff(12 * 3600) == "12h+"
    # negative lead never silently folds into 0-2h — honest separate bucket
    assert sqr.bucket_mint_to_kickoff(-60) == "post-kickoff"


def test_soft_book_bucket() -> None:
    assert [sqr.soft_book_bucket(n) for n in (0, 1, 3, 4, 9, 10, 19, 20, 35)] == [
        "0",
        "1-3",
        "1-3",
        "4-9",
        "4-9",
        "10-19",
        "10-19",
        "20+",
        "20+",
    ]


# --------------------------------------------------------------------------- #
# Sample-size honesty + SE math
# --------------------------------------------------------------------------- #
def test_mean_se_ddof1_and_small_n() -> None:
    assert sqr.mean_se([]) == (None, None)
    m, se = sqr.mean_se([0.5])
    assert m == 0.5 and se is None  # n<2: no fake-zero SE
    m, se = sqr.mean_se([1.0, 3.0])
    assert m == pytest.approx(2.0)
    assert se == pytest.approx(1.0)  # ddof=1: std=sqrt(2), se=sqrt(2)/sqrt(2)


def test_group_stats_insufficient_label() -> None:
    small = sqr.group_stats([0.01] * 29)
    assert small["label"] == "insufficient (n<30)"
    ok = sqr.group_stats([0.01, 0.02] * 15)
    assert ok["label"] == "ok"
    assert ok["n"] == 30
    assert ok["ci95"] is not None
    empty = sqr.group_stats([])
    assert empty["n"] == 0 and empty["mean"] is None and empty["ci95"] is None


def test_consensus_median_requires_min_books() -> None:
    assert sqr.consensus_median([0.5, 0.51]) is None  # < MIN_CONSENSUS_BOOKS
    assert sqr.consensus_median([0.5, 0.51, 0.52]) == pytest.approx(0.51)


# --------------------------------------------------------------------------- #
# Anchor-age computation from synthetic snapshot rows
# --------------------------------------------------------------------------- #
def test_latest_capture_prefers_market_and_respects_mint_time() -> None:
    snaps = [
        ("1x2", T0 - timedelta(minutes=90)),
        ("1x2", T0 - timedelta(minutes=10)),  # latest eligible in-market
        ("1x2", T0 + timedelta(minutes=5)),  # AFTER mint: never eligible
        ("btts", T0 - timedelta(minutes=1)),  # fresher but wrong market
    ]
    captured, fallback = sqr.latest_capture_at_or_before(snaps, T0, {"1x2"})
    assert captured == T0 - timedelta(minutes=10)
    assert fallback is False
    age = (T0 - captured).total_seconds()
    assert sqr.bucket_anchor_age(age) == "0-15m"


def test_latest_capture_market_fallback_flagged() -> None:
    snaps = [("btts", T0 - timedelta(hours=5))]
    captured, fallback = sqr.latest_capture_at_or_before(snaps, T0, {"1x2"})
    assert captured == T0 - timedelta(hours=5)
    assert fallback is True
    assert sqr.bucket_anchor_age((T0 - captured).total_seconds()) == "4h+"


def test_latest_capture_none_when_all_post_mint() -> None:
    captured, fallback = sqr.latest_capture_at_or_before([("1x2", T0 + timedelta(1))], T0, {"1x2"})
    assert captured is None and fallback is False


# --------------------------------------------------------------------------- #
# H6 replay consensus reconstruction (synthetic snapshots, real devig)
# --------------------------------------------------------------------------- #
def _snap(book: str, sel: str, odds: float, minutes_before: int) -> tuple:
    return (book, "1x2", sel, odds, T0 - timedelta(minutes=minutes_before))


def test_consensus_prob_at_mint_median_of_complete_books() -> None:
    snaps = []
    for book, (h, d, a) in {
        "bookA": (2.00, 3.40, 4.00),
        "bookB": (2.10, 3.30, 3.80),
        "bookC": (1.95, 3.50, 4.20),
    }.items():
        snaps += [_snap(book, "Home", h, 5), _snap(book, "Draw", d, 5), _snap(book, "Away", a, 5)]
    median, n_books = sqr.consensus_prob_at_mint(
        snaps, created_at=T0, selection="Home", allowed_markets={"1x2"}
    )
    assert n_books == 3
    assert median is not None and 0.4 < median < 0.6


def test_consensus_prob_at_mint_fail_closed() -> None:
    # only 2 books -> below MIN_CONSENSUS_BOOKS -> reference missing (None)
    snaps = [
        _snap("bookA", "Home", 2.0, 5),
        _snap("bookA", "Draw", 3.4, 5),
        _snap("bookA", "Away", 4.0, 5),
        _snap("bookB", "Home", 2.1, 5),
        _snap("bookB", "Draw", 3.3, 5),
        _snap("bookB", "Away", 3.8, 5),
    ]
    median, n_books = sqr.consensus_prob_at_mint(
        snaps, created_at=T0, selection="Home", allowed_markets={"1x2"}
    )
    assert median is None and n_books == 2
    # incomplete outcome set (no Away) contributes NOTHING — never a partial devig
    snaps_incomplete = [_snap("bookC", "Home", 2.0, 5), _snap("bookC", "Draw", 3.4, 5)]
    median, n_books = sqr.consensus_prob_at_mint(
        snaps_incomplete, created_at=T0, selection="Home", allowed_markets={"1x2"}
    )
    assert median is None and n_books == 0
    # outside the 30-minute window -> stale, excluded
    stale = [
        _snap("bookA", "Home", 2.0, 45),
        _snap("bookA", "Draw", 3.4, 45),
        _snap("bookA", "Away", 4.0, 45),
    ]
    median, n_books = sqr.consensus_prob_at_mint(
        stale, created_at=T0, selection="Home", allowed_markets={"1x2"}
    )
    assert median is None and n_books == 0


# --------------------------------------------------------------------------- #
# Pinnacle match-ceiling decomposition (pure classification, no DB)
# --------------------------------------------------------------------------- #
def test_normalize_league_exact_only() -> None:
    assert sqr.normalize_league("Premier  League!", "England") == ("england", "premier league")
    assert sqr.normalize_league("Serie-A") == ("", "serie a")


def test_classify_unmatched_co_occurrence_wins() -> None:
    co_map = {10: {100}, 11: {101}}
    kw = {
        "co_map": co_map,
        "pinnacle_inwindow_ids": {100},
        "pinnacle_inwindow_names": set(),
    }
    # mapped league with an in-window pinnacle event -> matcher missed it
    assert sqr.classify_unmatched_event(10, "EPL", "England", **kw) == "addressable"
    # mapped league, zero in-window pinnacle events -> structural
    assert sqr.classify_unmatched_event(11, "EPL", "England", **kw) == "structural"
    # co-occurrence beats a name match (name says addressable, map says structural)
    kw2 = dict(kw, pinnacle_inwindow_names={("england", "epl")})
    assert sqr.classify_unmatched_event(11, "EPL", "England", **kw2) == "structural"


def test_classify_unmatched_name_match_is_exact_and_country_aware() -> None:
    kw = {
        "co_map": {},
        "pinnacle_inwindow_ids": {100},
        "pinnacle_inwindow_names": {("england", "premier league"), ("", "serie a")},
    }
    # exact normalized name + country -> addressable
    assert sqr.classify_unmatched_event(1, "Premier - League", "ENGLAND", **kw) == "addressable"
    # same name, DIFFERENT non-empty country -> never matched (Ethiopia bug guard)
    assert sqr.classify_unmatched_event(2, "Premier League", "Ethiopia", **kw) == "unknown"
    # empty country on either side -> name-only match allowed
    assert sqr.classify_unmatched_event(3, "Serie A", "Italy", **kw) == "addressable"
    assert sqr.classify_unmatched_event(4, "Premier League", "", **kw) == "addressable"
    # substrings are NOT matches — exact equality only
    assert sqr.classify_unmatched_event(5, "Premier", "England", **kw) == "unknown"
    # no evidence at all -> unknown, never guessed structural
    assert sqr.classify_unmatched_event(6, "Obscure Cup", "Nowhere", **kw) == "unknown"


def test_corrected_match_rates_bounds() -> None:
    # 100 events, 30 matched, 40 structural, 20 unknown
    lower, upper = sqr.corrected_match_rates(100, 30, 40, 20)
    assert lower == pytest.approx(30 / 60)  # excludes only structural
    assert upper == pytest.approx(30 / 40)  # also excludes unknown
    # degenerate denominators -> None, never a division blow-up
    assert sqr.corrected_match_rates(5, 5, 5, 0) == (None, None)
    lower, upper = sqr.corrected_match_rates(5, 3, 1, 4)
    assert lower == pytest.approx(3 / 4) and upper is None


def test_ceiling_block_counts_and_rates() -> None:
    from collections import Counter

    block = sqr._ceiling_block(100, 30, Counter(structural=40, addressable=10, unknown=20))
    assert block["unmatched"] == 70
    assert block["structural"] == 40
    assert block["addressable"] == 10
    assert block["unknown_league"] == 20
    assert block["corrected_match_rate_lower"] == pytest.approx(0.5)
    assert block["corrected_match_rate_upper"] == pytest.approx(0.75)
    empty = sqr._ceiling_block(0, 0, Counter())
    assert empty["unmatched"] == 0 and empty["corrected_match_rate_lower"] is None


def test_expected_outcomes_and_soft_book_classifier() -> None:
    assert sqr.expected_outcomes("1x2") == 3
    assert sqr.expected_outcomes("double_chance") == 3
    assert sqr.expected_outcomes("over_under_2_5") == 2
    assert sqr.is_soft_book("bet365")
    assert not sqr.is_soft_book("Pinnacle")
    assert not sqr.is_soft_book(" Betfair Exchange ")
