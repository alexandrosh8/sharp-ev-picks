"""API surface: health endpoint and payload validation (no DB required)."""

import re
from collections.abc import AsyncIterator

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


def test_health_reports_picks_only_mode() -> None:
    client = TestClient(make_app())
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["mode"] == "picks-only"


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


def test_dashboard_served_at_root() -> None:
    client = TestClient(make_app())
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    # safety reminder must be visible on the dashboard
    assert "not</b> place bets" in response.text
    assert 'id="picks-table"' in response.text
    # untrusted scrape strings must never go through innerHTML
    assert "innerHTML" not in response.text


def test_dashboard_fetches_are_timeout_guarded() -> None:
    # Regression (browser QA, 2026-06-11): with postgres paused, /picks and
    # /performance hang ~80s before failing while /health answers instantly.
    # Each 60s tick's load() then failed ~20s AFTER the next tick had already
    # started, so the offline banner never rendered. The dashboard must
    # (a) abort every fetch after 15s, (b) never start a tick while a load()
    # is in flight, and (c) render a distinct ENGINE UNRESPONSIVE state for
    # the aborted/timed-out case (process up, not answering) — different from
    # OFFLINE (connection refused) and SERVER ERROR (HTTP 5xx).
    text = TestClient(make_app()).get("/").text
    assert "function fetchWithTimeout" in text
    # every data fetch must go through the timeout helper — a bare fetch(
    # would reintroduce the indefinite hang
    assert 'fetchWithTimeout("/picks' in text
    assert 'fetchWithTimeout("/performance' in text
    assert 'fetchWithTimeout("/health' in text
    # settle POST too (formatter may wrap the URL onto the next line)
    assert re.search(r'fetchWithTimeout\(\s*"/events/', text)
    # the raw fetch( primitive appears exactly once: inside the helper
    assert text.count("fetch(") == 1
    # in-flight guard: a new tick must not pile onto a hung load()
    assert "LOAD_IN_FLIGHT" in text
    # the distinct third banner state
    assert "ENGINE UNRESPONSIVE" in text
    assert "UNRESPONSIVE" in text


def test_dashboard_has_tier_filter_and_premium_scoped_cards() -> None:
    """Two-tier UI contract: a tier filter (default PREMIUM) consistent with
    the existing view toggles; volume rows marked by a muted VOL badge that
    reuses the status badge slot (no new column — the 1280px no-scroll
    layout must hold); every summary card explicitly labelled premium."""
    text = TestClient(make_app()).get("/").text
    assert 'id="f-tier"' in text
    assert '<option value="premium" selected>' in text  # premium is default
    assert "ALL TIERS" in text
    # the muted VOL badge + its honest tooltip
    assert 'isVolume ? "vol" : "open"' in text
    assert "volume (shadow) tier" in text
    # summary cards say they are premium-scoped
    assert "Open picks (premium, verified)" in text
    assert "Avg live edge (premium open)" in text
    assert "Settled (premium, all time)" in text
    assert "P&amp;L / ROI (premium settled)" in text
    assert "Stake-wtd CLV (premium settled)" in text
    # textContent discipline still holds with the new badge path
    assert "innerHTML" not in text


def test_picks_tier_param_is_validated() -> None:
    # tier scopes the feed server-side (premium|volume); anything else must
    # 422 before the handler ever touches the DB.
    client = TestClient(make_app())
    assert client.get("/picks?tier=bogus").status_code == 422
    assert client.get("/picks?tier=").status_code == 422


def test_games_endpoint_serves_unrestricted_latest_fixture_view() -> None:
    from app.pipeline import AVAILABLE_GAMES

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
        assert client.get("/games?sport=tennis").status_code == 422
    finally:
        AVAILABLE_GAMES.pop("soccer", None)
        AVAILABLE_GAMES.pop("basketball", None)


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


def test_dashboard_fetches_picks_per_tier() -> None:
    """Volume-flood regression: one unscoped /picks?limit=200 window fills
    with volume rows (~6x premium) and open premium picks vanish from both
    the table and the headline cards. The dashboard must fetch each tier's
    own window."""
    text = TestClient(make_app()).get("/").text
    assert "/picks?limit=200&tier=premium" in text
    assert "/picks?limit=200&tier=volume" in text
    assert '"/picks?limit=200"' not in text  # the unscoped fetch is gone


def test_dashboard_fetches_and_renders_available_games() -> None:
    text = TestClient(make_app()).get("/").text
    assert 'id="toggle-games"' in text
    assert 'aria-expanded="false"' in text
    assert 'id="games-panel" hidden' in text
    assert 'id="games-table"' in text
    assert 'id="f-game-sport"' in text
    assert 'fetchWithTimeout("/games?limit=1000")' in text
    assert "function setGamesOpen" in text
    assert "setGamesOpen(false)" in text
    assert '$("toggle-games").addEventListener("click"' in text
    assert "renderGames" in text
    assert "NO GAMES LOADED" in text
    assert "innerHTML" not in text


def test_dashboard_has_mobile_table_card_layout() -> None:
    text = TestClient(make_app()).get("/").text
    assert "@media (max-width: 720px)" in text
    assert "#picks-table td:nth-child(11)::before" in text
    assert 'content: "Status"' in text
    assert "#games-table td:nth-child(8)::before" in text
    assert 'content: "Updated"' in text
    assert "overflow-wrap: anywhere" in text
    assert "td[colspan]::before" in text
    assert "innerHTML" not in text


def test_performance_payload_includes_live_evidence(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """GET /performance carries the stratified live-evidence section — the
    instrument for the VALUE_ML_FILTER flip. DB reads are stubbed at the
    route's own imports; the pure report runs for real, so the honest-n
    contract (sufficient=false under min_n CLV rows) is asserted end-to-end."""
    from app.api import routes
    from app.backtesting.live_evidence import SettledPickRow

    async def fake_perf(session):  # type: ignore[no-untyped-def]
        return {"n_settled": 2, "tier_scope": "premium"}

    async def fake_rows(session):  # type: ignore[no-untyped-def]
        return [
            SettledPickRow("premium", 0.80, 0.02, True, 10.0, 1.0),
            SettledPickRow("volume", None, None, None, 5.0, None),
        ]

    monkeypatch.setattr(routes, "performance_report", fake_perf)
    monkeypatch.setattr(routes, "live_evidence_rows", fake_rows)
    monkeypatch.setattr(routes, "_ml_operating_point", lambda: 0.725)

    body = TestClient(make_app()).get("/performance").json()
    assert body["tier_scope"] == "premium"  # headline scope untouched
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


def test_dashboard_has_live_evidence_panel_and_min_odds_helper() -> None:
    """Live-evidence panel + execution helper on the dashboard: honest-n
    insufficient states, hidden-until-served panel, the 'ok >=' odds-floor
    line, and the textContent discipline still holding."""
    text = TestClient(make_app()).get("/").text
    assert 'id="evidence-panel"' in text
    assert "renderEvidence" in text
    assert "if (!Number(ev.n_settled))" in text
    assert "function setEvidenceGroupOpen" in text
    assert 'button.className = "evtoggle"' in text
    assert 'button.addEventListener("click"' in text
    assert 'button.setAttribute("aria-expanded", String(open))' in text
    assert "tr.dataset.evGroup = groupKey" in text
    assert "insufficient data (n<" in text  # explicit per-stratum state
    assert "Live evidence" in text
    # execution helper line in the odds column
    assert "min_acceptable_odds" in text
    assert "still +EV down to" in text
    assert "innerHTML" not in text  # untrusted strings stay on textContent


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
