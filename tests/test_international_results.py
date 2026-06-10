"""International results parsing: completed vs fixtures, neutral flag, adapt."""

from datetime import date

from app.ingestion.international_results import (
    parse_fixtures,
    parse_results,
    to_match_rows,
)

CSV = """date,home_team,away_team,home_score,away_score,tournament,city,country,neutral
2024-09-05,Brazil,Argentina,1,0,FIFA World Cup qualification,Rio,Brazil,FALSE
2024-09-08,Spain,Portugal,2,2,UEFA Nations League,Madrid,Spain,FALSE
2026-06-11,Mexico,South Africa,NA,NA,FIFA World Cup,Mexico City,Mexico,FALSE
2026-06-12,South Korea,Czech Republic,NA,NA,FIFA World Cup,Toronto,Canada,TRUE
2026-06-12,United States,Paraguay,NA,NA,Friendly,New York,United States,FALSE
"""


def test_parse_results_only_completed() -> None:
    matches = parse_results(CSV)
    assert len(matches) == 2
    assert matches[0].home_team == "Brazil"
    assert matches[0].home_goals == 1
    assert matches[0].neutral is False
    assert matches[1].tournament == "UEFA Nations League"


def test_parse_fixtures_filters_tournament_and_date() -> None:
    fx = parse_fixtures(CSV, tournament="FIFA World Cup", on_or_after=date(2026, 6, 10))
    assert len(fx) == 2  # the two WC fixtures; the Friendly is excluded
    homes = {f.home_team for f in fx}
    assert homes == {"Mexico", "South Korea"}
    sk = next(f for f in fx if f.home_team == "South Korea")
    assert sk.neutral is True  # neutral venue (Toronto)
    mx = next(f for f in fx if f.home_team == "Mexico")
    assert mx.neutral is False  # host nation at home


def test_to_match_rows_carries_neutral_and_results() -> None:
    rows, neutral = to_match_rows(parse_results(CSV))
    assert len(rows) == len(neutral) == 2
    assert rows[0].result == "H"  # Brazil 1-0
    assert rows[1].result == "D"  # Spain 2-2
    assert all(o is None for o in (rows[0].b365_home, rows[0].pinnacle_closing_home))
    assert neutral == [False, False]
