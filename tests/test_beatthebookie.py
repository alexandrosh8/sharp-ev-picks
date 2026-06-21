"""BeatTheBookie (arXiv 1710.02824) read-only loader. Pure-parser tests — no network.

closing_odds.csv has a header; ~880k worldwide matches 2000-2015 with CONSENSUS
(avg) + best-price (max) 1X2 odds + results. NOT a sharp book: `max >= avg` by
construction, so it tests best-price-vs-consensus, not sharp-beating skill. Use
the bet-everything baseline as the null when reading any ROI.
"""

from datetime import date

from app.ingestion.beatthebookie import BttMatch, parse_btb_rows


def _row(**kw: str) -> dict[str, str]:
    base = {
        "match_id": "170088",
        "league": "England: Premier League",
        "match_date": "2005-01-01",
        "home_team": "Liverpool",
        "home_score": "0",
        "away_team": "Chelsea",
        "away_score": "1",
        "avg_odds_home_win": "2.9944",
        "avg_odds_draw": "3.1944",
        "avg_odds_away_win": "2.2256",
        "max_odds_home_win": "3.2000",
        "max_odds_draw": "3.2500",
        "max_odds_away_win": "2.2900",
    }
    base.update(kw)
    return base


def test_parse_btb_keeps_complete_rows() -> None:
    matches = parse_btb_rows([_row()])
    assert len(matches) == 1
    m = matches[0]
    assert isinstance(m, BttMatch)
    assert m.league == "England: Premier League"
    assert m.match_date == date(2005, 1, 1)
    assert m.home_score == 0 and m.away_score == 1
    assert m.avg_home == 2.9944 and m.avg_draw == 3.1944 and m.avg_away == 2.2256
    assert m.max_home == 3.2000 and m.max_draw == 3.2500 and m.max_away == 2.2900


def test_parse_btb_skips_missing_odds() -> None:
    assert parse_btb_rows([_row(avg_odds_home_win="", max_odds_home_win="None")]) == []


def test_parse_btb_skips_missing_score() -> None:
    assert parse_btb_rows([_row(home_score="", away_score="")]) == []
