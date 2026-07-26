"""football-data.co.uk loader tests."""

from app.ingestion.football_data import parse_season_csv


def test_parse_season_csv_captures_shots_and_corners() -> None:
    """Wheatcroft shots screen: HS/AS/HST/AST/HC/AC are present in the free
    main-league CSVs and must be captured as Optional[int]."""
    csv_text = (
        "Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR,HS,AS,HST,AST,HC,AC,"
        "B365H,B365D,B365A\n"
        "E0,16/08/2025,Arsenal,Wolves,2,0,H,18,6,7,2,9,3,1.30,5.50,9.00\n"
    )
    rows = parse_season_csv(csv_text)
    assert len(rows) == 1
    r = rows[0]
    assert r.home_shots == 18
    assert r.away_shots == 6
    assert r.home_shots_on_target == 7
    assert r.away_shots_on_target == 2
    assert r.home_corners == 9
    assert r.away_corners == 3


def test_parse_season_csv_tolerates_missing_shots_columns() -> None:
    """Older/minor-league CSVs without match-stats columns -> all None,
    row still parses (backward compatible)."""
    csv_text = (
        "Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR,B365H,B365D,B365A\n"
        "E0,16/08/2014,Arsenal,Wolves,2,0,H,1.30,5.50,9.00\n"
    )
    rows = parse_season_csv(csv_text)
    assert len(rows) == 1
    r = rows[0]
    assert r.home_shots is None
    assert r.away_shots is None
    assert r.home_shots_on_target is None
    assert r.away_shots_on_target is None
    assert r.home_corners is None
    assert r.away_corners is None


def test_parse_season_csv_blank_shots_values_are_none() -> None:
    """Columns present but blank on a row -> None, not 0 and not a skip."""
    csv_text = (
        "Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR,HS,AS,HST,AST,HC,AC\n"
        "E0,16/08/2025,Arsenal,Wolves,2,0,H,,,,,,\n"
    )
    rows = parse_season_csv(csv_text)
    assert len(rows) == 1
    assert rows[0].home_shots is None
    assert rows[0].home_shots_on_target is None
    assert rows[0].home_corners is None


def test_parse_season_csv_extracts_betfair_exchange_open_and_close() -> None:
    """Audit 2026-07-10 (free-data mandate): football-data now carries Betfair
    Exchange OPEN (BFEH/BFED/BFEA) and CLOSE (BFECH/BFECD/BFECA) columns —
    verified ~94% populated on 2025-26 E0 with open != close on 93% of rows.
    With Pinnacle columns dead since ~2026-01-15, the exchange pair is the
    free sharp anchor+close replacement for the football backtest spine."""
    csv_text = (
        "Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR,B365H,B365D,B365A,"
        "PSCH,PSCD,PSCA,BFEH,BFED,BFEA,BFECH,BFECD,BFECA\n"
        "E0,16/08/2025,Arsenal,Wolves,2,0,H,1.30,5.50,9.00,"
        ",,,1.34,6.6,9.4,1.32,6.8,10\n"
    )
    rows = parse_season_csv(csv_text)
    assert len(rows) == 1
    r = rows[0]
    assert r.pinnacle_closing_home is None  # dead columns stay honest-None
    assert r.betfair_exchange_open_home == 1.34
    assert r.betfair_exchange_open_draw == 6.6
    assert r.betfair_exchange_open_away == 9.4
    assert r.betfair_exchange_closing_home == 1.32
    assert r.betfair_exchange_closing_draw == 6.8
    assert r.betfair_exchange_closing_away == 10.0
