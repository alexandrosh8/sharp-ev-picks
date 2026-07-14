"""API surface: health endpoint and payload validation (no DB required)."""

import asyncio
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_session
from app.api.routes import router


async def _no_session() -> AsyncIterator[None]:
    yield None


def make_app() -> FastAPI:
    # Router only — lifespan (DB/scheduler) intentionally not started; the
    # session dependency is stubbed so validation paths can be exercised.
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = _no_session
    from app.api.auth import require_dashboard_auth

    app.dependency_overrides[require_dashboard_auth] = lambda: None
    return app


@pytest.fixture(autouse=True)
def _health_detail_visible(monkeypatch: pytest.MonkeyPatch) -> None:
    # make_app() bypasses require_dashboard_auth, but /health checks
    # is_authenticated() inline, which reads the HOST .env — on a prod host
    # (DASHBOARD_AUTH_ENABLED=true) the body is redacted and the detail
    # assertions below would fail while passing in CI. Pin the same bypass so
    # this module is deterministic everywhere; the redaction contract itself
    # is covered in tests/test_ops_security.py.
    from app.api import routes

    monkeypatch.setattr(routes, "is_authenticated", lambda request: True)


def test_health_reports_picks_only_mode() -> None:
    from app.pipeline import LAST_POLL

    LAST_POLL.clear()  # no recorded cycles -> no evidence of a dead engine
    client = TestClient(make_app())
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["mode"] == "picks-only"


def test_live_is_process_only_even_when_poll_is_stale() -> None:
    from app.pipeline import LAST_POLL

    LAST_POLL.clear()
    LAST_POLL["soccer"] = {"finished_at": "2026-01-01T00:00:00+00:00"}
    try:
        response = TestClient(make_app()).get("/live")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "mode": "picks-only"}
    finally:
        LAST_POLL.clear()


def test_router_only_ready_fails_closed_without_dependencies() -> None:
    response = TestClient(make_app()).get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"] == {
        "exposure_seeded": False,
        "scheduler": False,
        "database": False,
        "redis": False,
        "polls": True,
    }


def test_ready_dependency_probes_are_ttl_cached() -> None:
    from app.api import routes
    from app.pipeline import LAST_POLL

    class Session:
        def __init__(self, calls: dict[str, int]) -> None:
            self.calls = calls

        async def __aenter__(self) -> "Session":
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        async def execute(self, statement):  # type: ignore[no-untyped-def]
            self.calls["db"] += 1

    class Factory:
        def __init__(self, calls: dict[str, int]) -> None:
            self.calls = calls

        def __call__(self) -> Session:
            return Session(self.calls)

    class Redis:
        def __init__(self, calls: dict[str, int]) -> None:
            self.calls = calls

        async def ping(self) -> bool:
            self.calls["redis"] += 1
            return True

    routes._READINESS_CACHE.clear()
    routes._READINESS_LOCKS.clear()
    LAST_POLL.clear()
    calls = {"db": 0, "redis": 0}
    app = make_app()
    app.state.exposure_seeded = True
    app.state.scheduler = SimpleNamespace(running=True)
    app.state.expected_poll_sports = ()
    app.state.session_factory = Factory(calls)
    app.state.redis = Redis(calls)
    client = TestClient(app)

    assert client.get("/ready").status_code == 200
    assert client.get("/ready").status_code == 200
    assert calls == {"db": 1, "redis": 1}


def test_health_degraded_when_polls_stale() -> None:
    # P0-3: a recorded cycle whose newest finish is far older than N*poll_interval
    # means the engine is starved/dead -> 503 + status:"degraded" + the stale age,
    # while KEEPING the existing payload (dashboard contract).
    from app.pipeline import LAST_POLL

    LAST_POLL.clear()
    LAST_POLL["soccer"] = {
        "finished_at": "2026-06-01T00:00:00+00:00",  # weeks old
        "snapshots": 0,
        "picks": 0,
        "matches_found": 0,
        "per_market": {},
    }
    try:
        resp = TestClient(make_app()).get("/health")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["mode"] == "picks-only"  # existing fields preserved
        assert body["newest_poll_age_seconds"] > 0
    finally:
        LAST_POLL.clear()


def test_health_ok_when_polls_fresh() -> None:
    # A cycle that finished moments ago is healthy regardless of pick count
    # (a quiet slate that still completes cycles must read 200/"ok").
    from app.pipeline import LAST_POLL

    LAST_POLL.clear()
    LAST_POLL["soccer"] = {
        "finished_at": datetime.now(tz=UTC).isoformat(),
        "snapshots": 12,
        "picks": 0,  # quiet slate, engine alive
        "matches_found": 6,
        "per_market": {},
    }
    try:
        resp = TestClient(make_app()).get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["newest_poll_age_seconds"] is not None
    finally:
        LAST_POLL.clear()


def test_poll_health_uses_full_sequential_sweep_budget() -> None:
    from app.api.routes import _poll_freshness_ceiling, _poll_health

    now = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    assert _poll_freshness_ceiling(300, 4, 900) == 3600.0
    polls = {
        sport: {"finished_at": (now - timedelta(seconds=3500)).isoformat()}
        for sport in ("soccer", "basketball", "tennis", "american_football")
    }
    assert _poll_health(
        polls,
        now,
        300,
        expected_sport_count=4,
        cycle_timeout_seconds=900,
    )[:2] == ("ok", 200)
    polls["soccer"]["finished_at"] = (now - timedelta(seconds=3700)).isoformat()
    assert _poll_health(
        polls,
        now,
        300,
        expected_sport_count=4,
        cycle_timeout_seconds=900,
    )[:2] == ("degraded", 503)


def test_recent_in_progress_heartbeat_supersedes_old_finish() -> None:
    from app.api.routes import _poll_health

    now = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    poll = {
        "finished_at": (now - timedelta(hours=2)).isoformat(),
        "started_at": (now - timedelta(minutes=10)).isoformat(),
        "in_progress": True,
        "state": "in_progress",
        "degraded": False,
    }
    assert _poll_health(
        {"soccer": poll},
        now,
        300,
        expected_sport_count=4,
        cycle_timeout_seconds=900,
    )[:2] == ("ok", 200)


def test_over_budget_or_failed_heartbeat_degrades() -> None:
    from app.api.routes import _poll_health

    now = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    over_budget = {
        "started_at": (now - timedelta(seconds=901)).isoformat(),
        "in_progress": True,
        "state": "in_progress",
    }
    failed = {
        "finished_at": now.isoformat(),
        "in_progress": False,
        "state": "failed",
        "degraded": True,
    }
    for poll in (over_budget, failed):
        assert _poll_health(
            {"soccer": poll},
            now,
            300,
            expected_sport_count=4,
            cycle_timeout_seconds=900,
        )[:2] == ("degraded", 503)


def test_health_exposes_full_sweep_poll_ceiling() -> None:
    from app.pipeline import LAST_POLL

    LAST_POLL.clear()
    LAST_POLL["soccer"] = {"finished_at": datetime.now(tz=UTC).isoformat()}
    app = make_app()
    app.state.expected_poll_sports = (
        "soccer",
        "basketball",
        "tennis",
        "american_football",
    )
    try:
        body = TestClient(app).get("/health").json()
        assert body["poll_max_age_seconds"] == 3600.0
    finally:
        LAST_POLL.clear()


def test_one_fresh_poll_cannot_mask_another_stale_poll() -> None:
    from app.pipeline import LAST_POLL

    now = datetime.now(tz=UTC)
    LAST_POLL.clear()
    LAST_POLL["soccer"] = {"finished_at": now.isoformat()}
    LAST_POLL["basketball"] = {"finished_at": (now - timedelta(days=1)).isoformat()}
    try:
        response = TestClient(make_app()).get("/health")
        assert response.status_code == 503
        assert response.json()["status"] == "degraded"
    finally:
        LAST_POLL.clear()


@pytest.mark.parametrize("finished_at", [None, "not-a-date", "2026-07-13T12:00:00"])
def test_invalid_or_naive_poll_finish_degrades_without_crashing(finished_at: object) -> None:
    from app.pipeline import LAST_POLL

    LAST_POLL.clear()
    LAST_POLL["soccer"] = {"finished_at": finished_at}
    try:
        response = TestClient(make_app()).get("/health")
        assert response.status_code == 503
        assert response.json()["status"] == "degraded"
    finally:
        LAST_POLL.clear()


def test_explicit_degraded_cycle_is_unhealthy_even_when_fresh() -> None:
    from app.pipeline import LAST_POLL

    LAST_POLL.clear()
    LAST_POLL["soccer"] = {
        "finished_at": datetime.now(tz=UTC).isoformat(),
        "degraded": True,
    }
    try:
        assert TestClient(make_app()).get("/health").status_code == 503
    finally:
        LAST_POLL.clear()


def test_stale_starvation_cycle_is_unhealthy_and_next_healthy_cycle_recovers() -> None:
    from app.pipeline import LAST_POLL

    now = datetime.now(tz=UTC).isoformat()
    LAST_POLL.clear()
    try:
        LAST_POLL["soccer"] = {
            "finished_at": now,
            "degraded": True,
            "stale_candidates": 107,
            "stale_drop_ratio": 0.87,
            "stale_drop_ratio_warn_threshold": 0.5,
            "degradation_reasons": ["stale_drop_ratio"],
        }
        response = TestClient(make_app()).get("/health")
        assert response.status_code == 503
        assert response.json()["polls"]["soccer"]["degradation_reasons"] == ["stale_drop_ratio"]

        LAST_POLL["soccer"] = {
            "finished_at": datetime.now(tz=UTC).isoformat(),
            "degraded": False,
            "stale_candidates": 0,
            "stale_drop_ratio": 0.0,
            "stale_drop_ratio_warn_threshold": 0.5,
            "degradation_reasons": [],
        }
        assert TestClient(make_app()).get("/health").status_code == 200
    finally:
        LAST_POLL.clear()


def test_health_exposes_poll_liveness_payload() -> None:
    # The dashboard renders a degraded state (selector break / anti-bot wall:
    # matches listed, zero odds parsed) straight from the polls payload —
    # per-market counts, listing count and the explicit flag must pass through.
    from app.pipeline import LAST_POLL

    LAST_POLL["soccer"] = {
        "finished_at": "2026-06-11T00:00:00+00:00",
        "snapshots": 0,
        "picks": 0,
        "matches_found": 7,
        "per_market": {},
        "degraded": True,
    }
    try:
        body = TestClient(make_app()).get("/health").json()
        poll = body["polls"]["soccer"]
        assert poll["degraded"] is True
        assert poll["matches_found"] == 7
        assert poll["per_market"] == {}
    finally:
        LAST_POLL.pop("soccer", None)


def test_health_exposes_poll_interval_seconds() -> None:
    # The dashboard's "verified within" window must track the configured poll
    # cadence (max(45min, 3 * poll_interval)) instead of hardcoding 45 min —
    # so the cadence has to ride in the health payload.
    body = TestClient(make_app()).get("/health").json()
    assert isinstance(body["poll_interval_seconds"], int)
    assert body["poll_interval_seconds"] >= 30  # Settings enforces the floor
    # The dashboard "verified/fresh" window now tracks the value-freshness window
    # (MAX_ODDS_AGE_SECONDS) so a stale-priced pick reads UNVERIFIED (audit 2026-06-26).
    assert isinstance(body["max_odds_age_seconds"], (int, float))
    assert body["max_odds_age_seconds"] > 0


def test_health_exposes_tier_edge_floors() -> None:
    # dash-2 / EEV-1: the dashboard colours edges/verdicts against the tier's
    # edge FLOOR, not a hardcoded 3%. The per-row payload carries `edge_floor`,
    # but /health is the global fallback (and the volume tier's lower floor).
    body = TestClient(make_app()).get("/health").json()
    assert isinstance(body["value_min_edge"], (int, float))
    assert isinstance(body["value_volume_min_edge"], (int, float))
    # volume tier is permitted a lower (or equal) floor than premium
    assert body["value_volume_min_edge"] <= body["value_min_edge"]


def test_health_exposes_resolver_quarantine_counters() -> None:
    # Monitor-only operator visibility: process-lifetime counts of the two
    # fail-closed pinnacle-close refusal guards (league-marker veto and
    # same-pair ambiguity), stamped with the process start they count from
    # ("since") so the operator can rate the refusal volume without log greps.
    body = TestClient(make_app()).get("/health").json()
    rq = body["resolver_quarantine"]
    assert isinstance(rq["marker_veto"], int)
    assert isinstance(rq["same_pair_ambiguity"], int)
    since = datetime.fromisoformat(rq["since"])
    assert since.tzinfo is not None  # UTC-aware process-start stamp
    assert since <= datetime.now(tz=UTC)


def test_picks_serializer_includes_tier_aware_edge_floor(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Each /picks row carries `edge_floor` = the tier's minimum edge (premium
    vs volume), so the dashboard can colour/verdict each row against its OWN
    floor instead of a hardcoded 3% (dash-2 / EEV-1). The repo computes it from
    the thresholds the route passes; here we exercise the real repo with a fake
    ORM result set."""
    from app.api import routes
    from app.storage import repositories

    captured: dict[str, float | None] = {}

    async def fake_rows(session, limit, tier=None, min_edge=None, volume_min_edge=None):  # type: ignore[no-untyped-def]
        captured["min_edge"] = min_edge
        captured["volume_min_edge"] = volume_min_edge
        # The route passes the real thresholds; mirror the repo's tier-aware
        # choice so the serialized contract is asserted end-to-end.
        return [
            {
                **_pick_row(tier="premium"),
                "edge_floor": str(min_edge),
            },
            {
                **_pick_row(id=2, tier="volume"),
                "edge_floor": str(volume_min_edge),
            },
        ]

    monkeypatch.setattr(routes, "latest_picks_with_events", fake_rows)
    body = TestClient(make_app()).get("/picks").json()
    assert captured["min_edge"] is not None and captured["volume_min_edge"] is not None
    assert body[0]["edge_floor"] == str(captured["min_edge"])
    assert body[1]["edge_floor"] == str(captured["volume_min_edge"])
    # and the real repo builds the same field (guards against the column being
    # dropped from the SELECT/serializer)
    assert "edge_floor" in repositories.latest_picks_with_events.__doc__  # type: ignore[operator]


def test_picks_tier_param_is_validated() -> None:
    # tier scopes the feed server-side (premium|volume); anything else must
    # 422 before the handler ever touches the DB.
    client = TestClient(make_app())
    assert client.get("/picks?tier=bogus").status_code == 422
    assert client.get("/picks?tier=").status_code == 422


def test_games_endpoint_serves_unrestricted_latest_fixture_view() -> None:
    from app.pipeline import AVAILABLE_GAMES

    saved = dict(AVAILABLE_GAMES)
    AVAILABLE_GAMES.clear()
    AVAILABLE_GAMES["soccer"] = [
        {
            "sport": "soccer",
            "sport_label": "Football",
            "event_id": "evt-football",
            "event": "Home FC vs Away FC",
            "home": "Home FC",
            "away": "Away FC",
            "league": "EPL",
            "starts_at": "2026-06-16T18:00:00+00:00",
            "market_count": 1,
            "markets": ["1x2"],
            "bookmaker_count": 3,
            "bookmakers": ["A", "B", "C"],
            "snapshot_count": 9,
            "first_captured_at": "2026-06-16T10:00:00+00:00",
            "last_captured_at": "2026-06-16T10:01:00+00:00",
            "updated_at": "2026-06-16T10:02:00+00:00",
        }
    ]
    AVAILABLE_GAMES["basketball"] = [
        {
            "sport": "basketball",
            "sport_label": "NBA",
            "event_id": "evt-nba",
            "event": "Home Hoops vs Away Hoops",
            "home": "Home Hoops",
            "away": "Away Hoops",
            "league": "NBA",
            "starts_at": "2026-06-16T20:00:00+00:00",
            "market_count": 0,
            "markets": [],
            "bookmaker_count": 0,
            "bookmakers": [],
            "snapshot_count": 0,
            "first_captured_at": None,
            "last_captured_at": None,
            "updated_at": "2026-06-16T10:02:00+00:00",
        }
    ]
    try:
        client = TestClient(make_app())
        all_rows = client.get("/games").json()
        assert [row["event_id"] for row in all_rows] == ["evt-football", "evt-nba"]
        nba_rows = client.get("/games?sport=basketball").json()
        assert len(nba_rows) == 1
        assert nba_rows[0]["event"] == "Home Hoops vs Away Hoops"
        assert nba_rows[0]["snapshot_count"] == 0
        # Tennis is a visibility-only sport (OFF by default) but DOES surface in
        # /games when enabled, so the sport filter must accept it (200, empty
        # here) rather than 422 - otherwise the view shows tennis unfiltered
        # but rejects filtering to it.
        tennis_resp = client.get("/games?sport=tennis")
        assert tennis_resp.status_code == 200
        assert tennis_resp.json() == []
        football_resp = client.get("/games?sport=american_football")
        assert football_resp.status_code == 200
        assert football_resp.json() == []
    finally:
        AVAILABLE_GAMES.clear()
        AVAILABLE_GAMES.update(saved)


def test_games_endpoint_falls_back_to_warehouse_when_poll_registry_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import routes
    from app.pipeline import AVAILABLE_GAMES

    saved = dict(AVAILABLE_GAMES)
    AVAILABLE_GAMES.clear()
    fake_session = object()
    calls: list[tuple[int, str | None]] = []

    class FakeSessionFactory:
        def __call__(self) -> "FakeSessionFactory":
            return self

        async def __aenter__(self) -> object:
            return fake_session

        async def __aexit__(self, *exc: object) -> bool:
            return False

    async def fake_latest_available_games_with_events(
        session: object,
        limit: int,
        sport: str | None,
    ) -> list[dict[str, object]]:
        assert session is fake_session
        calls.append((limit, sport))
        return [
            {
                "sport": "basketball",
                "sport_label": "NBA",
                "event_id": "evt-db-nba",
                "event": "Restart Hawks vs Restart Bulls",
                "home": "Restart Hawks",
                "away": "Restart Bulls",
                "league": "NBA",
                "starts_at": "2026-06-16T20:00:00+00:00",
                "market_count": 1,
                "markets": ["h2h"],
                "bookmaker_count": 2,
                "bookmakers": ["A", "B"],
                "snapshot_count": 6,
                "first_captured_at": "2026-06-16T10:00:00+00:00",
                "last_captured_at": "2026-06-16T10:01:00+00:00",
                "updated_at": "2026-06-16T10:02:00+00:00",
            }
        ]

    monkeypatch.setattr(
        routes,
        "latest_available_games_with_events",
        fake_latest_available_games_with_events,
    )
    app = make_app()
    app.state.session_factory = FakeSessionFactory()
    try:
        body = TestClient(app).get("/games?sport=basketball").json()
    finally:
        AVAILABLE_GAMES.clear()
        AVAILABLE_GAMES.update(saved)

    assert calls == [(1000, "basketball")]
    assert body[0]["event"] == "Restart Hawks vs Restart Bulls"
    assert body[0]["snapshot_count"] == 6


def test_games_endpoint_merges_partial_registry_with_warehouse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One completed poll must not hide durable rows for pending sports."""
    from app.api import routes
    from app.pipeline import AVAILABLE_GAMES

    saved = dict(AVAILABLE_GAMES)
    live_soccer = {
        "sport": "soccer",
        "sport_label": "Football",
        "event_id": "evt-shared",
        "event": "Fresh Home vs Fresh Away",
        "home": "Fresh Home",
        "away": "Fresh Away",
        "league": "Live",
        "starts_at": "2026-06-16T18:00:00+00:00",
        "market_count": 2,
        "markets": ["h2h", "totals"],
        "bookmaker_count": 2,
        "bookmakers": ["A", "B"],
        "snapshot_count": 12,
        "first_captured_at": None,
        "last_captured_at": None,
        "updated_at": "2026-06-16T12:00:00+00:00",
    }
    AVAILABLE_GAMES.clear()
    AVAILABLE_GAMES["soccer"] = [live_soccer]

    class FakeSessionFactory:
        def __call__(self) -> "FakeSessionFactory":
            return self

        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *exc: object) -> bool:
            return False

    async def fake_latest_available_games_with_events(
        session: object,
        limit: int,
        sport: str | None,
    ) -> list[dict[str, object]]:
        assert limit == 1000
        assert sport is None
        return [
            {**live_soccer, "event": "Stale Home vs Stale Away", "snapshot_count": 1},
            {
                **live_soccer,
                "sport": "basketball",
                "sport_label": "NBA",
                "event_id": "evt-db-basketball",
                "event": "Durable Hawks vs Durable Bulls",
                "starts_at": "2026-06-16T20:00:00+00:00",
            },
        ]

    monkeypatch.setattr(
        routes,
        "latest_available_games_with_events",
        fake_latest_available_games_with_events,
    )
    app = make_app()
    app.state.session_factory = FakeSessionFactory()
    try:
        rows = TestClient(app).get("/games").json()
    finally:
        AVAILABLE_GAMES.clear()
        AVAILABLE_GAMES.update(saved)

    by_id = {row["event_id"]: row for row in rows}
    assert set(by_id) == {"evt-shared", "evt-db-basketball"}
    assert by_id["evt-shared"]["event"] == "Fresh Home vs Fresh Away"
    assert by_id["evt-shared"]["snapshot_count"] == 12
    assert by_id["evt-db-basketball"]["event"] == "Durable Hawks vs Durable Bulls"


def test_picks_payload_carries_reason_summary(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Forward-evidence contract: the dashboard's shadow-gate indicator parses
    each pick's `reason_summary` (visibility-only / steam(shadow) notes ride on
    it), so the /picks payload must carry the field through the confidence-
    enrichment step untouched."""
    import app.api.routes as routes

    async def fake_rows(session, limit, tier=None, min_edge=None, volume_min_edge=None):  # type: ignore[no-untyped-def]
        return [
            _pick_row(
                tier="volume",
                market="spreads",
                reason_summary=(
                    "value: Pinnacle fair 1.95 vs BookA 2.10 | "
                    "visibility-only market: capped at volume (shadow) | "
                    "steam(shadow) (soft_converging): would demote"
                ),
            )
        ]

    monkeypatch.setattr(routes, "latest_picks_with_events", fake_rows)
    body = TestClient(make_app()).get("/picks").json()
    assert "reason_summary" in body[0]
    assert "visibility-only" in body[0]["reason_summary"]
    assert "steam(shadow)" in body[0]["reason_summary"]


def test_performance_payload_includes_live_evidence(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """GET /performance carries the stratified live-evidence section — the
    instrument for the VALUE_ML_FILTER flip. DB reads are stubbed at the
    route's own imports; the pure report runs for real, so the honest-n
    contract (sufficient=false under min_n CLV rows) is asserted end-to-end."""
    from app.api import routes
    from app.backtesting.live_evidence import SettledPickRow

    async def fake_perf(session, *, close_coverage_sla=0.85):  # type: ignore[no-untyped-def]
        return {
            "n_settled": 2,
            "tier_scope": "premium",
            # The sharp-subset progress fields the PROOF-OF-EDGE hero needs to
            # render its INSUFFICIENT-EVIDENCE state ("n / min — accruing").
            "n_sharp_close": 0,
            "min_headline_n": 50,
            "sharp_status": "insufficient",
        }

    async def fake_rows(session):  # type: ignore[no-untyped-def]
        # close_independent_of_fill=True: per-stratum CLV samples require
        # PROVEN independence (is True, 2026-07-11 alignment) — this test is
        # about the payload plumbing, not the independence guard.
        return [
            SettledPickRow("premium", 0.80, 0.02, True, 10.0, 1.0, close_independent_of_fill=True),
            SettledPickRow("volume", None, None, None, 5.0, None),
        ]

    async def fake_band(session):  # type: ignore[no-untyped-def]
        # The route also reads the claimed-fair reliability monitor's rows (P1-1);
        # stub them at the route's own import so this test stays DB-free.
        return []

    monkeypatch.setattr(routes, "performance_report", fake_perf)
    monkeypatch.setattr(routes, "live_evidence_rows", fake_rows)
    monkeypatch.setattr(routes, "bet_band_observations", fake_band)
    monkeypatch.setattr(routes, "_ml_operating_point", lambda: 0.725)

    body = TestClient(make_app()).get("/performance").json()
    assert body["tier_scope"] == "premium"  # headline scope untouched
    # PROOF-OF-EDGE accruing state: the route must pass the sharp-subset progress
    # fields through untouched so the hero can render "0 / 50 — accruing" instead
    # of a blank "—" that reads as broken.
    assert body["n_sharp_close"] == 0
    assert body["min_headline_n"] == 50
    assert body["sharp_status"] == "insufficient"
    ev = body["live_evidence"]
    assert ev["q_star"] == 0.725
    assert ev["min_n"] == 50
    assert ev["by_score"]["score_ge_q"]["n"] == 1
    assert ev["by_score"]["unscored"]["n"] == 1
    assert ev["by_tier"]["premium"]["n_clv"] == 1
    # 1 CLV row < 50: the stratum is explicitly insufficient — the dashboard
    # must render the state, never a lone point estimate. Estimates are
    # nulled AT THE SOURCE so any other /performance consumer sees no
    # noise-level numbers either.
    assert ev["by_tier"]["premium"]["sufficient"] is False
    assert ev["by_tier"]["premium"]["mean_clv_log"] is None
    assert ev["by_tier"]["premium"]["roi"] is None
    # anchor dimension feature-detected: absent until the column lands
    assert ev["by_anchor"] is None
    # P1-1 claimed-fair reliability monitor rides alongside (report-only); with
    # no settled binary picks it is honestly empty + insufficient, never a crash.
    cal = body["calibration"]
    assert cal["n_total"] == 0
    assert cal["insufficient"] is True
    assert cal["ece"] is None


def test_resolution_match_rate_endpoint_serializes_report(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """GET /resolution/match-rate serializes the strict shadow match-rate report
    (overall rate + coverage/alias diagnostic buckets). The DB read is stubbed
    at the route's own import; the pure summarizer runs for real."""
    from app.api import routes
    from app.resolution.shadow import ShadowOutcome

    async def fake_outcomes(session, *, since=None):  # type: ignore[no-untyped-def]
        return [
            ShadowOutcome(
                pick_id=1, sport="soccer", league="soccer_epl", candidates_in_window=1, matched=True
            ),
            ShadowOutcome(
                pick_id=2,
                sport="soccer",
                league="soccer_epl",
                candidates_in_window=1,
                matched=False,
            ),  # alias/ambiguity gap
            ShadowOutcome(
                pick_id=3,
                sport="soccer",
                league="soccer_epl",
                candidates_in_window=0,
                matched=False,
            ),  # coverage gap
        ]

    async def fake_capture(session, **_kw):  # type: ignore[no-untyped-def]
        return [
            {"sport": "american_football", "captured": 4, "scraped": 0, "matched": 0},
            {"sport": "basketball", "captured": 96, "scraped": 64, "matched": 20},
            {"sport": "soccer", "captured": 218, "scraped": 149, "matched": 50},
            {"sport": "tennis", "captured": 60, "scraped": 6, "matched": 5},
        ]

    async def fake_betfair_capture(session, **_kw):  # type: ignore[no-untyped-def]
        # Near-empty archive path (separate betfair: namespace, default OFF) — it
        # feeds the per-sport panel body, NOT the headline anymore.
        return [
            {"sport": "soccer", "scraped": 149, "captured": 4},
            {"sport": "basketball", "scraped": 64, "captured": 0},
        ]

    async def fake_betfair_inline_capture(session, **_kw):  # type: ignore[no-untyped-def]
        # The REAL pick-feeding anchor: inline Betfair Exchange rows on the
        # canonical event (~66% of scraped soccer fixtures with soft odds).
        return [
            {"sport": "soccer", "scraped": 149, "captured": 99},
            {"sport": "basketball", "scraped": 64, "captured": 0},
        ]

    async def fake_link_metrics(session):  # type: ignore[no-untyped-def]
        # Shape contract of repositories.source_link_metrics (null-safe zeros
        # when the link tables are empty; per-source averages otherwise).
        return {
            "auto_linked": 2,
            "review_queued": 1,
            "rejected_observed": 1,
            "weak_links": 1,
            "by_source": {"pinnacle_arcadia": {"links": 2, "avg_confidence": 0.955}},
        }

    async def fake_close_density(session, **_kw):  # type: ignore[no-untyped-def]
        # D4 capture-density panel: final-hour sharp rows per source (Q5/Q6
        # shape) — the shape contract of repositories.sharp_close_capture_density.
        return {
            "window_days": 7,
            "final_window_minutes": 60,
            "events_kicked_off": 40,
            "sources": {
                "betfair": {"final_window_rows": 12, "events_with_rows": 9},
                "pinnacle": {"final_window_rows": 3, "events_with_rows": 2},
            },
        }

    monkeypatch.setattr(routes, "shadow_match_rate_outcomes", fake_outcomes)
    monkeypatch.setattr(routes, "pinnacle_archive_capture_by_sport", fake_capture)
    monkeypatch.setattr(routes, "betfair_archive_capture_by_sport", fake_betfair_capture)
    monkeypatch.setattr(routes, "betfair_inline_capture_by_sport", fake_betfair_inline_capture)
    monkeypatch.setattr(routes, "source_link_metrics", fake_link_metrics)
    monkeypatch.setattr(routes, "sharp_close_capture_density", fake_close_density)

    async def fake_session() -> AsyncIterator[object]:
        # Router-only mode still requires a request-scoped session. The report
        # functions above are stubbed; the two null-safe diagnostics below see
        # this inert sentinel and return their documented empty shapes.
        yield object()

    app = make_app()
    app.dependency_overrides[get_session] = fake_session
    body = TestClient(app).get("/resolution/match-rate").json()
    # Betfair STALENESS-GUARD diagnostics ride the same payload (P3). NOT
    # stubbed — the real repositories.betfair_staleness_metrics runs and must
    # be NULL-SAFE on an empty/absent verdict table: zeros + None medians,
    # never a 500. (The demote-rate instrument for the shadow->enforce review.)
    stale = body["betfair_staleness"]
    assert stale["rows"] == 0
    assert stale["decisions"] == {}
    assert stale["fresh_decisions"] == {}
    assert stale["stale_rows"] == 0
    assert stale["median_tick_diff"] is None
    assert stale["median_freshness_gap_seconds"] is None
    assert stale["ttl_seconds"] > 0
    # D4 capture-density panel rides the same payload (capture, not matching).
    density = body["close_capture_density"]
    assert density["events_kicked_off"] == 40
    assert density["sources"]["betfair"]["events_with_rows"] == 9
    assert density["sources"]["pinnacle"]["final_window_rows"] == 3
    # Cross-source link observability rides the same payload.
    assert body["links"]["auto_linked"] == 2
    assert body["links"]["review_queued"] == 1
    assert body["links"]["weak_links"] == 1
    assert body["links"]["by_source"]["pinnacle_arcadia"]["links"] == 2
    assert body["total"] == 3
    assert body["matched"] == 1
    assert body["match_rate"] == pytest.approx(1 / 3)
    assert body["no_archive_candidates"] == 1
    assert body["unmatched_with_candidates"] == 1
    sport = body["by_sport"][0]
    assert sport["key"] == "soccer"
    assert sport["total"] == 3
    assert sport["matched"] == 1
    assert sport["match_rate"] == pytest.approx(1 / 3)
    # archive_capture lists ALL arcadia sports — tennis + american_football too,
    # not just the pick sports that surface in the match rate above.
    cap = {row["sport"]: row for row in body["archive_capture"]}
    assert set(cap) == {"soccer", "basketball", "tennis", "american_football"}
    assert cap["tennis"]["captured"] == 60
    # tennis mints no picks yet carries a fixture-level close-match count, so the
    # panel can show coverage instead of an empty cell.
    assert cap["tennis"]["matched"] == 5
    assert cap["american_football"]["scraped"] == 0
    # The per-sport panel AND the headline both now come from the INLINE coverage
    # (the real pick-feeding anchor: Betfair Exchange bound onto the canonical event),
    # NOT the near-empty archive path — the panel matches the headline instrument.
    bf_panel = {row["sport"]: row for row in body["betfair_capture"]}
    assert bf_panel["soccer"]["captured"] == 99  # panel = INLINE canonical coverage
    bf_inline = {row["sport"]: row for row in body["betfair_inline_capture"]}
    assert bf_inline["soccer"]["captured"] == 99  # inline canonical-event coverage
    # The archive counter survives ONLY as a separate diagnostic, never the panel.
    bf_archive = {row["sport"]: row for row in body["betfair_archive_capture"]}
    assert bf_archive["soccer"]["captured"] == 4  # archive path stays near-empty
    # coverage_summary is the always-populated headline the panel shows BEFORE
    # the operator expands it (replaces the bare "—"). Betfair = sum(INLINE
    # captured)/sum(scraped) = 99/(149+64)=99/213; Pinnacle = sum(matched)/
    # sum(scraped) = (0+20+50+5)/(0+64+149+6)=75/219.
    cov = body["coverage_summary"]
    assert cov["betfair_captured"] == 99
    assert cov["betfair_scraped"] == 213
    assert cov["betfair_rate"] == pytest.approx(99 / 213)
    assert cov["pinnacle_matched"] == 75
    assert cov["pinnacle_scraped"] == 219
    assert cov["pinnacle_rate"] == pytest.approx(75 / 219)
    assert cov["headline"] == "Betfair 46% · Pinnacle 34%"


async def test_match_rate_singleflight_caches_after_waiter_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import routes

    started = asyncio.Event()
    release = asyncio.Event()
    report = {"total": 7, "matched": 5}

    async def fake_compute(request, session, days):  # type: ignore[no-untyped-def]
        started.set()
        await release.wait()
        return report

    monkeypatch.setattr(routes, "_compute_resolution_match_rate", fake_compute)
    routes._MATCH_RATE_CACHE.clear()
    routes._MATCH_RATE_INFLIGHT.clear()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(session_factory=object())))
    waiter = asyncio.create_task(
        routes.resolution_match_rate(request, None, 17)  # type: ignore[arg-type]
    )
    await started.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    release.set()
    for _ in range(10):
        await asyncio.sleep(0)
        if 17 in routes._MATCH_RATE_CACHE:
            break
    assert routes._MATCH_RATE_CACHE[17][1] == report
    assert 17 not in routes._MATCH_RATE_INFLIGHT


async def test_match_rate_singleflight_retrieves_detached_error_without_secret(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.api import routes

    started = asyncio.Event()
    release = asyncio.Event()
    secret_url = "https://operator:credential@example.invalid/private"

    async def fake_compute(request, session, days):  # type: ignore[no-untyped-def]
        started.set()
        await release.wait()
        raise RuntimeError(secret_url)

    monkeypatch.setattr(routes, "_compute_resolution_match_rate", fake_compute)
    routes._MATCH_RATE_CACHE.clear()
    routes._MATCH_RATE_INFLIGHT.clear()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(session_factory=object())))
    waiter = asyncio.create_task(
        routes.resolution_match_rate(request, None, 23)  # type: ignore[arg-type]
    )
    await started.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    with caplog.at_level("ERROR"):
        release.set()
        for _ in range(10):
            await asyncio.sleep(0)
            if 23 not in routes._MATCH_RATE_INFLIGHT:
                break
    assert 23 not in routes._MATCH_RATE_CACHE
    assert 23 not in routes._MATCH_RATE_INFLIGHT
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "RuntimeError" in log_text
    assert secret_url not in log_text


def test_resolution_review_queue_endpoint_serializes_rows(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """GET /resolution/review-queue serializes the newest match_review_queue rows
    (names from evidence_json, candidate kickoff, confidence, reason, review
    status). The DB read is stubbed at the route's own import; the pure
    serializer runs for real. Read-only browse — no review action exists."""
    from decimal import Decimal

    from app.api import routes
    from app.storage.models import MatchReviewQueue

    full = MatchReviewQueue(
        id=7,
        source="pinnacle_arcadia",
        source_event_id="evt-1",
        candidate_canonical_event_id=42,
        confidence_score=Decimal("0.8912"),
        reason="jw_below_accept",
        evidence_json={
            "query_base_home": "arsenal",
            "query_base_away": "chelsea",
            "candidate_base_home": "arsenal fc",
            "candidate_base_away": "chelsea fc",
            "kickoff_delta_seconds": 600.0,
        },
        review_status="pending",
    )
    full.created_at = datetime(2026, 7, 3, 12, 0, tzinfo=UTC)
    full.reviewed_at = None
    # Degenerate row: no evidence, no candidate link, already reviewed —
    # serializes as Nones (the dashboard renders '—'), never a 500.
    bare = MatchReviewQueue(
        id=6,
        source="betfair",
        source_event_id="evt-2",
        candidate_canonical_event_id=None,
        confidence_score=Decimal("0.8500"),
        reason="ambiguity_margin",
        evidence_json=None,
        review_status="approved",
    )
    bare.created_at = datetime(2026, 7, 2, 9, 30, tzinfo=UTC)
    bare.reviewed_at = datetime(2026, 7, 3, 8, 0, tzinfo=UTC)

    async def fake_rows(session, *, limit):  # type: ignore[no-untyped-def]
        assert limit == 50  # default flows through to the repository read
        return [
            (full, datetime(2026, 7, 4, 18, 30, tzinfo=UTC)),
            (bare, None),
        ]

    monkeypatch.setattr(routes, "review_queue_rows", fake_rows)
    body = TestClient(make_app()).get("/resolution/review-queue").json()
    assert body["limit"] == 50
    assert body["count"] == 2
    first, second = body["rows"]
    assert first["id"] == 7
    assert first["source"] == "pinnacle_arcadia"
    assert first["event"] == "arsenal v chelsea"
    assert first["candidate"] == "arsenal fc v chelsea fc"
    assert first["kickoff_utc"] == "2026-07-04T18:30:00+00:00"
    assert first["kickoff_delta_seconds"] == pytest.approx(600.0)
    assert first["confidence"] == pytest.approx(0.8912)
    assert first["reason"] == "jw_below_accept"
    assert first["review_status"] == "pending"
    assert first["created_at"] == "2026-07-03T12:00:00+00:00"
    assert first["reviewed_at"] is None
    assert second["event"] is None  # missing evidence -> None, dashboard shows '—'
    assert second["candidate"] is None
    assert second["kickoff_utc"] is None
    assert second["kickoff_delta_seconds"] is None
    assert second["review_status"] == "approved"
    assert second["reviewed_at"] == "2026-07-03T08:00:00+00:00"


def test_resolution_review_queue_limit_is_validated() -> None:
    """limit is bounded 1..200 — out-of-range values are a 422, never a
    table-sized read."""
    client = TestClient(make_app())
    assert client.get("/resolution/review-queue?limit=0").status_code == 422
    assert client.get("/resolution/review-queue?limit=201").status_code == 422


def test_lab_promotion_distance_endpoint_serializes(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """B1: GET /lab/promotion-distance is a thin auth-gated passthrough of the
    per-(sport, market) trusted-CLV accrual aggregate — the DB read is stubbed
    at the route import (the aggregate's min-n nulling is pinned in
    tests/test_promotion_distance.py). Sub-floor cells carry denominators and
    a None estimate — never a point estimate."""
    from app.api import routes

    payload = {
        "ok_n": 30,
        "cadence_window_days": 14,
        "note": (
            "Distance to the trusted-CLV evidence floor only — informational. "
            "Promotion stays gated by SportMarketClvGate and operator ADR sign-off."
        ),
        "cells": [
            {
                "sport": "soccer",
                "market": "h2h",
                "n_settled": 40,
                "n_trusted": 12,
                "ok_n": 30,
                "status": "accruing",
                "n_recent_trusted": 6,
                "cadence_window_days": 14,
                "est_days_to_threshold": 42.0,
                "mean_clv_log": None,
                "se_clv_log": None,
            }
        ],
    }

    async def fake_report(session):  # type: ignore[no-untyped-def]
        return payload

    monkeypatch.setattr(routes, "sport_market_promotion_distance", fake_report)
    body = TestClient(make_app()).get("/lab/promotion-distance").json()
    assert body["ok_n"] == 30
    assert "SportMarketClvGate" in body["note"]  # no promotion implication
    (cell,) = body["cells"]
    assert cell["status"] == "accruing"
    assert cell["mean_clv_log"] is None  # sub-floor estimates nulled at the source
    assert cell["est_days_to_threshold"] == 42.0


def test_resolution_match_ceiling_endpoint_serializes(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """B3: GET /resolution/match-ceiling serves the LIVE per-sport ceiling
    decomposition (structural vs addressable vs unknown-league) — the DB read
    is stubbed at the route import; assembly/classification parity is pinned
    in tests/test_match_ceiling.py."""
    from app.api import routes

    captured: dict[str, int] = {}

    async def fake_decomposition(session, *, days):  # type: ignore[no-untyped-def]
        captured["days"] = days
        return {
            "window_days": days,
            "source": "live",
            "note": "structural = no in-window pinnacle event for the event's league",
            "sports": {
                "soccer": {
                    "events": 10,
                    "matched": 4,
                    "matched_rate": 0.4,
                    "unmatched": 6,
                    "structural": 3,
                    "addressable": 2,
                    "unknown_league": 1,
                    "corrected_match_rate_lower": 4 / 7,
                    "corrected_match_rate_upper": 4 / 6,
                }
            },
        }

    monkeypatch.setattr(routes, "match_ceiling_decomposition", fake_decomposition)
    body = TestClient(make_app()).get("/resolution/match-ceiling?days=45").json()
    assert captured["days"] == 45
    assert body["source"] == "live"  # never the static research artifact
    assert body["window_days"] == 45
    soccer = body["sports"]["soccer"]
    assert soccer["structural"] == 3
    assert soccer["addressable"] == 2


def test_resolution_match_ceiling_days_is_validated() -> None:
    """days is bounded 1..365 — out-of-range values are a 422 before the
    handler ever touches the DB (default is 30)."""
    client = TestClient(make_app())
    assert client.get("/resolution/match-ceiling?days=0").status_code == 422
    assert client.get("/resolution/match-ceiling?days=366").status_code == 422


def test_dashboard_html_is_not_browser_cached() -> None:
    """The dashboard HTML shell must not be browser-cached: a deploy ships new
    structure (panels, badges, banner) but the page only reloads on a full
    refresh — the 60s auto-refresh re-fetches DATA, not the page. A cached shell
    masks the update behind a stale tab. Cache-Control: no-store forces a fresh
    shell each load."""
    res = TestClient(make_app()).get("/")
    assert res.status_code == 200
    assert "no-store" in res.headers.get("cache-control", "").lower()


def test_result_payload_validation_rejects_bad_outcome() -> None:
    client = TestClient(make_app())
    response = client.post(
        "/picks/1/result",
        json={
            "pick_id": "1",
            "outcome": "smashed_it",  # not a valid Outcome
            "settled_at": "2026-06-10T12:00:00Z",
        },
    )
    assert response.status_code == 422


def test_result_payload_validation_rejects_naive_datetime() -> None:
    client = TestClient(make_app())
    response = client.post(
        "/picks/1/result",
        json={
            "pick_id": "1",
            "outcome": "won",
            "settled_at": "2026-06-10T12:00:00",  # naive
        },
    )
    assert response.status_code == 422


@pytest.mark.parametrize("invalid_id", [0, -1, 9_223_372_036_854_775_808])
def test_manual_settlement_path_ids_are_bounded_before_database(invalid_id: int) -> None:
    client = TestClient(make_app())
    timestamp = datetime.now(tz=UTC).isoformat()
    pick_response = client.post(
        f"/picks/{invalid_id}/result",
        json={
            "pick_id": str(invalid_id),
            "outcome": "won",
            "settled_at": timestamp,
        },
    )
    event_response = client.post(
        f"/events/{invalid_id}/result",
        json={"home_score": 1, "away_score": 0},
    )
    assert pick_response.status_code == 422
    assert event_response.status_code == 422


@pytest.mark.parametrize(
    "invalid_fields",
    [
        {"pick_id": "1" * 65},
        {"actual_stake": "10000000000.00"},
        {"actual_stake": "1.001"},
        {"actual_odds": "NaN"},
        {"actual_odds": "Infinity"},
        {"actual_odds": "1000.0001"},
        {"actual_odds": "2.12345"},
        {"bookmaker_used": "b" * 65},
        {"notes": "n" * 4097},
        {"actual_stake": "10100000.00", "actual_odds": "1000.0000"},
    ],
)
def test_result_payload_rejects_values_that_cannot_fit_persistence(
    invalid_fields: dict[str, str],
) -> None:
    payload = {
        "pick_id": "1",
        "outcome": "won",
        "settled_at": "2026-06-10T12:00:00Z",
        **invalid_fields,
    }
    response = TestClient(make_app()).post("/picks/1/result", json=payload)

    assert response.status_code == 422


@pytest.mark.parametrize(
    "fill_fields",
    [
        {"bet_placed": False, "actual_stake": "10.00"},
        {"bet_placed": False, "actual_odds": "2.10"},
        {"bet_placed": False, "bookmaker_used": "Betfair"},
        {"bet_placed": True, "actual_odds": "2.10"},
        {"bet_placed": True, "bookmaker_used": "Betfair"},
        {"bet_placed": True, "actual_stake": "0.00"},
    ],
)
def test_result_payload_rejects_inconsistent_actual_fill_fields(
    fill_fields: dict[str, object],
) -> None:
    response = TestClient(make_app()).post(
        "/picks/1/result",
        json={
            "pick_id": "1",
            "outcome": "won",
            "settled_at": "2026-06-10T12:00:00Z",
            **fill_fields,
        },
    )
    assert response.status_code == 422


def test_event_result_rejects_negative_and_missing_scores() -> None:
    client = TestClient(make_app())
    assert (
        client.post("/events/1/result", json={"home_score": -1, "away_score": 0}).status_code == 422
    )
    assert client.post("/events/1/result", json={"home_score": 2}).status_code == 422
    assert (
        client.post("/events/1/result", json={"home_score": 2, "away_score": "x"}).status_code
        == 422
    )


def _pick_row(**over: object) -> dict[str, object]:
    """A minimal /picks repo row (every numeric serialized as a string)."""
    base: dict[str, object] = {
        "id": 1,
        "event_id": 10,
        "event": "Home vs Away",
        "league": "EPL",
        "starts_at": "2026-06-16T18:00:00+00:00",
        "market": "h2h",
        "selection": "Home",
        "bookmaker": "BookA",
        "decimal_odds": "2.00",
        "model_probability": "0.55",
        "fair_probability": "0.52",
        "edge": "0.03",
        "ev": "0.05",
        "confidence": "0.9",
        "recommended_stake_fraction": "0.012",
        "recommended_stake_amount": "1.20",
        "reason_summary": "value vs sharp",
        "status": "alerted",
        "tier": "premium",
        "value_filter_score": None,
        "anchor_type": "consensus",
        "created_at": "2026-06-16T10:00:00+00:00",
        "clv_log": None,
        "beat_close": None,
        "current_odds": None,
        "current_edge": None,
        "revalidated_at": None,
        "min_acceptable_odds": "1.74",
    }
    base.update(over)
    return base


def test_banner_fair_odds_reconciles_to_one_fair() -> None:
    """FIX 5: the banner's Fair odds derives from the SAME fair as Edge and
    Min-acceptable — closing_fair_probability if present, else model_probability
    — NEVER fair_probability (which equals offered on value picks)."""
    from app.storage.repositories import banner_fair_odds

    # value pick: model_probability 0.62 (sharp fair) != 1/1.67 (offered implied).
    # No re-price yet -> fair odds from model_probability = 1/0.62 = 1.61, NOT 1.67.
    assert banner_fair_odds(None, 0.62) == f"{1.0 / 0.62:.2f}"
    assert banner_fair_odds(None, 0.62) != "1.67"
    # once a re-price exists, the live closing fair wins (0.50 -> 2.00).
    assert banner_fair_odds(0.50, 0.62) == "2.00"
    # degenerate stored prob -> None (no honest fair odds).
    assert banner_fair_odds(None, None) is None
    assert banner_fair_odds(None, 0.0) is None
    assert banner_fair_odds(None, 1.0) is None


def test_picks_serializer_stamps_structural_sane(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """FIX 1 display defense-in-depth: the /picks serializer stamps
    ``structural_sane`` per row so a stored-impossible pick can never render
    star-rated. Offered below its own min-acceptable floor => False; a normal
    self-consistent row => True."""
    from app.api import routes

    rows = [
        # offered 1.67 BELOW the floor recomputed from its OWN entry fair
        # (0.60 @ edge_floor 0.03 -> 1/(0.60-0.03) = 1.754): the reported
        # "MIN ACCEPTABLE > OFFERED" totals row, judged entry-vs-entry.
        _pick_row(decimal_odds="1.67", model_probability="0.60", edge_floor="0.03", edge="0.50"),
        # inverted pair: fair (1/0.60 = 1.667) at/above offered 1.60, edge>0
        _pick_row(decimal_odds="1.60", model_probability="0.60", edge="0.05"),
        # normal self-consistent premium row (entry floor 1/(0.55-0.03) = 1.923 <= 2.00)
        _pick_row(
            decimal_odds="2.00",
            model_probability="0.55",
            edge_floor="0.03",
            min_acceptable_odds="1.74",
        ),
        # AUDIT 2026-07-10 regression: the market moved TOWARD the pick, so the
        # LIVE-fair min_acceptable_odds (2.20) now exceeds the ENTRY price
        # (2.00). That is a GOOD (positive-CLV) pick, not a structural
        # impossibility — the live floor must NOT be compared to the entry
        # price. Entry basis is self-consistent (floor 1.923 <= 2.00).
        _pick_row(
            decimal_odds="2.00",
            model_probability="0.55",
            edge_floor="0.03",
            min_acceptable_odds="2.20",
            current_odds="1.80",
            current_edge="0.02",
        ),
    ]

    async def fake_rows(session, limit, tier=None, min_edge=0.0, volume_min_edge=0.0):  # type: ignore[no-untyped-def]
        return [dict(r) for r in rows]

    monkeypatch.setattr(routes, "latest_picks_with_events", fake_rows)
    body = TestClient(make_app()).get("/picks").json()

    assert body[0]["structural_sane"] is False  # entry price < entry-fair floor
    assert body[1]["structural_sane"] is False  # inverted fair/offered pair
    assert body[2]["structural_sane"] is True  # self-consistent
    assert body[3]["structural_sane"] is True  # market moved toward pick: sane


def test_picks_serializer_attaches_confidence_rating(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The /picks route enriches each row with a 1..5 star confidence block
    computed from existing fields — the dashboard headline that replaces the
    recommended stake. The repository layer is stubbed; the pure rating runs
    for real, so the band formula is asserted end-to-end."""
    from app.api import routes

    rows = [
        # bare-minimum premium: edge==floor, consensus, no ML -> 2 stars
        _pick_row(edge="0.03", anchor_type="consensus", value_filter_score=None),
        # strong edge + pinnacle + ML>=q* -> 5 stars; live edge preferred
        _pick_row(
            current_edge="0.07",
            edge="0.03",
            anchor_type="pinnacle",
            value_filter_score="0.80",
        ),
    ]

    async def fake_rows(session, limit, tier=None, min_edge=0.0, volume_min_edge=0.0):  # type: ignore[no-untyped-def]
        return [dict(r) for r in rows]

    monkeypatch.setattr(routes, "latest_picks_with_events", fake_rows)
    body = TestClient(make_app()).get("/picks").json()

    assert body[0]["confidence_rating"]["level"] == 2
    assert body[0]["confidence_rating"]["label"] == "low"
    assert body[1]["confidence_rating"]["level"] == 5
    assert body[1]["confidence_rating"]["label"] == "very high"
    # "why this rating" reasons ride along for the tooltip
    assert any("pinnacle" in r for r in body[1]["confidence_rating"]["reasons"])
    # the stake figures stay on the row (moved to a tooltip, never dropped)
    assert body[0]["recommended_stake_fraction"] == "0.012"
    assert body[0]["recommended_stake_amount"] == "1.20"


def test_picks_serializer_exposes_closing_odds() -> None:
    """The GET /picks payload carries closing_odds so the dashboard can show the
    closing price. It is null until a pick settles; the dashboard falls back to
    the frozen current_odds for kicked-off-but-unsettled picks."""
    import inspect

    from app.storage import repositories

    src = inspect.getsource(repositories.latest_picks_with_events)
    assert '"closing_odds"' in src
    assert "p.closing_odds" in src


def test_picks_serializer_exposes_close_independence_flag() -> None:
    """CLV-1: the GET /picks payload carries a per-row close_independent_of_fill
    flag so the dashboard's per-pick CLV tile can mark whether the pick's CLV came
    from a genuine, independent close (True) or a circular self-priced one (False;
    None = unknown / pre-column)."""
    import inspect

    from app.storage import repositories

    src = inspect.getsource(repositories.latest_picks_with_events)
    assert '"close_independent_of_fill"' in src
    assert "p.close_independent_of_fill" in src


def test_health_includes_redacted_proxy_pool() -> None:
    """Operator 2026-07-03 item 5: the proxy tile reads /health (eager, every
    cycle, no DB) — /resolution/match-rate is slow enough on live to hit the
    dashboard's fetch abort, which left the tile permanently '—'. The payload
    is the same REDACTED registry diagnostics (indices/counters/class names;
    never a URL, IP, or credential)."""
    body = TestClient(make_app()).get("/health").json()
    pool = body["proxy_pool"]
    for key in (
        "configured",
        "healthy",
        "quarantined",
        "dead",
        "failovers_15m",
        "failovers_1h",
        "verdict",
        "dominant_failure_class",
    ):
        assert key in pool
    assert pool["verdict"] in ("Proxy pool healthy", "Proxy pool degraded")
    # no secrets: the whole blob must not smell like a URL or credential
    import json as _json

    blob = _json.dumps(pool)
    assert "http" not in blob
    assert "@" not in blob


def test_dashboard_avoids_promotional_language() -> None:
    """Banned-language regression: no gambling-hype vocabulary anywhere in the
    served dashboard. 'lock/locked' (as a word), 'sure bet', 'easy money',
    'risk-free' and 'guaranteed' must never appear; the word 'guarantee' may
    appear ONLY inside the honest negations ('not a profit guarantee' /
    'never a profit guarantee')."""
    text = TestClient(make_app()).get("/").text
    low = text.lower()
    for banned in ("sure bet", "easy money", "risk-free", "guaranteed"):
        assert banned not in low, banned
    # 'lock' as a standalone word (lock/locks/locked/locking) is banned;
    # identifiers like toggleBlock/.sub-block do not count.
    assert re.search(r"\block(?:ed|s|ing)?\b", low) is None
    # every 'guarantee' occurrence is an explicit negation. JS string
    # concatenation may split the phrase ("never a " + "profit guarantee"),
    # so normalise the quote-plus-quote seams before checking.
    joined = re.sub(r'"\s*\+\s*"', "", low)
    for m in re.finditer("guarante", joined):
        ctx = joined[max(0, m.start() - 40) : m.start()]
        assert "not a profit" in ctx or "never a profit" in ctx, ctx


def test_login_page_uses_sharp_ev_picks_wordmark() -> None:
    """Login branding matches the README/dashboard wordmark 'sharp-ev-picks';
    the legacy 'SignalDesk' name is gone from the login page."""
    from app.api.routes import _LOGIN_HTML

    assert "sharp-ev-picks" in _LOGIN_HTML
    assert "SignalDesk" not in _LOGIN_HTML
    assert "<title>sharp-ev-picks — sign in</title>" in _LOGIN_HTML


def test_login_page_hardened_against_double_submit() -> None:
    """_LOGIN_HTML only (no auth-logic change): both fields are required with
    proper autocomplete tokens, and the submit button disables on first submit
    (re-enabled only on failure so a wrong password can be retried; success
    navigates away)."""
    from app.api.routes import _LOGIN_HTML

    assert 'autocomplete="username"' in _LOGIN_HTML
    assert 'autocomplete="current-password"' in _LOGIN_HTML
    # required on BOTH inputs
    assert _LOGIN_HTML.count("required />") == 2
    # disable-on-submit guard markers
    assert 'id="login-submit"' in _LOGIN_HTML
    assert "if (submitBtn.disabled) return;" in _LOGIN_HTML
    assert "submitBtn.disabled = true;" in _LOGIN_HTML
    # failure paths re-enable so the user can retry after a 401
    assert _LOGIN_HTML.count("submitBtn.disabled = false;") == 1
    # error text still set via textContent (never innerHTML)
    assert "innerHTML" not in _LOGIN_HTML
