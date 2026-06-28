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


def test_dashboard_served_at_root() -> None:
    client = TestClient(make_app())
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    # safety reminder must be visible on the dashboard. The TAPE redesign keeps
    # the no-autobet / no-profit framing in the legend ("This system never places
    # bets" + "not a profit guarantee"); the picks live in the "tape" feed.
    assert "never places bets" in response.text
    assert "not a profit guarantee" in response.text
    assert "<footer>" not in response.text
    assert 'id="tape"' in response.text
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
    # the mutating POST goes through the timeout helper too. The TAPE redesign is
    # read-only (no manual settle button), so the surviving POST is /logout; it
    # must still ride the helper (formatter may wrap the URL onto the next line).
    assert re.search(r'fetchWithTimeout\(\s*"/logout', text)
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
    # premium is the default tier — it is the FIRST option (the browser selects
    # the first <option> when none is marked selected).
    assert '<option value="premium">' in text
    assert text.index('value="premium"') < text.index('value="volume"')
    assert "ALL TIERS" in text
    # the muted SHADOW badge (volume tier) + its honest tooltip in the legend
    assert '"SHADOW"' in text
    assert "volume tier: never alerted or sized" in text
    # summary cards (settled ledger) say they are premium-scoped
    assert "Open · premium verified" in text
    assert "Settled (all-time)" in text
    assert "P&amp;L / ROI (units)" in text
    # CLV-1: the hero is the TRUSTED sharp-close subset; the blended figure is a
    # clearly-labelled secondary (context) tile.
    assert "sharp closes only" in text
    assert "All-closes CLV · context" in text
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
    # the games table's tbody is addressable (was id="games-table" on the table)
    assert 'id="game-rows"' in text
    assert 'id="f-game-sport"' in text
    assert 'fetchWithTimeout("/games?limit=1000")' in text
    # the collapsible is wired through the generic toggleBlock helper (was the
    # bespoke setGamesOpen): it binds the click and lazily renders on open.
    assert 'toggleBlock("toggle-games", "games-panel"' in text
    assert 'btn.addEventListener("click"' in text
    assert "renderGames" in text
    assert "No games loaded" in text
    assert "innerHTML" not in text


def test_dashboard_has_mobile_table_card_layout() -> None:
    text = TestClient(make_app()).get("/").text
    # TAPE redesign: picks are NATIVE article cards (el.className = "pick"), not a
    # table that collapses via td::before pseudo-labels — so on mobile they are
    # inherently card-shaped, never a cramped horizontally-scrolling table. The
    # same intent (mobile = stacked, readable cards) is met by the responsive
    # deck collapsing to a single column at the tablet breakpoint.
    assert 'el.className = "pick"' in text
    assert "@media (max-width: 980px)" in text
    # the two-pane deck stacks to one column at that breakpoint
    assert "grid-template-columns: 1fr;" in text
    assert 'grid-template-areas: "tape" "settled" "coverage";' in text
    # long event/league names are truncated, not allowed to blow out the card
    assert "text-overflow: ellipsis" in text
    assert "innerHTML" not in text


def test_performance_payload_includes_live_evidence(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """GET /performance carries the stratified live-evidence section — the
    instrument for the VALUE_ML_FILTER flip. DB reads are stubbed at the
    route's own imports; the pure report runs for real, so the honest-n
    contract (sufficient=false under min_n CLV rows) is asserted end-to-end."""
    from app.api import routes
    from app.backtesting.live_evidence import SettledPickRow

    async def fake_perf(session):  # type: ignore[no-untyped-def]
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
        return [
            SettledPickRow("premium", 0.80, 0.02, True, 10.0, 1.0),
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

    monkeypatch.setattr(routes, "shadow_match_rate_outcomes", fake_outcomes)
    monkeypatch.setattr(routes, "pinnacle_archive_capture_by_sport", fake_capture)
    monkeypatch.setattr(routes, "betfair_archive_capture_by_sport", fake_betfair_capture)
    monkeypatch.setattr(routes, "betfair_inline_capture_by_sport", fake_betfair_inline_capture)
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
    # The headline's Betfair number now comes from the INLINE coverage (the real
    # pick-feeding anchor: Betfair Exchange bound onto the canonical event), NOT
    # the near-empty archive path. The per-sport panel body keeps the archive
    # numbers so a structural-vs-thin-slate read stays available.
    bf_panel = {row["sport"]: row for row in body["betfair_capture"]}
    assert bf_panel["soccer"]["captured"] == 4  # archive path stays near-empty
    bf_inline = {row["sport"]: row for row in body["betfair_inline_capture"]}
    assert bf_inline["soccer"]["captured"] == 99  # inline canonical-event coverage
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
    # the static legend block (no dismissible intro/footer) carries the framing
    assert 'class="legend"' in text
    # CLV framed as proof of edge, NOT a profit guarantee (hero safety copy)
    assert "honest proof of edge" in text
    assert "not a profit guarantee" in text
    # ★ confidence framed as edge-confidence, not win probability
    assert "confidence in the EDGE" in text
    # the picks-only safety reminder (was "the system places none")
    assert "This system never places bets" in text
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
    # the panel header (was "Pinnacle archive coverage") and the Pinnacle-archive
    # readiness framing in the panel body
    assert "Sharp-close coverage" in text
    assert "Pinnacle sharp-close archive" in text
    assert 'id="archive-panel"' in text
    assert 'id="toggle-archive"' in text
    assert "/resolution/match-rate" in text


def test_dashboard_surfaces_coverage_headline_eagerly() -> None:
    """The sharp-anchor coverage panel HEADER shows the real Betfair/Pinnacle %
    up front (the operator saw a bare '—'). The headline comes from the backend
    coverage_summary and is populated on initial load, BEFORE the lazy panel is
    expanded — so the dashboard reads coverage_summary and calls the eager
    loader at boot."""
    text = TestClient(make_app()).get("/").text
    # eager headline loader exists and is invoked at boot (not only on expand)
    assert "function loadCoverageHeadline" in text
    assert "loadCoverageHeadline();" in text
    # the header reads the backend's coverage_summary.headline
    assert "coverage_summary" in text
    assert "function setCoverageHeadline" in text
    # the headline renders into the collapsible's header (the sb-head toggle's
    # count slot) so it's visible BEFORE the panel is expanded
    assert 'class="sb-head"' in text
    assert 'id="archive-count"' in text
    assert "games-head" not in text  # old cluttered layout fully removed
    assert "innerHTML" not in text  # untrusted strings stay on textContent


def test_dashboard_surfaces_unvalidated_sport_in_plain_language() -> None:
    """Visibility-only sports (tennis, American football) show a clear,
    plain-language 'not validated — informational only' status in the games
    table, not a bare confusing 'UNVALIDATED' word."""
    text = TestClient(make_app()).get("/").text
    # plain-language "not validated — informational only" status (games table
    # tooltip + the picks-feed sport tag), not a bare confusing "UNVALIDATED"
    assert "model not validated — informational only" in text
    assert "Unvalidated sport — informational only" in text
    # the per-row flag still drives it (validated===false OR unvalidated===true)
    assert "g.validated === false || g.unvalidated === true" in text


def test_dashboard_renders_known_kickoff_and_clean_tbd() -> None:
    """Kickoff renders a real local time when starts_at is present, and a clean
    tooltipped 'TBD' (never a bare 'kickoff unknown') when it is null — in BOTH
    the picks table and the games table."""
    text = TestClient(make_app()).get("/").text
    # known kickoff -> real local time via fmtLocal; TBD branch is tooltipped
    assert "fmtLocal(g.starts_at)" in text
    assert "the odds source has not reported a kickoff time" in text
    # the bare confusing "kickoff unknown" copy is gone everywhere
    assert "kickoff unknown" not in text
    assert "time to be confirmed" in text


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


def test_dashboard_edge_color_uses_tier_aware_floor() -> None:
    """dash-2 / EEV-1: the Edge colour + the Odds-cell verdict must compare the
    live edge to the row's TIER floor (edgeFloor(p)), not a hardcoded 3%, so a
    volume pick at 1.8% reads green, not amber. The helper prefers the
    serialized per-row edge_floor and falls back to the /health floors."""
    text = TestClient(make_app()).get("/").text
    assert "function edgeFloor(" in text
    assert "p.edge_floor" in text
    # /health fallback floors are consumed
    assert "VALUE_MIN_EDGE" in text
    assert "VALUE_VOLUME_MIN_EDGE" in text
    assert "health.value_min_edge" in text
    assert "health.value_volume_min_edge" in text
    # the hardcoded 0.03 colour/verdict comparisons are gone
    assert "curEdge >= 0.03" not in text
    assert "eff >= 0.03" not in text
    # and the call sites now use the tier-aware floor
    assert text.count("edgeFloor(p)") >= 2


def test_dashboard_live_ev_uses_commission_netted_edge() -> None:
    """edge-ev-devig-r2-1: on a re-priced row the live EV is derived from the
    commission-netted live edge (ev = liveFair/(liveFair - current_edge) - 1),
    NOT the raw book odds, so exchange commission isn't double-counted away."""
    text = TestClient(make_app()).get("/").text
    # netted formula present: ev = liveFair/(liveFair - current_edge) - 1, with the
    # commission-netted denominator bound to `denom` first.
    assert "denom = liveFair - ce" in text
    assert "liveFair / denom - 1" in text
    assert "p.current_edge != null" in text
    # raw-odds form survives only as the fallback when current_edge is null
    assert "liveFair * Number(p.current_odds) - 1" in text


def test_dashboard_clv_hero_tiles_use_sharp_subset() -> None:
    """CLV-1: the PROOF-OF-EDGE hero (stake-wtd CLV) and the Beat-close tile read
    the TRUSTED sharp-close subset (perf.sharp_*), gated on sharp_status==='ok'.
    The blended (all-closes) figure is demoted to a clearly labelled secondary
    tile so consensus/fallback closes never headline the proof of edge."""
    text = TestClient(make_app()).get("/").text
    assert "perf.sharp_status" in text
    assert "perf.sharp_stake_weighted_clv_log" in text
    assert "perf.sharp_beat_close_rate" in text
    # the blended (all-closes) figure is demoted to a clearly-labelled context
    # tile sourced from the blended field, never the headline
    assert "All-closes CLV · context" in text
    assert "perf.stake_weighted_clv_log" in text


def test_dashboard_clv_hero_shows_accruing_progress_when_insufficient() -> None:
    """DASHBOARD CLARITY: below the sharp-close floor (sharp_status !== 'ok') the
    PROOF-OF-EDGE hero must render an explicit INSUFFICIENT-EVIDENCE progress
    state ('n / min — accruing') from perf.n_sharp_close + perf.min_headline_n,
    NOT a blank '—' (which reads as broken) and NOT the blended/circular CLV."""
    text = TestClient(make_app()).get("/").text
    # progress fraction is built from BOTH payload fields
    assert "perf.min_headline_n" in text
    assert "perf.n_sharp_close" in text
    assert 'nSharp + " / " + minSharp' in text
    # demoted, honest label + neutral 'accruing' styling (not green +EV)
    assert "accruing" in text
    assert ".hero-num.accruing" in text


def test_dashboard_per_row_clv_respects_close_independence() -> None:
    """CLV-1: a circular (self-priced) close — close_independent_of_fill === false
    — must NOT be shown as honest CLV; the per-row CLV cell renders a neutral
    'self-priced' state instead of a green/red beat-close badge."""
    text = TestClient(make_app()).get("/").text
    assert "p.close_independent_of_fill === false" in text
    assert "self-priced" in text


def test_dashboard_per_pick_clv_chip_inherits_sharp_anchor_trust() -> None:
    """Audit 2026-06-28 P2: the per-pick CLV chip must inherit the headline's
    SHARP-anchor trust rule. Only a close anchored by a named sharp book
    (closing_anchor_type in pinnacle/sharp) renders as a trusted (green/red
    beat-close) CLV; a consensus/soft/null-anchored close renders INDICATIVE
    (neutral) with a 'consensus close' sub-label, so a RESULTS scan can never
    misread an untrusted close as validated."""
    text = TestClient(make_app()).get("/").text
    # the sharp-anchor allow-list mirrors the backend _SHARP_CLOSE_ANCHORS
    assert "SHARP_CLOSE_ANCHORS" in text
    assert '"pinnacle"' in text and '"sharp"' in text
    # clvInfo consults the close anchor and demotes non-sharp closes to indicative
    assert "p.closing_anchor_type" in text
    assert "consensus close" in text
    # the trusted (green/red) styling is gated behind the sharp-anchor check
    assert "trusted" in text


def test_dashboard_per_pick_clv_applies_fabricated_guard() -> None:
    """Audit 2026-06-28 P2: the per-pick CLV chip + the beat-close distribution
    must apply the SAME fabricated/tautological guard the aggregate uses, so a
    physically-impossible close (close-implied edge > 0.20, or |clv_log| > 0.5)
    or a tautological identical-line close is dropped/neutralised — never shown
    green and never counted in the >5% histogram bin."""
    text = TestClient(make_app()).get("/").text
    # the guard constants mirror the backend (repositories.py)
    assert "CLV_IMPLAUSIBLE_CLOSE_EDGE" in text
    assert "0.2" in text  # the impossible close-implied-edge ceiling
    assert "CLV_IMPLAUSIBLE_LOG" in text
    assert "CLV_TAUTOLOGY_EPS" in text
    # the guard helpers exist and are applied before a chip / a histogram count
    assert "function clvRowFabricated" in text
    assert "function clvRowTautological" in text
    assert "clvRowExcluded(p)" in text


def test_dashboard_drift_bar_and_fair_stat_use_same_fair_source() -> None:
    """Audit 2026-06-28 P2: the drift-bar FAIR marker and the 'fair %' stat must
    read the SAME fair. Both use the reprised rule (closing_fair_probability AND
    current_odds present) -> live fair, else the entry fair (model_probability),
    so a partially-repriced pick (closing_fair set, current_odds null) can never
    show two different fairs on the same card."""
    text = TestClient(make_app()).get("/").text
    # the drift bar's fair source now requires a live PRICE too (current_odds),
    # exactly like the stat's `reprised` rule — no bare closing_fair ?? model fork
    assert "p.closing_fair_probability != null && p.current_odds != null" in text
    # the legacy drift-bar fork that ignored current_odds is gone
    assert "p.closing_fair_probability != null ? Number(p.closing_fair_probability)" not in text


def test_dashboard_distribution_scoped_to_active_tier() -> None:
    """Audit 2026-06-28 P2 (P3): the beat-close distribution must be scoped to the
    active tier filter, consistent with the premium-scoped hero — not built from
    the global PICKS set."""
    text = TestClient(make_app()).get("/").text
    # renderDistribution reads the tier filter and scopes rows to it
    assert "function renderDistribution" in text
    body = text[text.index("function renderDistribution") :]
    body = body[: body.index("function ", 1)]
    assert "f-tier" in body
    assert "tierOf(p)" in body


def test_dashboard_open_card_matches_live_tab() -> None:
    """open-card mismatch: the 'Open picks (premium, verified)' card counts the
    SAME set as the LIVE tab (inLiveTab) so the card and the LIVE (n) badge can
    never diverge (e.g. when a value-gone pick is excluded from one but not the
    other)."""
    text = TestClient(make_app()).get("/").text
    assert "premium.filter(inLiveTab)" in text


def test_dashboard_shows_fair_drift_delta() -> None:
    """Welwalo clarity: when a re-price moved the fair probability, the Fair/EV
    cell shows a VISIBLE entry->live delta (e.g. 'fair 64%->58%'), and the
    'ok >=' odds floor line reads 'needs >= X (fair drifted out)' when the book
    price is unchanged but the fair has drifted the value away."""
    text = TestClient(make_app()).get("/").text
    assert "entryFair" in text
    assert "fair drifted out" in text
    # the delta is rendered as a visible element, not only a tooltip
    assert "fair " in text


def test_dashboard_verified_window_comment_matches_freshness() -> None:
    """dash-1: verifiedWindowMs() returns MAX_ODDS_AGE_SECONDS when /health
    reports it; the comment + the status-bar tooltip must describe the value
    FRESHNESS window (not assert a flat '45 min') so the annotation matches the
    value actually used."""
    text = TestClient(make_app()).get("/").text
    # the freshness window is named in the verified-window tooltip
    assert "freshness window" in text
    # the tooltip switches to the fallback wording only when MAX_ODDS_AGE_S is null
    assert "MAX_ODDS_AGE_S !== null" in text


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


def test_dashboard_renders_confidence_stars_not_visible_stake() -> None:
    """The Confidence stars are the headline (they replaced the stake column).
    The operator removed the hover tooltip, so the stake % is no longer surfaced
    in the dashboard at ALL, and the ★ framing (confidence in the EDGE, not a win
    probability) lives in the static legend — never claiming a win rate."""
    text = TestClient(make_app()).get("/").text
    # the stars are the confidence headline in the pick card (was a <th>Confidence</th>
    # table column); the cell carries class "stars" and is built from confidence_rating
    assert 'stars.className = "stars"' in text
    assert 'stars.textContent = "★".repeat(n)' in text
    assert "confidence_rating" in text
    # framing (no hover): edge-confidence, not P(win)
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
    # the faded card class is applied AND styled (was tr.valuegone; now .pick.gone)
    assert 'el.className = "pick" + (valueGone(p) ? " gone"' in text
    assert ".pick.gone {" in text
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
    # the close price is rendered in the drift legend labelled "close" (vs "now"
    # for a still-open pre-kickoff line); textContent only, no markup injection
    assert '? "close" : "now"' in text
    assert "addLeg(nowLabel, nowO.toFixed(2))" in text
    assert '"Book " + nowLabel' in text  # the book close/now tooltip
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


def test_dashboard_has_results_tab_and_clv_scorecard() -> None:
    """The RESULTS view MERGES kicked-off picks (line closed, CLV locked) with
    settled ones: still-open ones show their won/lost/awaiting result graded from
    the auto-fetched score, settled ones show the recorded outcome + P&L. The
    proof-of-edge scorecard (W-L-P record + % beat close + mean CLV) is computed
    server-side and rendered text-only.

    TAPE redesign: SETTLED is not a separate tab (it folds into RESULTS via
    inResultsTab), and the scorecard moved out of a per-tab element into the
    always-visible 'THE CLOSE' hero band. The redesign DID re-introduce a
    separate CLOSED filter alongside RESULTS, but RESULTS still merges both —
    that merge is what this test guards."""
    text = TestClient(make_app()).get("/").text
    assert 'data-status="results"' in text
    # SETTLED is not its own tab — it is folded into RESULTS
    assert 'data-status="settled"' not in text
    # the merge predicate proves RESULTS = kicked-off (closed) ∪ settled
    assert "function inResultsTab(" in text
    assert '(p.status === "alerted" && hasStarted(p)) || p.status === "settled"' in text
    # RESULTS is the router's fallback branch + the count wiring
    assert ": inResultsTab(p)" in text
    assert "results: scoped.filter(inResultsTab)" in text
    # the scorecard relocated to the hero band: W-L-P record + % beat close (sharp)
    assert "Record W-L-P" in text
    assert "Beat close · sharp" in text
    assert "beat close" in text
    assert "innerHTML" not in text


def test_dashboard_settled_view_swaps_table_header() -> None:
    """SETTLED-label regression: settled/closed picks must show their results
    (score, Result outcome badge, P&L) under the RIGHT labels, distinct from the
    LIVE column set (Fair/EV/Edge), so a P&L value never reads under a FAIR/EV
    label.

    TAPE redesign: picks are inline-labelled CARDS, not a fixed-column table with
    a swapped <thead> — so the mislabeling bug class is structurally impossible
    (every stat carries its own label via mkStat(label, val)). The same
    distinct-column-set behaviour is realised by the per-card settledView branch:
    settled/closed picks render score + outcome + P&L, live picks render
    fair/EV/edge."""
    text = TestClient(make_app()).get("/").text
    # the settled/closed view branch (was renderHead(settledView) for the thead swap)
    assert "const settledView" in text
    assert 'p.status === "settled" || inClosedTab(p)' in text
    # SETTLED branch renders the results set: score, Result outcome badge, P&L
    assert 'mkStat("score"' in text
    assert "p.outcome || p.provisional_outcome" in text
    assert 'mkStat("p&l"' in text
    # LIVE branch renders a DISTINCT set (fair/EV/edge) — never shown for settled
    assert 'mkStat("fair"' in text
    assert 'mkStat("EV"' in text
    # every stat carries its own inline label, so a value can never sit under the
    # wrong column (the structural guarantee that replaces the header swap)
    assert "function mkStat(label, val)" in text


def test_dashboard_tape_has_persisted_sort_control() -> None:
    """The TAPE card feed replaced the old clickable column-sort TABLE with an
    explicit 'sort by' selector over the tape. The control must: exist as a real
    <select id="f-sort"> with the documented comparator options; drive the order
    via a comparator registry consumed by render() (sortRows reads the select);
    keep the best-on-top default (confidence -> effective edge) as the no-key
    fallback; and PERSIST the choice in localStorage, validated against the known
    comparators on restore. Display-only — it reorders the `rows` array, never
    the server ORDER BY (the no-innerHTML XSS contract is asserted globally)."""
    text = TestClient(make_app()).get("/").text
    # the sort control is a real, accessible selector on the tape
    assert 'id="f-sort"' in text
    assert 'aria-label="Sort the tape"' in text
    # the documented comparator options are offered
    assert '<option value="default">' in text
    for key in ("confidence", "edge", "kickoff", "clv"):
        assert '<option value="' + key + '">' in text
    # a comparator registry keyed per option + the function that applies it
    assert "const SORT_KEYS = {" in text
    assert "function sortRows(rows, key)" in text
    # render() drives the order from the live selector value (not a static order)
    assert 'sortRows(rows, ($("f-sort") && $("f-sort").value) || "default")' in text
    # the default best-on-top order (confidence -> effective edge) is the
    # no-explicit-key fallback inside sortRows
    assert "confLevel" in text
    assert "SORT_KEYS.confidence" in text
    # the chosen sort PERSISTS across reloads and is validated on restore against
    # the known comparator keys before being applied
    assert 'localStorage.setItem("pt_sortcol"' in text
    assert 'localStorage.getItem("pt_sortcol")' in text
    assert "SORT_KEYS[savedSortCol]" in text
    # display-only reorder, never a second server fetch / no XSS sink
    assert "innerHTML" not in text
