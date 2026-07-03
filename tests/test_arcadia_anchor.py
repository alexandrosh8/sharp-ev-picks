"""ARCADIA anchor validation workflow (app/backtesting/arcadia_anchor.py) —
exporter schema, fail-closed matcher rejections, preflight DO-NOT-RUN,
contamination guards, and live-path isolation. Pure: no network, no DB."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.backtesting.arcadia_anchor import (
    ACCEPTANCE_MIN_N_PER_MARKET,
    ANCHOR_SOURCE,
    EXPORT_COLUMNS,
    FROZEN_CONFIG_SHA256,
    PREFLIGHT_MIN_USABLE_EVENTS_PER_MONTH,
    AnchorRow,
    SnapshotObs,
    ValidationFixture,
    build_anchor_rows,
    evaluate_contamination_guards,
    match_anchor_to_fixture,
    preflight_report,
    read_dataset,
    write_dataset,
)
from app.resolution import AliasTable, default_aliases

KO = datetime(2026, 7, 10, 15, 0, tzinfo=UTC)  # inside the eligible H2 window


@pytest.fixture(scope="module")
def aliases() -> AliasTable:
    return default_aliases()


def obs(
    *,
    snapshot_id: int = 1,
    event_id: int = 100,
    market: str = "h2h",
    selection: str = "Alpha FC",
    odds: str = "2.10",
    captured_at: datetime | None = None,
    starts_at: datetime = KO,
    home: str = "Alpha FC",
    away: str = "Beta United",
    sport_key: str = "pinnacle_soccer",
    league: str = "England - Premier League",
) -> SnapshotObs:
    return SnapshotObs(
        snapshot_id=snapshot_id,
        event_id=event_id,
        sport_key=sport_key,
        league=league,
        home=home,
        away=away,
        starts_at=starts_at,
        market=market,
        selection=selection,
        decimal_odds=Decimal(odds),
        captured_at=captured_at or (starts_at - timedelta(hours=2)),
    )


def h2h_set(
    event_id: int = 100,
    captured_at: datetime | None = None,
    *,
    starts_at: datetime = KO,
    home: str = "Alpha FC",
    away: str = "Beta United",
    base_id: int = 1,
) -> list[SnapshotObs]:
    cap = captured_at or (starts_at - timedelta(hours=2))
    return [
        obs(
            snapshot_id=base_id + i,
            event_id=event_id,
            selection=sel,
            captured_at=cap,
            starts_at=starts_at,
            home=home,
            away=away,
        )
        for i, sel in enumerate((home, "Draw", away))
    ]


def usable_anchor_rows(
    aliases: AliasTable, event_id: int = 100, base_id: int = 1
) -> list[AnchorRow]:
    rows = build_anchor_rows(h2h_set(event_id, base_id=base_id), aliases=aliases)
    assert any(r.usable for r in rows)
    return rows


# --------------------------------------------------------------------------- #
# Exporter schema + provenance
# --------------------------------------------------------------------------- #
def test_export_schema_has_all_required_fields() -> None:
    required = {
        "source",
        "source_event_id",
        "source_market_id",
        "canonical_event_id",
        "sport",
        "league",
        "home",
        "away",
        "event_start_time_utc",
        "market_type",
        "period",
        "line",
        "selection",
        "price",
        "captured_at",
        "freshness_seconds",
        "raw_snapshot_id",
        "parser_method",
        "match_confidence",
        "match_method",
        "usable",
        "rejection_reason",
    }
    assert required <= set(EXPORT_COLUMNS)


def test_dataset_roundtrip_and_no_overwrite(tmp_path: Path, aliases: AliasTable) -> None:
    rows = usable_anchor_rows(aliases)
    out = tmp_path / "anchors.csv"
    sha = write_dataset(out, rows)
    assert len(sha) == 64
    back = read_dataset(out)
    assert [r.raw_snapshot_id for r in back] == [r.raw_snapshot_id for r in rows]
    assert back[0].price == rows[0].price  # Decimal survives as string
    with pytest.raises(FileExistsError):
        write_dataset(out, rows)  # raw outputs are never overwritten


def test_rejected_rows_are_preserved_with_reasons(aliases: AliasTable) -> None:
    observations = [
        # unsupported market key
        obs(snapshot_id=50, market="asian_handicap_-0_25", selection="Alpha FC"),
        # incomplete outcome set (home price only)
        obs(snapshot_id=51, selection="Alpha FC", captured_at=KO - timedelta(hours=3)),
    ]
    rows = build_anchor_rows(observations, aliases=aliases)
    by_reason = {r.rejection_reason for r in rows if not r.usable}
    assert "unsupported_market" in by_reason
    assert "incomplete_outcome_set" in by_reason
    assert len(rows) == 2  # nothing silently dropped


def test_provenance_preserved_on_every_row(aliases: AliasTable) -> None:
    rows = build_anchor_rows(h2h_set(), aliases=aliases)
    for r in rows:
        assert r.raw_snapshot_id > 0
        assert r.parser_method
        assert r.captured_at.endswith("Z")
        assert r.event_start_time_utc.endswith("Z")
        assert r.freshness_seconds is not None
        assert r.source == ANCHOR_SOURCE


def test_stale_anchor_rejected(aliases: AliasTable) -> None:
    rows = build_anchor_rows(h2h_set(captured_at=KO - timedelta(hours=30)), aliases=aliases)
    assert all(not r.usable for r in rows)
    assert {r.rejection_reason for r in rows} == {"anchor_stale"}


def test_tautological_close_excluded(aliases: AliasTable) -> None:
    anchor_set = h2h_set(captured_at=KO - timedelta(seconds=3700), base_id=1)
    close_set = h2h_set(captured_at=KO - timedelta(seconds=3500), base_id=10)
    rows = build_anchor_rows(anchor_set + close_set, aliases=aliases)
    closes = [r for r in rows if r.role == "close_secondary"]
    assert closes and all(r.rejection_reason == "close_tautological" for r in closes)
    anchors = [r for r in rows if r.role == "anchor"]
    assert anchors and all(r.usable for r in anchors)  # the anchor itself survives


# --------------------------------------------------------------------------- #
# Fail-closed matcher
# --------------------------------------------------------------------------- #
def fixture(
    *,
    sport: str = "soccer",
    league: str = "E0",
    home: str = "Alpha FC",
    away: str = "Beta United",
    kickoff: datetime = KO,
    market_type: str = "1x2",
    period: str = "match",
    line: float | None = None,
    selection: str = "home",
) -> ValidationFixture:
    return ValidationFixture(
        sport=sport,
        league=league,
        home=home,
        away=away,
        kickoff=kickoff,
        market_type=market_type,
        period=period,
        line=line,
        selection=selection,
    )


def test_matcher_accepts_exact_fixture(aliases: AliasTable) -> None:
    rows = usable_anchor_rows(aliases)
    match, reason = match_anchor_to_fixture(fixture(), rows, aliases=aliases)
    assert reason is None
    assert match is not None and match.selection == "home"


def test_missing_anchor_rejected(aliases: AliasTable) -> None:
    match, reason = match_anchor_to_fixture(fixture(), [], aliases=aliases)
    assert match is None and reason == "anchor_missing"


def test_stale_anchor_never_matchable(aliases: AliasTable) -> None:
    rows = build_anchor_rows(h2h_set(captured_at=KO - timedelta(hours=30)), aliases=aliases)
    match, reason = match_anchor_to_fixture(fixture(), rows, aliases=aliases)
    assert match is None and reason == "anchor_missing"  # stale rows are not candidates


def test_market_line_mismatch_rejected(aliases: AliasTable) -> None:
    rows = usable_anchor_rows(aliases)
    match, reason = match_anchor_to_fixture(
        fixture(market_type="ou25", line=3.5, selection="over"), rows, aliases=aliases
    )
    assert match is None and reason == "market_mismatch"


def test_home_away_reversal_rejected(aliases: AliasTable) -> None:
    rows = usable_anchor_rows(aliases)
    match, reason = match_anchor_to_fixture(
        fixture(home="Beta United", away="Alpha FC"), rows, aliases=aliases
    )
    assert match is None and reason == "event_unmatched"  # flips are never allowed


def test_kickoff_drift_rejected(aliases: AliasTable) -> None:
    rows = usable_anchor_rows(aliases)
    match, reason = match_anchor_to_fixture(
        fixture(kickoff=KO + timedelta(minutes=90)), rows, aliases=aliases
    )
    assert match is None and reason == "kickoff_out_of_window"


def test_marker_mismatch_rejected(aliases: AliasTable) -> None:
    rows = usable_anchor_rows(aliases)
    match, reason = match_anchor_to_fixture(
        fixture(home="Alpha FC Women", away="Beta United Women"), rows, aliases=aliases
    )
    assert match is None  # one-sided women marker is a categorical veto


def test_ambiguous_match_rejected(aliases: AliasTable) -> None:
    rows = usable_anchor_rows(aliases, event_id=100, base_id=1) + usable_anchor_rows(
        aliases, event_id=200, base_id=20
    )
    match, reason = match_anchor_to_fixture(fixture(), rows, aliases=aliases)
    assert match is None and reason == "event_ambiguous"


def test_league_country_contradiction_rejected(aliases: AliasTable) -> None:
    rows = usable_anchor_rows(aliases)
    match, reason = match_anchor_to_fixture(
        fixture(league="Spain - La Liga"), rows, aliases=aliases
    )
    assert match is None  # anchor league is England - ... => country contradiction


# --------------------------------------------------------------------------- #
# Preflight
# --------------------------------------------------------------------------- #
def test_preflight_do_not_run_on_low_coverage(aliases: AliasTable) -> None:
    report = preflight_report(usable_anchor_rows(aliases))
    assert report["verdict"] == "DO-NOT-RUN"
    assert report["failures"]
    assert report["usable_anchor_rows"] > 0


def test_preflight_pass_when_coverage_sufficient(aliases: AliasTable) -> None:
    template = usable_anchor_rows(aliases)
    usable = [r for r in template if r.usable and r.role == "anchor"]
    rows: list[AnchorRow] = []
    needed_events = max(
        PREFLIGHT_MIN_USABLE_EVENTS_PER_MONTH,
        ACCEPTANCE_MIN_N_PER_MARKET * 3 * 20,  # comfortably clear expected-n bar
    )
    for i in range(needed_events):
        for r in usable:
            rows.append(
                replace(
                    r,
                    source_event_id=1000 + i,
                    source_market_id=f"{1000 + i}:h2h",
                    raw_snapshot_id=10_000 + i * 3,
                    match_confidence=1.0,
                    match_method="exact_canonical",
                )
            )
    report = preflight_report(rows)
    assert report["verdict"] == "PASS", report["failures"]
    assert report["acceptance_bar_reachable"]["1x2"] is True
    assert report["event_coverage_by_month"]["2026-07"] == needed_events


# --------------------------------------------------------------------------- #
# Contamination guards
# --------------------------------------------------------------------------- #
def test_guards_stop_on_spent_data_and_bad_config(tmp_path: Path, aliases: AliasTable) -> None:
    dataset = tmp_path / "anchors.csv"
    write_dataset(dataset, usable_anchor_rows(aliases))
    violations = evaluate_contamination_guards(
        dataset_path=dataset,
        input_sha256s=["9123d3203f79e33ac09e2a6a2f8d91ccff8d684ac74e0a02cbde9002440ed330"],
        config_sha256="deadbeef",
        preflight={"verdict": "DO-NOT-RUN"},
    )
    text = "\n".join(violations)
    assert "SPENT input data" in text
    assert "config hash mismatch" in text
    assert "preflight verdict is not PASS" in text


def test_guards_stop_on_missing_dataset_and_preflight() -> None:
    violations = evaluate_contamination_guards(
        dataset_path=Path("/nonexistent/anchors.csv"), preflight=None
    )
    text = "\n".join(violations)
    assert "anchor dataset missing" in text
    assert "no preflight report" in text


def test_guards_stop_on_window_and_source(tmp_path: Path, aliases: AliasTable) -> None:
    from datetime import date

    dataset = tmp_path / "anchors.csv"
    write_dataset(dataset, usable_anchor_rows(aliases))
    violations = evaluate_contamination_guards(
        dataset_path=dataset,
        window_start=date(2026, 1, 1),  # spent Jan-Jun window
        window_end=date(2027, 3, 1),  # beyond eligible end
        preflight={"verdict": "PASS"},
        anchor_source="football-data",
    )
    text = "\n".join(violations)
    assert "window start" in text
    assert "window end" in text
    assert "not approved in the pre-registration" in text


def test_guards_clear_on_valid_setup(tmp_path: Path, aliases: AliasTable) -> None:
    from datetime import date

    dataset = tmp_path / "anchors.csv"
    sha = write_dataset(dataset, usable_anchor_rows(aliases))
    violations = evaluate_contamination_guards(
        dataset_path=dataset,
        dataset_sha256=sha,
        input_sha256s=["a" * 64],
        window_start=date(2026, 7, 1),
        window_end=date(2026, 12, 31),
        config_sha256=FROZEN_CONFIG_SHA256,
        output_dir=tmp_path,
        preflight={"verdict": "PASS"},
        preflight_dataset_sha256=sha,
    )
    assert violations == []


# --------------------------------------------------------------------------- #
# H2 frozen-eval constants (split design frozen 2026-07-03)
# --------------------------------------------------------------------------- #
def test_frozen_eval_constants_match_the_adr() -> None:
    """The pure-prospective single-shot loads parameters from these pinned
    constants — never free CLI numbers. Values are the ADR-0019 frozen H2
    thresholds and the POWER devig default; changing them voids the
    pre-registration."""
    from app.backtesting.arcadia_anchor import FROZEN_EVAL_DEVIG, FROZEN_EVAL_THRESHOLDS

    assert FROZEN_EVAL_THRESHOLDS == {"1x2": 0.010, "ou25": 0.005}
    assert FROZEN_EVAL_DEVIG == "power"


# --------------------------------------------------------------------------- #
# Live-path isolation
# --------------------------------------------------------------------------- #
def test_validation_path_does_not_touch_live_pick_minting() -> None:
    """No live-minting module may import the validation-only anchor path.
    The exporter and its matcher exist for the offline H2 run ONLY."""
    app_dir = Path(__file__).resolve().parent.parent / "app"
    offenders: list[str] = []
    for py in app_dir.rglob("*.py"):
        if py.parent.name == "backtesting":
            continue  # the validation home itself
        if "arcadia_anchor" in py.read_text(encoding="utf-8"):
            offenders.append(str(py))
    assert offenders == []
    # And the live edge/pipeline modules specifically stay clean.
    for live in ("pipeline.py", "edge/value.py", "scheduler.py"):
        text = (app_dir / live).read_text(encoding="utf-8")
        assert "arcadia_anchor" not in text
