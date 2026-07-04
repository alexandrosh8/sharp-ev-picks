"""B3 match-ceiling decomposition — pure assembly + PARITY with the research
script (scripts/research/sport_quality_report.py, the A1 ceiling instrument).

GET /resolution/match-ceiling classifies unmatched OddsPortal-side events with
an app-local mirror of the script's conservative league heuristic; the parity
tests here pin that the mirror never drifts from the script's classification
(the same loader pattern as tests/test_sport_quality_report.py — scripts/ is
not an importable package).
"""

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from app.storage.repositories import (
    _classify_unmatched_event,
    _corrected_match_rates,
    match_ceiling_blocks,
)

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "research"


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sqr: Any = _load(_SCRIPTS / "sport_quality_report.py", "sport_quality_report_ceiling_t")


def test_classifier_parity_with_research_script() -> None:
    co_map = {1: {101}, 2: {202}}
    inwindow_ids = {101}
    inwindow_names = {("england", "premier league"), ("", "atp tour")}
    scenarios = [
        (1, "Anything", "Anywhere"),  # co-mapped to an in-window pinnacle league
        (2, "Anything", "Anywhere"),  # co-mapped to an out-of-window league
        (3, "Premier League", "England"),  # exact normalized name + country
        (3, "premier-league!", "ENGLAND"),  # normalization collapses punctuation/case
        (3, "Premier League", "Scotland"),  # country disagreement
        (3, "ATP Tour", "France"),  # empty pinnacle country matches any
        (4, "Mystery Cup", ""),  # no evidence either way
        (5, "", ""),  # empty name never matches
    ]
    for lid, name, country in scenarios:
        ours = _classify_unmatched_event(
            lid,
            name,
            country,
            co_map=co_map,
            pinnacle_inwindow_ids=inwindow_ids,
            pinnacle_inwindow_names=inwindow_names,
        )
        theirs = sqr.classify_unmatched_event(
            lid,
            name,
            country,
            co_map=co_map,
            pinnacle_inwindow_ids=inwindow_ids,
            pinnacle_inwindow_names=inwindow_names,
        )
        assert ours == theirs, (lid, name, country)


def test_corrected_rates_parity_with_research_script() -> None:
    for args in [(10, 4, 3, 1), (5, 5, 0, 0), (3, 0, 3, 0), (2, 1, 1, 1), (0, 0, 0, 0)]:
        assert _corrected_match_rates(*args) == sqr.corrected_match_rates(*args), args


def test_match_ceiling_blocks_assembly() -> None:
    totals = [("soccer", 10)]
    matched = [("soccer", 4)]
    unmatched = (
        # 3 structural: league 5 co-maps only to pinnacle league 999, which has
        # no in-window event — a match was never possible.
        [("soccer", 5, "Liga X", "Ruritania")] * 3
        # 2 addressable: exact normalized league-name match in-window.
        + [("soccer", 6, "Premier League", "England")] * 2
        # 1 unknown: no league-identity evidence — never guessed.
        + [("soccer", 7, "Mystery Cup", "")]
    )
    co_rows = [(5, 999)]
    pinn_leagues = [("pinnacle_soccer", 101, "Premier League", "England")]
    blocks = match_ceiling_blocks(totals, matched, unmatched, co_rows, pinn_leagues)
    b = blocks["soccer"]
    assert b["events"] == 10
    assert b["matched"] == 4
    assert b["matched_rate"] == pytest.approx(0.4)
    assert b["unmatched"] == 6
    assert b["structural"] == 3
    assert b["addressable"] == 2
    assert b["unknown_league"] == 1
    # lower bound excludes only PROVEN structural; upper also excludes unknown.
    assert b["corrected_match_rate_lower"] == pytest.approx(4 / 7)
    assert b["corrected_match_rate_upper"] == pytest.approx(4 / 6)


def test_match_ceiling_blocks_empty_and_zero_denominators() -> None:
    assert match_ceiling_blocks([], [], [], [], []) == {}
    # a sport whose window has events but all structural: corrected rates are
    # None (undefined denominators), never a fabricated 0 or a crash.
    blocks = match_ceiling_blocks(
        [("tennis", 2)],
        [],
        [("tennis", 9, "Cup", "")] * 2,
        [(9, 900)],
        [],
    )
    b = blocks["tennis"]
    assert b["matched"] == 0
    assert b["structural"] == 2
    assert b["corrected_match_rate_lower"] is None
    assert b["corrected_match_rate_upper"] is None
