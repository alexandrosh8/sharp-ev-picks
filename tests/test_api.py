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
    # safety reminder must be visible on the dashboard. The old bold footer
    # banner was removed (operator request); the picks-only / no-profit framing
    # now lives in the relocated Status/CLV legend copy.
    assert "the system places none" in response.text
    assert "not a profit guarantee" in response.text
    assert "<footer>" not in response.text
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
    the existing view toggles; volume rows marked by a muted SHADOW badge that
    reuses the status badge slot (no new column — the 1280px no-scroll
    layout must hold); every summary card explicitly labelled premium."""
    text = TestClient(make_app()).get("/").text
    assert 'id="f-tier"' in text
    assert '<option value="premium" selected>' in text  # premium is default
    assert "ALL TIERS" in text
    # the muted SHADOW badge (relabel of "vol") + its honest tooltip
    assert '"SHADOW"' in text
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
        # Tennis is a visibility-only sport (OFF by default) but DOES surface in
        # /games when enabled, so the sport filter must accept it (200, empty
        # here) rather than 422 - otherwise the view shows tennis unfiltered
        # but rejects filtering to it.
        tennis_resp = client.get("/games?sport=tennis")
        assert tennis_resp.status_code == 200
        assert tennis_resp.json() == []
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
    # confidence replaced the Stake label at picks column 9
    assert "#picks-table td:nth-child(9)::before" in text
    assert 'content: "Confidence"' in text
    # games table is now 5 columns: last label is Coverage at child 5
    assert "#games-table td:nth-child(5)::before" in text
    assert 'content: "Coverage"' in text
    # SETTLED view drives mobile labels off data-label so they follow the
    # (shorter) active column set
    assert "#picks-table.settled td::before" in text
    assert "content: attr(data-label)" in text
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

    monkeypatch.setattr(routes, "shadow_match_rate_outcomes", fake_outcomes)
    monkeypatch.setattr(routes, "pinnacle_archive_capture_by_sport", fake_capture)
    body = TestClient(make_app()).get("/resolution/match-rate").json()
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


def test_dashboard_html_is_not_browser_cached() -> None:
    """The dashboard HTML shell must not be browser-cached: a deploy ships new
    structure (panels, badges, banner) but the page only reloads on a full
    refresh — the 60s auto-refresh re-fetches DATA, not the page. A cached shell
    masks the update behind a stale tab. Cache-Control: no-store forces a fresh
    shell each load."""
    res = TestClient(make_app()).get("/")
    assert res.status_code == 200
    assert "no-store" in res.headers.get("cache-control", "").lower()


def test_dashboard_legend_frames_clv_and_confidence() -> None:
    """The static legend (relocated to the page bottom, below the Pinnacle
    archive panel) frames CLV (proof of edge, NOT a profit guarantee) and the ★
    confidence stars (edge-confidence, not win probability). The old dismissible
    intro banner, the hover "?" explainers, AND the bold footer banner were all
    removed (operator request); the picks-only / no-profit safety framing now
    lives entirely in this legend copy."""
    text = TestClient(make_app()).get("/").text
    assert 'id="picks-legend"' in text
    assert "proof of real edge" in text
    assert "not a profit guarantee" in text
    assert "confidence in the EDGE" in text
    # the picks-only safety reminder migrated from the footer into the legend
    assert "the system places none" in text
    # the dismissible intro banner, hover "?" explainers, footer, and the old
    # "Always confirm the live price" line were all removed
    assert 'id="intro"' not in text
    assert 'id="intro-dismiss"' not in text
    assert "data-tip" not in text
    assert "<footer>" not in text
    assert "Always confirm the live price" not in text


def test_dashboard_has_archive_coverage_panel() -> None:
    """The dashboard surfaces the Pinnacle-archive match-rate dial (the
    readiness signal for CLV_USE_PINNACLE_ARCHIVE) as a collapsed, lazy-loaded
    panel that reads GET /resolution/match-rate. Honest framing: shadow-only,
    never presented as changing a pick."""
    text = TestClient(make_app()).get("/").text
    assert "Pinnacle archive coverage" in text
    assert 'id="archive-panel"' in text
    assert 'id="toggle-archive"' in text
    assert "/resolution/match-rate" in text


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

    async def fake_rows(session, limit, tier=None, min_edge=0.0):  # type: ignore[no-untyped-def]
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


def test_dashboard_renders_confidence_stars_not_visible_stake() -> None:
    """The Confidence stars are the headline (they replaced the stake column).
    The operator removed the hover tooltip, so the stake % is no longer surfaced
    in the dashboard at ALL, and the ★ framing (confidence in the EDGE, not a win
    probability) lives in the static legend — never claiming a win rate."""
    text = TestClient(make_app()).get("/").text
    assert "<th>Confidence</th>" in text
    assert "confidence_rating" in text
    assert 'cell.className = "confcell"' in text  # the stars cell
    # framing moved to the static legend (no hover): edge-confidence, not P(win)
    assert "model confidence in the EDGE, not your" in text
    # the stake % is no longer surfaced anywhere in the dashboard (tooltip gone)
    assert text.count("recommended_stake_amount") == 0
    assert "Recommended fractional-Kelly" not in text
    # the star glyphs are present and the rating reads off confidence_rating
    assert "★" in text  # filled star
    assert "☆" in text  # empty star
    # stars built with textContent, never innerHTML
    assert "innerHTML" not in text


def test_dashboard_fades_value_gone_picks() -> None:
    """A pick whose LIVE re-price no longer beats fair value (current_edge <= 0)
    is FADED + tagged 'no value now' so it isn't mistaken for a fresh opportunity —
    but it is NEVER removed from the board or from CLV tracking (dropping losers
    would be survivorship bias). We assert the affordance (predicate + row class
    + chip) is present, and the chip is built via the badge() helper (textContent,
    not innerHTML)."""
    text = TestClient(make_app()).get("/").text
    # predicate keyed on current_edge <= 0 for open/alerted picks
    assert "function valueGone(" in text
    assert "current_edge" in text
    # the faded row class is applied AND styled
    assert 'tr.classList.add("valuegone")' in text
    assert "tr.valuegone {" in text
    # the muted chip label — time-scoped so it doesn't read as a contradiction
    # against a positive (green) CLV badge on the same row
    assert "no value now" in text
    # purely presentational — still no markup injection anywhere
    assert "innerHTML" not in text


def test_dashboard_shows_closing_price() -> None:
    """Once a pick has kicked off / settled, the Odds cell shows the de-facto
    closing price ("close X.XX") so the pick→close move is visible next to the
    entry (value) price. The close is the finalized closing_odds when present,
    else the frozen pre-kickoff current_odds (re-pricing stops at kickoff)."""
    text = TestClient(make_app()).get("/").text
    # the close line is rendered (textContent, no markup injection)
    assert 'textContent = "close "' in text
    assert "Book closing price" in text
    # it sources closing_odds first, then the frozen current_odds fallback
    assert "p.closing_odds" in text
    assert "const closeRaw" in text
    assert "innerHTML" not in text


def test_picks_serializer_exposes_closing_odds() -> None:
    """The GET /picks payload carries closing_odds so the dashboard can show the
    closing price. It is null until a pick settles; the dashboard falls back to
    the frozen current_odds for kicked-off-but-unsettled picks."""
    import inspect

    from app.storage import repositories

    src = inspect.getsource(repositories.latest_picks_with_events)
    assert '"closing_odds"' in src
    assert "p.closing_odds" in src


def test_dashboard_has_closed_tab_and_clv_scorecard() -> None:
    """The CLOSED tab is the proof-of-edge ledger: every kicked-off pick (line
    closed, CLV locked), split out of the old conflated UNVERIFIED tab. It is
    topped by a CLV scorecard (% that beat the close + mean CLV), computed
    client-side from the closed picks' clv_log/beat_close — text only."""
    text = TestClient(make_app()).get("/").text
    # the 4-tab segmented control includes CLOSED, between UNVERIFIED and SETTLED
    assert 'data-status="closed"' in text
    assert ">\n          CLOSED\n        </button>" in text or "CLOSED" in text
    # the tab predicate + router branch exist
    assert "function inClosedTab(" in text
    assert 'STATUS_TAB === "closed"' in text
    # the CLV scorecard element + its proof-of-edge headline (textContent)
    assert 'id="clv-scorecard"' in text
    assert "CLV ledger" in text
    assert "beat the close" in text
    assert "innerHTML" not in text


def test_dashboard_settled_view_swaps_table_header() -> None:
    """SETTLED-header regression: the desktop <thead> must be swapped to the
    8-col results set when the body renders the SETTLED column set, so each
    value sits under the right label. Before the fix the body rendered 8 cells
    under the fixed 11-col LIVE header and every Result/P&L/CLV value appeared
    under a mislabeling column (e.g. P&L under FAIR/EV). We assert the swap
    machinery (renderHead + a distinct SETTLED header set, called from
    render()) is present in the served page."""
    text = TestClient(make_app()).get("/").text
    # the header row is addressable and the swap function exists + is invoked
    assert 'id="picks-head"' in text
    assert "function renderHead(" in text
    assert "renderHead(settledView)" in text
    # the SETTLED header set is results-oriented (Result/P&L), distinct from the
    # LIVE set (Market/Fair/EV/Edge/Confidence/Status do NOT appear in it)
    assert '"Result"' in text
    assert '"P&L"' in text
    # header cells built with textContent / text nodes (no markup injection):
    # keyless headers use textContent, sortable ones append a label text node.
    # The global no-innerHTML guard above covers the whole-page XSS contract.
    assert "th.textContent = col.label" in text
    assert "createTextNode(col.label)" in text


def test_dashboard_picks_table_columns_are_sortable() -> None:
    """Clickable column-sort machinery is served: a comparator registry keyed
    per column, per-view sortable-key gating, a toggle that flips direction,
    accessible sortable headers (aria-sort), and persisted column+direction
    validated on restore. Display-only — sorts the `rows` array, never the
    server ORDER BY (the no-innerHTML XSS contract is asserted globally)."""
    text = TestClient(make_app()).get("/").text
    # comparator registry + the per-view key gate
    assert "const SORT_COLS = {" in text
    assert "function headSortKeys(settledView)" in text
    assert "function toggleSort(key)" in text
    # accessible, clickable headers
    assert 'th.classList.add("sortable")' in text
    assert '"aria-sort"' in text
    # settled Result ranking exists (clusters + orders outcomes)
    assert "const OUTCOME_RANK = {" in text
    # active column + direction persist across reloads and are validated on
    # restore against the known comparator keys
    assert '"pt_sortcol"' in text
    assert '"pt_sortdir"' in text
    assert "SORT_COLS[savedSortCol]" in text
    # the default best-on-top sort is still the no-active-column fallback
    assert "confLevel" in text
