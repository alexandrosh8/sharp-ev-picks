"""Betfair historical STREAM loader — pure-parser tests (no network, no DB).

Covers the documented Betfair Exchange Stream market-change (``mcm``) format
(``marketDefinition`` + per-runner ``rc`` price changes): closing-price
derivation (last pre-in-play best-back, BSP preferred when reconciled),
in-play-turn detection, WINNER/LOSER settlement, .bz2 handling, the 1x2 /
moneyline runner->home/draw/away mapping, and the join that attaches the
Betfair sharp close to football-data pre-match rows. All fixtures are SYNTHETIC
(the live data is account-gated / operator-placed), built to the documented
schema. No bets are ever placed.
"""

from __future__ import annotations

import bz2
import io
import json
import tarfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.ingestion.betfair_bsp import (
    DRAW_SELECTION_ID,
    BetfairMarketClose,
    attach_betfair_close,
    home_draw_away_close,
    load_betfair_dir,
    load_betfair_tar,
    parse_market_stream,
)
from app.resolution.matching import default_aliases

HOME_ID = 111
AWAY_ID = 222
MARKET_TIME = "2026-06-28T18:00:00.000Z"


def _mcm(pt: int, *, market_def: dict | None = None, rc: list[dict] | None = None) -> str:
    mc: dict = {"id": "1.234567890"}
    if market_def is not None:
        mc["marketDefinition"] = market_def
    if rc is not None:
        mc["rc"] = rc
    return json.dumps({"op": "mcm", "clk": "AAA", "pt": pt, "mc": [mc]})


def _runner(
    sid: int, name: str, sort: int, *, status: str = "ACTIVE", bsp: float | None = None
) -> dict:
    r: dict = {"id": sid, "name": name, "sortPriority": sort, "status": status}
    if bsp is not None:
        r["bsp"] = bsp
    return r


def _market_def(
    *, in_play: bool, status: str, runners: list[dict], bsp_reconciled: bool = False
) -> dict:
    return {
        "marketType": "MATCH_ODDS",
        "eventTypeId": "1",
        "eventName": "Arsenal v Chelsea",
        "competition": {"id": "10", "name": "English Premier League"},
        "marketTime": MARKET_TIME,
        "openDate": MARKET_TIME,
        "status": status,
        "inPlay": in_play,
        "bspMarket": True,
        "bspReconciled": bsp_reconciled,
        "runners": runners,
    }


def _rc(sid: int, *, back: float | None = None, ltp: float | None = None) -> dict:
    d: dict = {"id": sid}
    if back is not None:
        d["batb"] = [[0, back, 100.0]]
    if ltp is not None:
        d["ltp"] = ltp
    return d


def _soccer_stream(*, with_bsp: bool = False) -> list[str]:
    active = [
        _runner(HOME_ID, "Arsenal", 1),
        _runner(AWAY_ID, "Chelsea", 2),
        _runner(DRAW_SELECTION_ID, "The Draw", 3),
    ]
    settled = [
        _runner(HOME_ID, "Arsenal", 1, status="WINNER", bsp=2.05 if with_bsp else None),
        _runner(AWAY_ID, "Chelsea", 2, status="LOSER", bsp=4.10 if with_bsp else None),
        _runner(DRAW_SELECTION_ID, "The Draw", 3, status="LOSER", bsp=3.55 if with_bsp else None),
    ]
    return [
        # pre-in-play prices
        _mcm(
            1_700_000_000_000,
            market_def=_market_def(in_play=False, status="OPEN", runners=active),
            rc=[
                _rc(HOME_ID, back=2.00),
                _rc(AWAY_ID, back=4.00),
                _rc(DRAW_SELECTION_ID, back=3.50),
            ],
        ),
        # last pre-in-play snapshot (this is the CLOSE when no BSP)
        _mcm(
            1_700_000_060_000,
            rc=[
                _rc(HOME_ID, back=2.10),
                _rc(AWAY_ID, back=3.80),
                _rc(DRAW_SELECTION_ID, back=3.60),
            ],
        ),
        # in-play turn — prices here and after must NOT be the close
        _mcm(
            1_700_000_120_000,
            market_def=_market_def(in_play=True, status="OPEN", runners=active),
            rc=[_rc(HOME_ID, back=1.50)],
        ),
        # settlement
        _mcm(
            1_700_000_900_000,
            market_def=_market_def(
                in_play=True, status="CLOSED", runners=settled, bsp_reconciled=with_bsp
            ),
        ),
    ]


def test_parse_close_is_last_pre_inplay_best_back() -> None:
    market = parse_market_stream(_soccer_stream(with_bsp=False))
    assert market is not None
    assert isinstance(market, BetfairMarketClose)
    assert market.market_type == "MATCH_ODDS"
    assert market.event_name == "Arsenal v Chelsea"
    assert market.competition == "English Premier League"
    assert market.settled is True
    by_id = {r.selection_id: r for r in market.runners}
    # close = last pre-in-play best-back (the 2nd message), in-play 1.50 ignored
    assert by_id[HOME_ID].close_price == Decimal("2.10")
    assert by_id[AWAY_ID].close_price == Decimal("3.80")
    assert by_id[DRAW_SELECTION_ID].close_price == Decimal("3.60")
    assert by_id[HOME_ID].won is True
    assert by_id[AWAY_ID].won is False


def test_kickoff_and_inplay_are_utc_aware() -> None:
    market = parse_market_stream(_soccer_stream())
    assert market is not None
    assert market.kickoff_utc == datetime(2026, 6, 28, 18, 0, 0, tzinfo=UTC)
    assert market.kickoff_utc.tzinfo is not None  # never naive
    assert market.in_play_utc is not None
    assert market.in_play_utc.tzinfo is not None
    # in-play turn = pt of the 3rd message
    assert market.in_play_utc == datetime.fromtimestamp(1_700_000_120_000 / 1000, tz=UTC)


def test_out_of_range_publish_timestamp_is_ignored_not_raised() -> None:
    lines = _soccer_stream()
    message = json.loads(lines[2])
    message["pt"] = 10**100
    lines[2] = json.dumps(message)
    market = parse_market_stream(lines)
    assert market is not None
    assert market.in_play_utc is None


def test_bsp_preferred_as_close_when_reconciled() -> None:
    market = parse_market_stream(_soccer_stream(with_bsp=True))
    assert market is not None
    by_id = {r.selection_id: r for r in market.runners}
    # BSP overrides the pre-in-play best-back as the settled sharp close
    assert by_id[HOME_ID].bsp == Decimal("2.05")
    assert by_id[HOME_ID].close_price == Decimal("2.05")
    assert by_id[AWAY_ID].close_price == Decimal("4.10")


def test_skips_non_mcm_and_blank_lines() -> None:
    lines = ["", '{"op":"status","id":1}', *_soccer_stream(), "   "]
    market = parse_market_stream(lines)
    assert market is not None
    assert len(market.runners) == 3


def test_malformed_nested_change_containers_do_not_abort_valid_siblings() -> None:
    malformed_mc = json.dumps({"op": "mcm", "pt": 1_700_000_000_000, "mc": {"id": "bad"}})
    malformed_rc = json.dumps(
        {
            "op": "mcm",
            "pt": 1_700_000_000_001,
            "mc": [{"id": "bad", "rc": {"id": HOME_ID}}],
        }
    )
    market = parse_market_stream([malformed_mc, malformed_rc, *_soccer_stream()])
    assert market is not None
    assert market.market_id == "1.234567890"


def test_home_draw_away_close_maps_by_name_and_draw_id() -> None:
    market = parse_market_stream(_soccer_stream())
    assert market is not None
    res = home_draw_away_close(market, "Arsenal", "Chelsea", aliases=default_aliases())
    assert res is not None
    assert res.home_close == Decimal("2.10")
    assert res.draw_close == Decimal("3.60")
    assert res.away_close == Decimal("3.80")
    assert res.result == "H"


def _basketball_stream() -> list[str]:
    active = [_runner(HOME_ID, "Boston Celtics", 1), _runner(AWAY_ID, "Miami Heat", 2)]
    settled = [
        _runner(HOME_ID, "Boston Celtics", 1, status="LOSER"),
        _runner(AWAY_ID, "Miami Heat", 2, status="WINNER"),
    ]
    bdef = {
        "marketType": "MATCH_ODDS",
        "eventTypeId": "7522",
        "eventName": "Celtics @ Heat",
        "competition": {"id": "9", "name": "NBA"},
        "marketTime": MARKET_TIME,
        "status": "OPEN",
        "inPlay": False,
        "bspMarket": False,
        "runners": active,
    }
    return [
        _mcm(
            1_700_000_000_000,
            market_def=bdef,
            rc=[_rc(HOME_ID, back=1.80), _rc(AWAY_ID, back=2.10)],
        ),
        _mcm(
            1_700_000_120_000,
            market_def={**bdef, "inPlay": True, "status": "CLOSED", "runners": settled},
        ),
    ]


def test_basketball_two_way_has_no_draw() -> None:
    market = parse_market_stream(_basketball_stream())
    assert market is not None
    assert market.event_type_id == "7522"
    res = home_draw_away_close(market, "Boston Celtics", "Miami Heat", aliases=default_aliases())
    assert res is not None
    assert res.home_close == Decimal("1.80")
    assert res.away_close == Decimal("2.10")
    assert res.draw_close is None
    assert res.result == "A"


def test_load_betfair_dir_reads_bz2_and_plain(tmp_path: Path) -> None:
    plain = tmp_path / "1.234567890.json"
    plain.write_text("\n".join(_soccer_stream()), encoding="utf-8")
    compressed = tmp_path / "1.999999999.bz2"
    compressed.write_bytes(bz2.compress("\n".join(_basketball_stream()).encode("utf-8")))
    markets = load_betfair_dir(tmp_path)
    assert len(markets) == 2
    types = {m.event_type_id for m in markets}
    assert types == {"1", "7522"}


def test_load_betfair_dir_malformed_nested_member_does_not_abort_sibling(tmp_path: Path) -> None:
    (tmp_path / "bad.json").write_text(
        json.dumps({"op": "mcm", "mc": {"id": "bad"}}), encoding="utf-8"
    )
    (tmp_path / "good.json").write_text("\n".join(_soccer_stream()), encoding="utf-8")

    markets = load_betfair_dir(tmp_path)

    assert [market.market_id for market in markets] == ["1.234567890"]


def test_load_betfair_dir_absent_is_empty(tmp_path: Path) -> None:
    assert load_betfair_dir(tmp_path / "nope") == []


def test_load_betfair_dir_skips_member_over_decompressed_limit(tmp_path: Path) -> None:
    compressed = tmp_path / "oversized.bz2"
    compressed.write_bytes(bz2.compress("\n".join(_soccer_stream()).encode("utf-8")))
    assert load_betfair_dir(tmp_path, max_decompressed_bytes=64) == []


def _soccer_over_under_stream() -> list[str]:
    """A soccer (eventTypeId=1) but NON-MATCH_ODDS market — must be skipped."""
    runners = [_runner(901, "Over 2.5 Goals", 1), _runner(902, "Under 2.5 Goals", 2)]
    mdef = {
        "marketType": "OVER_UNDER_25",
        "eventTypeId": "1",
        "eventName": "Arsenal v Chelsea",
        "competition": {"id": "10", "name": "English Premier League"},
        "marketTime": MARKET_TIME,
        "status": "OPEN",
        "inPlay": False,
        "runners": runners,
    }
    return [
        _mcm(1_700_000_000_000, market_def=mdef, rc=[_rc(901, back=1.90), _rc(902, back=1.95)]),
        _mcm(
            1_700_000_120_000,
            market_def={**mdef, "inPlay": True, "status": "CLOSED", "runners": runners},
        ),
    ]


def _make_betfair_tar(path: Path) -> None:
    """Build a fixture tar mimicking BASIC/YYYY/Mon/Day/EVENT/MARKET.bz2 layout
    with three members: soccer MATCH_ODDS (kept), basketball MATCH_ODDS (skip,
    wrong sport), soccer OVER_UNDER_25 (skip, wrong market type)."""
    members = {
        "BASIC/2024/Aug/2/111/1.111.bz2": _soccer_stream(with_bsp=True),
        "BASIC/2024/Aug/2/222/1.222.bz2": _basketball_stream(),
        "BASIC/2024/Aug/2/333/1.333.bz2": _soccer_over_under_stream(),
    }
    with tarfile.open(path, "w") as tar:
        for name, lines in members.items():
            payload = bz2.compress("\n".join(lines).encode("utf-8"))
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))


OVER_25_ID = 47973
UNDER_25_ID = 47972


def _soccer_ou_stream(*, over_wins: bool = True, with_bsp: bool = False) -> list[str]:
    """Soccer (eventTypeId=1) OVER_UNDER_25 market with a settled result.

    Runners use the real fixed Betfair 2.5-line selection ids (Over 47973 /
    Under 47972). Close = last pre-in-play best-back unless ``with_bsp``."""
    over_status = "WINNER" if over_wins else "LOSER"
    under_status = "LOSER" if over_wins else "WINNER"
    active = [_runner(OVER_25_ID, "Over 2.5 Goals", 1), _runner(UNDER_25_ID, "Under 2.5 Goals", 2)]
    settled = [
        _runner(
            OVER_25_ID, "Over 2.5 Goals", 1, status=over_status, bsp=1.80 if with_bsp else None
        ),
        _runner(
            UNDER_25_ID, "Under 2.5 Goals", 2, status=under_status, bsp=2.15 if with_bsp else None
        ),
    ]
    mdef = {
        "marketType": "OVER_UNDER_25",
        "eventTypeId": "1",
        "eventName": "Arsenal v Chelsea",
        "competition": {"id": "10", "name": "English Premier League"},
        "marketTime": MARKET_TIME,
        "status": "OPEN",
        "inPlay": False,
        "bspMarket": True,
        "bspReconciled": with_bsp,
        "runners": active,
    }
    return [
        _mcm(
            1_700_000_000_000,
            market_def=mdef,
            rc=[_rc(OVER_25_ID, back=1.90), _rc(UNDER_25_ID, back=2.05)],
        ),
        # last pre-in-play snapshot — the CLOSE when no BSP
        _mcm(1_700_000_060_000, rc=[_rc(OVER_25_ID, back=1.85), _rc(UNDER_25_ID, back=2.10)]),
        # in-play turn — must NOT be the close
        _mcm(
            1_700_000_120_000, market_def={**mdef, "inPlay": True}, rc=[_rc(OVER_25_ID, back=1.40)]
        ),
        _mcm(
            1_700_000_900_000,
            market_def={
                **mdef,
                "inPlay": True,
                "status": "CLOSED",
                "bspReconciled": with_bsp,
                "runners": settled,
            },
        ),
    ]


def _soccer_ah_stream() -> list[str]:
    """Soccer (eventTypeId=1) ASIAN_HANDICAP market — runners carry handicap lines."""
    runners = [
        {"id": 111, "name": "Arsenal", "sortPriority": 1, "status": "ACTIVE", "hc": -1.0},
        {"id": 222, "name": "Chelsea", "sortPriority": 2, "status": "ACTIVE", "hc": 1.0},
    ]
    mdef = {
        "marketType": "ASIAN_HANDICAP",
        "eventTypeId": "1",
        "eventName": "Arsenal v Chelsea",
        "competition": {"id": "10", "name": "English Premier League"},
        "marketTime": MARKET_TIME,
        "status": "OPEN",
        "inPlay": False,
        "runners": runners,
    }
    return [
        _mcm(1_700_000_000_000, market_def=mdef, rc=[_rc(111, back=1.95), _rc(222, back=1.95)]),
        _mcm(
            1_700_000_120_000,
            market_def={**mdef, "inPlay": True, "status": "CLOSED", "runners": runners},
        ),
    ]


def test_over_under_close_parses_and_settles() -> None:
    from app.ingestion.betfair_bsp import over_under_close

    market = parse_market_stream(_soccer_ou_stream(over_wins=True))
    assert market is not None
    assert market.market_type == "OVER_UNDER_25"
    ou = over_under_close(market)
    assert ou is not None
    # close = last pre-in-play best-back (2nd message); in-play 1.40 ignored
    assert ou.over_close == Decimal("1.85")
    assert ou.under_close == Decimal("2.10")
    assert ou.result == "O"


def test_over_under_close_prefers_bsp_and_settles_under() -> None:
    from app.ingestion.betfair_bsp import over_under_close

    market = parse_market_stream(_soccer_ou_stream(over_wins=False, with_bsp=True))
    assert market is not None
    ou = over_under_close(market)
    assert ou is not None
    assert ou.over_close == Decimal("1.80")  # reconciled BSP overrides snapshot
    assert ou.under_close == Decimal("2.15")
    assert ou.result == "U"


def test_event_name_home_away_parses_and_rejects_garbage() -> None:
    from app.ingestion.betfair_bsp import event_name_home_away

    assert event_name_home_away("Real Madrid v Atalanta") == ("Real Madrid", "Atalanta")
    assert event_name_home_away("Brighton & Hove Albion v Wolves") == (
        "Brighton & Hove Albion",
        "Wolves",
    )
    assert event_name_home_away(None) is None
    assert event_name_home_away("no separator here") is None
    assert event_name_home_away("a v b v c") is None  # ambiguous -> refuse


def test_attach_betfair_ou_close_joins_and_rejects_total_goals_conflict() -> None:
    from app.ingestion.betfair_bsp import attach_betfair_ou_close

    market = parse_market_stream(_soccer_ou_stream(over_wins=True))
    assert market is not None
    aliases = default_aliases()
    good = {
        "Date": "28/06/2026",
        "HomeTeam": "Arsenal",
        "AwayTeam": "Chelsea",
        "Max>2.5": "1.95",
        "Max<2.5": "2.00",
        "P>2.5": "1.85",
        "P<2.5": "2.05",
        "FTHG": "2",
        "FTAG": "1",  # total 3 -> Over (agrees with Betfair Over WINNER)
    }
    # total 1 -> Under, but Betfair settled Over -> result conflict -> drop
    conflict = {**good, "FTHG": "1", "FTAG": "0"}
    joined, stats = attach_betfair_ou_close([good, conflict], [market], aliases=aliases)
    assert stats.n_fd_rows == 2
    assert stats.n_markets == 1
    assert stats.n_joined == 1
    assert stats.n_result_conflict == 1
    row = joined[0]
    # Betfair Over/Under close written into the closing slots (decimal odds)
    assert float(row["PC>2.5"]) == 1.85
    assert float(row["PC<2.5"]) == 2.10
    assert float(row["MaxC>2.5"]) == 1.85
    # pre-match Max preserved untouched
    assert row["Max>2.5"] == "1.95"


def test_load_betfair_tar_by_type_extracts_ou_and_ah(tmp_path: Path) -> None:
    from app.ingestion.betfair_bsp import load_betfair_tar_by_type

    tar_path = tmp_path / "data.tar"
    members = {
        "BASIC/2024/Aug/2/111/1.111.bz2": _soccer_stream(with_bsp=True),  # MATCH_ODDS
        "BASIC/2024/Aug/2/222/1.222.bz2": _basketball_stream(),  # wrong sport -> skip
        "BASIC/2024/Aug/2/333/1.333.bz2": _soccer_ou_stream(over_wins=True),  # OVER_UNDER_25
        "BASIC/2024/Aug/2/444/1.444.bz2": _soccer_ah_stream(),  # ASIAN_HANDICAP
    }
    with tarfile.open(tar_path, "w") as tar:
        for name, lines in members.items():
            payload = bz2.compress("\n".join(lines).encode("utf-8"))
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))

    buckets = load_betfair_tar_by_type(
        tar_path, market_types=("MATCH_ODDS", "OVER_UNDER_25", "ASIAN_HANDICAP")
    )
    assert set(buckets) == {"MATCH_ODDS", "OVER_UNDER_25", "ASIAN_HANDICAP"}
    assert len(buckets["MATCH_ODDS"]) == 1
    assert len(buckets["OVER_UNDER_25"]) == 1
    assert len(buckets["ASIAN_HANDICAP"]) == 1
    # basketball MATCH_ODDS (eventTypeId 7522) is excluded by the soccer filter
    assert all(m.event_type_id == "1" for ms in buckets.values() for m in ms)
    assert buckets["OVER_UNDER_25"][0].market_type == "OVER_UNDER_25"
    assert buckets["ASIAN_HANDICAP"][0].market_type == "ASIAN_HANDICAP"


def test_load_betfair_tar_by_type_absent_is_empty_buckets(tmp_path: Path) -> None:
    from app.ingestion.betfair_bsp import load_betfair_tar_by_type

    buckets = load_betfair_tar_by_type(tmp_path / "nope.tar", market_types=("OVER_UNDER_25",))
    assert buckets == {"OVER_UNDER_25": []}


def test_load_betfair_tar_keeps_only_soccer_match_odds(tmp_path: Path) -> None:
    tar_path = tmp_path / "data.tar"
    _make_betfair_tar(tar_path)
    markets = load_betfair_tar(tar_path)
    # Only the soccer MATCH_ODDS member survives the cheap peek filter.
    assert len(markets) == 1
    m = markets[0]
    assert m.event_type_id == "1"
    assert m.market_type == "MATCH_ODDS"
    assert m.event_name == "Arsenal v Chelsea"
    # parse_market_stream was reused: BSP-reconciled close is preserved.
    by_id = {r.selection_id: r for r in m.runners}
    assert by_id[HOME_ID].close_price == Decimal("2.05")
    assert by_id[HOME_ID].won is True


def test_load_betfair_tar_enforces_member_and_archive_limits(tmp_path: Path) -> None:
    tar_path = tmp_path / "data.tar"
    _make_betfair_tar(tar_path)
    assert load_betfair_tar(tar_path, max_compressed_bytes=1) == []
    assert load_betfair_tar(tar_path, max_members=0) == []


def test_load_betfair_tar_absent_is_empty(tmp_path: Path) -> None:
    assert load_betfair_tar(tmp_path / "nope.tar") == []


def test_market_cache_round_trips(tmp_path: Path) -> None:
    from app.ingestion.betfair_bsp import read_market_cache, write_market_cache

    original = [
        parse_market_stream(_soccer_stream(with_bsp=True)),
        parse_market_stream(_soccer_stream(with_bsp=False)),
    ]
    assert all(m is not None for m in original)
    cache = tmp_path / "soccer_match_odds.jsonl.gz"
    n = write_market_cache(cache, [m for m in original if m is not None])
    assert n == 2
    restored = read_market_cache(cache)
    assert restored == original  # frozen dataclasses compare by value (Decimal/UTC intact)
    # spot-check the load-bearing fields survive the JSON boundary as Decimal/UTC
    by_id = {r.selection_id: r for r in restored[0].runners}
    assert by_id[HOME_ID].close_price == Decimal("2.05")
    assert isinstance(by_id[HOME_ID].close_price, Decimal)
    assert restored[0].kickoff_utc == datetime(2026, 6, 28, 18, 0, 0, tzinfo=UTC)
    assert restored[0].kickoff_utc.tzinfo is not None


def test_read_market_cache_absent_is_empty(tmp_path: Path) -> None:
    from app.ingestion.betfair_bsp import read_market_cache

    assert read_market_cache(tmp_path / "nope.jsonl.gz") == []


def test_attach_betfair_close_joins_and_rejects_result_mismatch() -> None:
    market = parse_market_stream(_soccer_stream())
    assert market is not None
    aliases = default_aliases()
    # fd-style pre-match rows (football-data Max soft) — one match, one mismatch
    good = {
        "Date": "28/06/2026",
        "HomeTeam": "Arsenal",
        "AwayTeam": "Chelsea",
        "MaxH": "2.20",
        "MaxD": "3.70",
        "MaxA": "3.90",
        "PSH": "2.05",
        "PSD": "3.50",
        "PSA": "3.70",
        "FTR": "H",
        "FTHG": "1",
        "FTAG": "0",
    }
    mismatch = {**good, "FTR": "A"}  # football-data says Away, Betfair says Home -> drop
    joined, stats = attach_betfair_close([good, mismatch], [market], aliases=aliases)
    assert stats.n_fd_rows == 2
    assert stats.n_markets == 1
    assert stats.n_joined == 1
    assert stats.n_result_conflict == 1
    assert len(joined) == 1
    row = joined[0]
    # Betfair sharp close written into the closing slots (decimal odds)
    assert float(row["PSCH"]) == 2.10
    assert float(row["PSCD"]) == 3.60
    assert float(row["PSCA"]) == 3.80
    # pre-match Max prices preserved untouched
    assert row["MaxH"] == "2.20"


# --------------------------------------------------------------------------- #
# Parser audit 2026-07-03 — F1 (ATB delta ladders) + F4 (settledTime/eventId)
# --------------------------------------------------------------------------- #
def test_atb_deltas_are_ladder_updates_not_snapshots() -> None:
    """Stream batb entries are per-LEVEL DELTAS: a level-1-only update must not
    displace the standing level-0 best, and a level-0 removal falls to the
    next populated level — never to a stale ltp."""
    active = [
        _runner(HOME_ID, "Arsenal", 1),
        _runner(AWAY_ID, "Chelsea", 2),
        _runner(DRAW_SELECTION_ID, "The Draw", 3),
    ]
    settled = [
        _runner(HOME_ID, "Arsenal", 1, status="WINNER"),
        _runner(AWAY_ID, "Chelsea", 2, status="LOSER"),
        _runner(DRAW_SELECTION_ID, "The Draw", 3, status="LOSER"),
    ]
    lines = [
        _mcm(
            1_700_000_000_000,
            market_def=_market_def(in_play=False, status="OPEN", runners=active),
            rc=[
                {"id": HOME_ID, "batb": [[0, 2.00, 50.0], [1, 1.98, 80.0]], "ltp": 5.0},
                _rc(AWAY_ID, back=4.00),
                _rc(DRAW_SELECTION_ID, back=3.50),
            ],
        ),
        # level-1-only delta: best back stays 2.00 (the old parser read 1.96)
        _mcm(1_700_000_030_000, rc=[{"id": HOME_ID, "batb": [[1, 1.96, 90.0]]}]),
        # level-0 removal: best falls to level 1 (1.96), NOT the stale ltp 5.0
        _mcm(1_700_000_060_000, rc=[{"id": HOME_ID, "batb": [[0, 0, 0]]}]),
        _mcm(
            1_700_000_120_000,
            market_def=_market_def(in_play=True, status="OPEN", runners=active),
        ),
        _mcm(
            1_700_000_900_000,
            market_def=_market_def(in_play=True, status="CLOSED", runners=settled),
        ),
    ]
    market = parse_market_stream(lines)
    assert market is not None
    by_id = {r.selection_id: r for r in market.runners}
    assert by_id[HOME_ID].close_price == Decimal("1.96")
    assert by_id[AWAY_ID].close_price == Decimal("4.00")


def test_settled_time_and_event_id_are_captured_and_cached(tmp_path: Path) -> None:
    lines = _soccer_stream(with_bsp=False)
    import json as _json

    # graft eventId + settledTime onto the final CLOSED definition
    last = _json.loads(lines[-1])
    mdef = last["mc"][0]["marketDefinition"]
    mdef["eventId"] = "28202626"
    mdef["settledTime"] = "2026-06-28T20:05:38.000Z"
    lines[-1] = _json.dumps(last)

    market = parse_market_stream(lines)
    assert market is not None
    assert market.event_id == "28202626"
    assert market.settled_time_utc == datetime(2026, 6, 28, 20, 5, 38, tzinfo=UTC)

    from app.ingestion.betfair_bsp import read_market_cache, write_market_cache

    cache = tmp_path / "cache.jsonl.gz"
    write_market_cache(cache, [market])
    (back,) = read_market_cache(cache)
    assert back.event_id == "28202626"
    assert back.settled_time_utc == market.settled_time_utc


def test_pre_f4_cache_lines_still_load(tmp_path: Path) -> None:
    """Caches written before the event_id/settled_time_utc fields must load
    with None defaults (the on-disk jsonl.gz caches are expensive to rebuild)."""
    import gzip
    import json as _json

    from app.ingestion.betfair_bsp import _market_to_dict, read_market_cache

    market = parse_market_stream(_soccer_stream())
    assert market is not None
    old_dict = _market_to_dict(market)
    old_dict.pop("event_id")
    old_dict.pop("settled_time_utc")
    cache = tmp_path / "old.jsonl.gz"
    with gzip.open(cache, "wt", encoding="utf-8") as fh:
        fh.write(_json.dumps(old_dict) + "\n")
    (back,) = read_market_cache(cache)
    assert back.event_id is None
    assert back.settled_time_utc is None
    assert back.market_type == "MATCH_ODDS"
