"""SignalDesk dashboard contract — pins SEMANTICS, not the old TAPE structure.

This is the from-scratch replacement contract for the dashboard rebuild. It
never asserts old ids/classes/function names (see the deleted assertions in
tests/test_api.py); it pins the honest state vocabulary, safety framing, data
layer discipline, and the new 5-view nav that the SignalDesk console must
carry regardless of how the markup is composed internally.
"""

import re

from fastapi.testclient import TestClient

from tests.test_api import make_app


def _text() -> str:
    return TestClient(make_app()).get("/").text


def test_no_tape_branding() -> None:
    text = _text()
    assert "TAPE" not in text
    # Rebranded to the README wordmark (sharp-ev-picks); SignalDesk retired.
    assert "sharp-ev-picks" in text
    assert "SignalDesk" not in text


def test_no_innerhtml_sink() -> None:
    assert "innerHTML" not in _text()


def test_avoids_promotional_language() -> None:
    text = _text()
    low = text.lower()
    for banned in ("sure bet", "easy money", "best bet", "risk-free"):
        assert banned not in low, banned
    # "guaranteed" / "lock" only as standalone words, never as identifiers
    # like "toggleBlock" or ".sub-block".
    assert re.search(r"\bguaranteed\b", low) is None
    assert re.search(r"\block(?:ed|s|ing)?\b", low) is None


def test_tier_scoped_picks_fetches_present_and_unscoped_absent() -> None:
    text = _text()
    assert '"/picks?limit=200&tier=premium"' in text
    assert '"/picks?limit=200&tier=volume"' in text
    assert '"/picks?limit=200"' not in text


def test_fetch_layer_is_timeout_guarded() -> None:
    text = _text()
    assert "function fetchGuarded" in text
    assert re.search(r'fetchGuarded\(\s*"/picks', text)
    assert re.search(r'fetchGuarded\(\s*"/performance', text)
    assert re.search(r'fetchGuarded\(\s*"/health', text)
    assert re.search(r'fetchGuarded\(\s*"/logout', text)
    # the raw fetch( primitive exists only inside the guard helper itself
    assert text.count("fetch(") == 1


def test_match_rate_is_lazy_with_its_own_timeout() -> None:
    text = _text()
    assert "MATCH_RATE_TIMEOUT_MS" in text
    assert re.search(r'fetchGuarded\("/resolution/match-rate[^)]*MATCH_RATE_TIMEOUT_MS', text)
    assert "function loadMatchRate" in text
    # never fetched inside the boot-time loadOnce() cycle
    load_once = text[
        text.index("async function loadOnce") : text.index("async function loadOnce") + 2000
    ]
    assert "/resolution/match-rate" not in load_once


def test_games_fetch_does_not_block_initial_core_hydration() -> None:
    text = _text()
    load_once = text[
        text.index("async function loadOnce") : text.index("// ===== lazy /resolution")
    ]
    critical_wait = load_once[
        load_once.index("const [premiumBodyR") : load_once.index("const valueOrNull")
    ]
    assert "gamesBodyP" not in critical_wait
    assert "const gamesResultP = gamesBodyP.then(" in load_once
    assert load_once.index("state.coreLoaded = true") < load_once.index(
        "const gamesBodyR = await gamesResultP"
    )
    assert 'gamesPendingWithoutCache() ? "Fixtures loading"' in text


def test_cached_games_remain_trusted_while_refresh_is_pending() -> None:
    text = _text()
    assert (
        "const sourceTrusted = healthIsTrusted(health) && state.gamesErr === null && "
        "!gamesPendingWithoutCache();"
    ) in text
    assert "state.gamesErr === null && !state.gamesLoading" not in text


def test_proxy_pool_rendered_from_health_payload() -> None:
    text = _text()
    assert "function renderProxyRow" in text
    assert re.search(r"renderProxyRow\(\s*health\s*&&\s*health\.proxy_pool", text)
    # Severity is classified from the payload (degraded verdict / dead / bad slots),
    # guarded so an older payload without headroom can never render "undefined".
    assert "function classifyProxyPool" in text
    assert 'pool.verdict === "Proxy pool degraded"' in text
    assert re.search(r'typeof\s+pool\.headroom\s*===\s*"number"', text)
    # headroom<=0 is a CAPACITY hint (amber), not a hard failure (the old
    # hard-red "NO HEADROOM" was misleading — a full pool is still healthy).
    assert "no spare proxies above the" in text
    # Dead / quarantined slots surface automatically.
    assert "function proxyBadSlots" in text


def test_401_redirects_with_authentication_required_message() -> None:
    text = _text()
    assert "Authentication required." in text
    assert "function authRequired" in text
    assert 'window.location.assign("/login")' in text


def test_stale_notice_copy() -> None:
    assert "Odds data is stale. Picks should not be treated as current." in _text()


def test_required_empty_states() -> None:
    text = _text()
    # Fix 2026-07-10 #10: single clean empty state; the redundant
    # "Low Evidence" restatements of Qualified Now are no longer queued.
    assert "Nothing needs attention right now." in text
    assert "Low Evidence — no premium pick currently qualifies." not in text
    assert "Low Evidence — sharp evidence insufficient." not in text
    assert "No pick currently qualifies." in text
    assert "Could not load picks." in text
    assert "Could not load games." in text
    assert "Could not load performance data." in text


def test_state_vocabulary_present() -> None:
    text = _text()
    required = [
        "Premium",
        "Shadow",
        "Tracked — informational",
        "Pending",
        "Settled",
        "Won",
        "Lost",
        "Push",
        "Void",
        "Stale",
        "Weak Match",
        "Display-only",
        "Sharp Anchor",
        "Consensus Anchor",
        "MISSING ANCHOR",
        "Trusted CLV",
        "Untrusted Close",
        "Tautological Close Excluded",
        "Circular Close Excluded",
        "Monitor-only",
        "Low Evidence",
        # H2 readiness copy was rewritten to plain language ("Not ready — still
        # accruing…"); the terse "DO-NOT-RUN" token was retired deliberately.
        "Source Degraded",
        "model not validated — informational only",
        "This system never places bets",
        "informational",
    ]
    for s in required:
        assert s in text, s


def test_shared_display_formatters_and_row_open() -> None:
    """Fixes 2026-07-10 #2/#4/#9: ONE shared market-key formatter, a
    display-only team-typo map (never resolution/alias code), and ONE shared
    row-open mechanism reusing the Edges hash router."""
    text = _text()
    assert "function marketLabel" in text
    assert '"Moneyline / H2H"' in text
    assert '"Double Chance"' in text
    assert '"Both Teams To Score"' in text
    assert "function eventLabel" in text
    assert '"Abroath": "Arbroath"' in text
    assert "function makeRowOpenPick" in text
    # the router-driven open: navigate to #/edges/<id>, never a second panel
    assert '"#/edges/" + String(pick.id)' in text


def test_quarantine_and_trust_banner_pins() -> None:
    """Fixes 2026-07-10 #3/#21/#22/#23: quarantined rows stay visible but
    excluded from rankings; the detail ticket surfaces the untrusted state."""
    text = _text()
    assert "function isRankable" in text
    assert "is-quarantined" in text
    assert "is-neg-closed" in text
    assert "Internally inconsistent — excluded, do not bet" in text
    assert "Untrusted / stale pricing — indicative only, not a recommended bet." in text
    assert "indicative, unverified" in text
    assert "informational only — not applicable while untrusted" in text


def test_independent_counts_never_joined_with_slash() -> None:
    """Fix 2026-07-10 #1: n_snapshot_close / n_fallback_close are independent
    counts — rendered as two labelled values, never "X / Y"."""
    text = _text()
    assert "Snapshot vs fallback closes" not in text
    assert '"snapshot " + (q.n_snapshot_close || 0) + " · fallback "' in text


def test_today_layout_and_edges_sport_subheaders() -> None:
    """Fixes 2026-07-10 #25/#26/#27/#28: the edge-magnitude bar chart is gone
    (ranked list stays), Today flows as two INDEPENDENT columns so panels size
    to content (Evidence Position compact), event names wrap instead of
    truncating, and the Edges sport ordering is legible via per-sport mono
    subheaders using the serialized sport_label vocabulary."""
    text = _text()
    # 25 — bar chart fully removed
    assert 'id="edge-chart"' not in text
    assert "edgechart" not in text
    assert "edge magnitude" not in text
    assert 'id="top-edges"' in text  # ranked numeric list preserved
    # 26/27 — independent columns; panels no longer row-height-locked
    assert text.count('class="today-col"') == 2
    assert 'data-testid="panel-evidence-position"' in text
    # 28 — visible per-sport subheaders driven by the serialized label
    assert "edge-sport-h" in text
    assert "p.sport_label" in text
    assert "function sportRank" in text


def test_trusted_clv_rule_pins() -> None:
    text = _text()
    assert "close_independent_of_fill === false" in text
    assert "sharp_stake_weighted_clv_log" in text
    assert "sharp_status" in text
    assert "n_sharp_close" in text
    assert "min_headline_n" in text
    assert "TRUSTED_CLOSE_ANCHORS" in text
    assert '"pinnacle"' in text
    assert '"sharp"' in text
    # AH-1 (audit 2026-07-26): the per-pick badge must require a GENUINE
    # snapshot close — the fallback close writer stamps
    # close_independent_of_fill WITHOUT has_snapshot_close, so independence
    # alone overcounts 'Trusted CLV'.
    assert "p.has_snapshot_close === true" in text


def test_open_pick_without_real_close_never_wears_exclusion_chips() -> None:
    """Defect C (fix 2026-08-02): while a pick is OPEN (not settled/superseded)
    and closing_odds is NULL, closing_fair_probability is only the LIVE
    re-priced fair (at mint it equals model_probability), so the tautology/
    exclusion read is meaningless — the provenance area must show the existing
    'no close recorded' state, and exclusion chips may render only once a real
    close exists or the pick is closed. Trusted-CLV gating (isTrustedClv /
    clvExclusionOf) stays byte-identical — this is a display-state guard only."""
    text = _text()
    # The pre-close guard exists and clvStateLabel consults it FIRST.
    assert "function closePending(p)" in text
    assert "p.closing_odds == null" in text
    label_fn = text.split("function clvStateLabel(p)", 1)[1]
    assert label_fn.index("closePending(p)") < label_fn.index("clvExclusionOf(p)")
    # The indicative CLV% value is suppressed while the close is pending.
    assert "!closePending(p) && p.clv_log != null" in text
    # Trusted gating unchanged: the exclusion read itself has no pending guard.
    excl_fn = text.split("function clvExclusionOf(p)", 1)[1].split("function ", 1)[0]
    assert "closePending" not in excl_fn


def test_clv_fabrication_mirror_nets_exchange_commission() -> None:
    """AH-2 (audit 2026-07-26): the JS clvIsFabricated mirror must judge the same
    COMMISSION-NETTED effective fill as the backend _clv_row_is_fabricated
    (app/edge/value.py effective_odds) — raw 1/decimal_odds understates the
    implied probability on exchange fills. Pin JS/Python constant parity so a
    backend commission change breaks this contract instead of silently drifting."""
    from app.edge.value import EXCHANGE_COMMISSION

    text = _text()
    assert "EXCHANGE_COMMISSION" in text
    assert "function effectiveOddsOf" in text
    for book, rate in EXCHANGE_COMMISSION.items():
        assert f'"{book}": {rate}' in text, book


def test_edge_copy_uses_mint_edge_for_closed_picks() -> None:
    """AH-3 (audit 2026-07-26): the drawer edge-copy clipboard text applies the
    SAME closed-pick guard as the list (mint edge for closed picks, never a live
    re-priced current_edge on a finished market): list edgeVal x2 + copy handler."""
    text = _text()
    guard = 'edgeGroupOf(p) === "closed" || p.current_edge == null ? p.edge : p.current_edge'
    assert text.count(guard) >= 3


def test_nav_contract_five_views() -> None:
    text = _text()
    for view in ("today", "edges", "radar", "lab", "sources"):
        assert text.count('data-view="' + view + '"') >= 2  # rail + dock
        assert 'id="view-' + view + '"' in text
    assert 'aria-label="Sections"' in text
    assert "aria-current" in text


def test_accessibility_markers() -> None:
    text = _text()
    assert "<h1" in text
    assert "<h2" in text
    assert "aria-live" in text
    assert 'role="alert"' in text
    assert ":focus-visible" in text
    assert "@media (prefers-reduced-motion: reduce)" in text
    assert 'id="edge-list" role="list"' not in text
    assert 'setAttribute("role", "listitem")' not in text
    assert 'id="system-popover" class="popover" role="dialog"' in text
    assert 'tabindex="-1" hidden' in text
    assert 'ev.key === "Escape" && !$("system-popover").hidden' in text


def test_initial_kpi_skeleton_reserves_hydrated_layout() -> None:
    text = _text()
    assert 'id="view-today" data-view-key="today" aria-busy="true"' in text
    assert 'id="today-stats" aria-busy="true"' in text
    assert text.count('class="stat stat-loading"') == 6
    assert 'stripBox.setAttribute("aria-busy", "false")' in text
    assert '$("view-today").setAttribute("aria-busy", "false")' in text
    assert ".stat-loading > * { visibility: hidden; }" in text


def test_mobile_lab_rows_wrap_without_document_overflow() -> None:
    text = _text()
    assert ".lab-board .kickoff-row { grid-template-columns: minmax(0, 1fr); }" in text
    assert ".lab-board .kickoff-row .kr-t" in text


def test_mobile_breakpoints_and_dock_touch_targets() -> None:
    text = _text()
    # Responsive breakpoints exist (the nav is now mobile-first: the desktop
    # top-bar is gated behind a min-width query, the dock is the mobile default).
    assert "@media (min-width:" in text or "@media (max-width:" in text
    dock_block = text[text.index(".dock button {") : text.index(".dock button {") + 400]
    assert "min-height: 44px" in dock_block


def test_no_hardcoded_timezone() -> None:
    assert "Asia/Nicosia" not in _text()


def test_fmt_helper_and_missing_value_dash() -> None:
    text = _text()
    assert "function fmt(v)" in text
    assert '"—"' in text


def test_review_queue_browse_is_lazy_collapsed_disclosure() -> None:
    """B6: the Sources-view review-queue browse is a collapsed-by-default
    <details> that lazy-fetches GET /resolution/review-queue on expand with
    the match-rate timeout guard — never in the boot-time loadOnce() cycle,
    and never with an <details open> default."""
    text = _text()
    assert 'id="reviewq-browse"' in text
    assert "Review queue — newest" in text
    disclosure = text[
        text.index('id="reviewq-browse"') - 200 : text.index('id="reviewq-browse"') + 100
    ]
    assert "<details" in disclosure
    assert "open" not in disclosure.split("<details", 1)[1].split(">", 1)[0]
    assert re.search(r'fetchGuarded\("/resolution/review-queue[^)]*MATCH_RATE_TIMEOUT_MS', text)
    assert "function loadReviewQueue" in text
    load_once = text[
        text.index("async function loadOnce") : text.index("async function loadOnce") + 2000
    ]
    assert "/resolution/review-queue" not in load_once
    # honest states, read-only wording
    assert "Loading review queue…" in text
    assert "Could not load review queue." in text
    assert "Review queue is empty." in text


def test_promotion_distance_widget_is_lazy_and_min_n_safe() -> None:
    """B1: the Lab evidence-distance widget lazy-loads GET /lab/promotion-distance
    (guarded fetch, never in the boot-time loadOnce() cycle), renders explicit
    loading/error/empty states, shows progress toward the ok threshold plus a
    days-to-threshold estimate ("—" without cadence), renders a CLV point
    estimate ONLY at/above the min-n bar, and never implies promotion —
    promotion stays gated by SportMarketClvGate + operator sign-off."""
    text = _text()
    assert 'id="promo-distance"' in text
    assert "function loadPromotionDistance" in text
    assert re.search(r'fetchGuarded\("/lab/promotion-distance"', text)
    load_once = text[
        text.index("async function loadOnce") : text.index("async function loadOnce") + 2000
    ]
    assert "/lab/promotion-distance" not in load_once
    # honest states
    assert "Loading promotion distance…" in text
    assert "Could not load promotion distance." in text
    assert "No settled sport/market cells yet." in text
    # progress + days-to-threshold with an explicit "—" no-cadence state
    assert "trusted closes" in text
    assert "days to threshold" in text
    # the point estimate is double-guarded on the min-n bar (the payload also
    # nulls sub-floor estimates at the source)
    assert re.search(
        r'c\.status === "ok" && Number\(c\.n_trusted\) >= okN && c\.mean_clv_log != null', text
    )
    # distance-to-evidence framing only — never "promotion imminent"
    assert "Promotion stays gated by SportMarketClvGate and operator ADR sign-off." in text


def test_close_quality_by_sport_renders_from_performance_payload() -> None:
    """B2: the per-sport close-quality breakdown renders straight from the
    existing /performance payload (by_sport[*].clv_quality) — no new fetch —
    keyed on the persisted close-exclusion reasons, with an honest empty state."""
    text = _text()
    assert 'id="closeq-sport"' in text
    assert "function renderCloseQualityBySport" in text
    assert "close_exclusion_reasons" in text
    assert "n_close_reason_known" in text
    assert "No per-sport close-reason data yet." in text
    assert "Could not load performance data." in text


def test_close_quality_by_sport_trusted_stamp_relabelled() -> None:
    """Task 4 2b (2026-07-10): the persisted close_exclusion_reason 'trusted'
    stamp has LOOSER semantics (no exclusion guard tripped at close-write, small
    recent-rows denominator) than the trusted-sharp subset behind the SLA and
    evidence-distance panels. The panel token is relabelled 'no guard tripped'
    and a footnote distinguishes the two, so the counts cannot be misread."""
    text = _text()
    assert '"no guard tripped"' in text
    assert 'k === "trusted" ? "no guard tripped"' in text
    # the footnote drawing the distinction
    assert "not the trusted-sharp subset" in text


def test_tier_scorecard_trusted_clv_first_rows() -> None:
    """Task 4 step 3 (2026-07-10): the Premium-vs-Shadow scorecard leads with
    the decision instrument — per-tier trusted CLV with 95% CI and n, the
    CLV→yield calibration ratio against the RebelBetting public 0.8× benchmark,
    and the plain-language evidence verdict — all read from the /performance
    live_evidence payload (trusted_clv_ci / clv_yield_ratio / evidence_verdict,
    already nulled at the source below the honesty floor)."""
    text = _text()
    assert 'id="tier-scorecard"' in text
    assert "trusted_clv_ci" in text
    assert "clv_yield_ratio" in text
    assert "evidence_verdict" in text
    assert "Trusted CLV — " in text
    assert "CLV→yield ratio" in text
    assert "benchmark 0.8×" in text
    assert "Verdict: " in text
    # honest states: pre-payload and sub-floor renders
    assert "Trusted-CLV scorecard not yet reported." in text
    assert "not computable — below floor or trusted CLV ≈ 0" in text
    assert '" — insufficient"' in text


def test_close_coverage_sla_renders_from_performance_payload() -> None:
    """Audit #8: the per sport-market CLOSE/FRESHNESS SLA panel renders straight
    from the existing /performance payload (close_coverage_sla) — no new fetch —
    flags the CLV/ROI CLAIM (not the picks) when coverage is below the SLA, with
    an honest empty state."""
    text = _text()
    assert 'id="close-sla"' in text
    assert "function renderCloseCoverageSla" in text
    assert "close_coverage_sla" in text
    # the claim is flagged, never the picks
    assert "CLV unreliable" in text
    assert "No settled picks yet." in text
    assert "Could not load performance data." in text


def test_match_ceiling_is_lazy_collapsed_disclosure() -> None:
    """B3: the Sources match-ceiling decomposition is a collapsed-by-default
    <details> that lazy-fetches GET /resolution/match-ceiling on expand with the
    match-rate timeout guard — never in the boot-time loadOnce() cycle, never
    <details open>, and explicitly LIVE (never the static research artifact)."""
    text = _text()
    assert 'id="ceiling-browse"' in text
    disclosure = text[
        text.index('id="ceiling-browse"') - 200 : text.index('id="ceiling-browse"') + 100
    ]
    assert "<details" in disclosure
    assert "open" not in disclosure.split("<details", 1)[1].split(">", 1)[0]
    assert re.search(r'fetchGuarded\("/resolution/match-ceiling[^)]*MATCH_RATE_TIMEOUT_MS', text)
    assert "function loadMatchCeiling" in text
    load_once = text[
        text.index("async function loadOnce") : text.index("async function loadOnce") + 2000
    ]
    assert "/resolution/match-ceiling" not in load_once
    # honest states + honest framing of the decomposition
    assert "Loading match ceiling…" in text
    assert "Could not load match ceiling." in text
    assert "No events in the window." in text
    assert "Structural" in text
    assert "Addressable" in text
    assert "never from a static research artifact" in text


def test_steam_shadow_widget_counts_and_min_n_split() -> None:
    """B4: the Lab steam shadow-verdict summary renders the would-demote/clear/
    unevaluated counts, the mint-week trend, and a trusted-CLV split that obeys
    the same min-n discipline as B1: below the floor it reads "n=X —
    insufficient", never a point estimate. Monitor-only framing throughout."""
    text = _text()
    assert 'id="steam-shadow"' in text
    assert "function renderSteamShadow" in text
    assert "Would demote" in text
    assert "Unevaluated" in text
    assert "Would-demote by mint week:" in text
    # a pre-migration payload (steam_shadow null) has an explicit honest state
    assert "Not yet reported." in text
    # the split's point estimate is gated on sharp_status === "ok"; the
    # insufficient state carries the n instead
    assert re.search(
        r'agg\.sharp_status === "ok" && agg\.sharp_stake_weighted_clv_log != null', text
    )
    assert '" — insufficient"' in text
    # shadow verdicts are observability only — nothing is demoted
    assert "no pick is demoted" in text


def test_bankroll_tile_is_lazy_with_chart_and_honest_states() -> None:
    """B7: the Lab bankroll tile lazy-loads GET /bankroll (guarded fetch +
    TTL, never in the boot-time loadOnce() cycle), renders a running-balance
    line chart as inline SVG via createElementNS (no innerHTML, no chart lib)
    with a dashed running-peak line as the drawdown read, text stats
    (current balance / total settled P&L / max drawdown), and honest states —
    including the A8 inactive shape ("Bankroll ledger is not configured.")
    gated strictly on active !== true. Informational-only framing throughout;
    nothing here feeds staking and the system never places a bet."""
    text = _text()
    assert 'id="bankroll-body"' in text
    assert "function loadBankroll" in text
    assert re.search(r'fetchGuarded\("/bankroll"\)', text)
    load_once = text[
        text.index("async function loadOnce") : text.index("async function loadOnce") + 2000
    ]
    assert "/bankroll" not in load_once
    # honest states, incl. the inactive (unconfigured-ledger) shape
    assert "Loading bankroll…" in text
    assert "Could not load bankroll." in text
    assert "Bankroll ledger is not configured." in text
    assert "No ledger entries yet." in text
    assert re.search(r"b\.active !== true.*Bankroll ledger is not configured\.", text)
    # chart is inline SVG built safely — never innerHTML, never a library
    assert "function bankrollChartEl" in text
    assert 'document.createElementNS(SVG_NS, "svg")' in text
    assert 'document.createElementNS(SVG_NS, "polyline")' in text
    # drawdown indication: dashed running-peak line, never color-only (legend text)
    assert "stroke-dasharray" in text
    assert "running peak" in text
    # text stats consumed from the A8 payload
    assert "Current balance" in text
    assert "Total settled P&L" in text
    assert "Max drawdown" in text
    assert "balance_after" in text
    # informational-only caption — hypothetical, no bets placed by the system
    assert "Bankroll — hypothetical ledger" in text
    assert "informational only — this system never places a bet" in text


def test_pick_ticket_mint_now_provenance_labels() -> None:
    """2026-07-11 Task 3: the ticket labels value provenance — EV is fixed at
    mint; Edge and Fair odds are live re-priced values — plus a muted one-liner
    under Pricing distinguishing the mint-time fair/edge (archived in the raw
    reason summary) from the live re-priced fair."""
    text = _text()
    assert '"EV (at mint)"' in text
    assert '"Edge (now)"' in text
    assert '"Fair odds (now)"' in text
    # the bare, provenance-ambiguous KPI labels are gone
    assert 'mkKpi("EV",' not in text
    assert 'mkKpi("Edge",' not in text
    # the provenance one-liner under Pricing
    assert "re-priced live" in text


def test_volume_demotion_chips_parsed_from_reason_summary() -> None:
    """2026-07-11 Task 4: volume-tier picks render compact demotion-note chips
    parsed client-side from reason_summary's " | slug: …" segments (stake_zero,
    ml-filter, steam, non-major league, per-market floor …). Display only —
    no schema change, defensive parse."""
    text = _text()
    assert "function demotionChips" in text
    assert 'split(" | ")' in text
    assert "function demotionChipsEl" in text
    # chips only ever appear on the shadow/volume tier
    assert re.search(r'function demotionChips\(p\) \{\s*\n\s*if \(tierOf\(p\) !== "volume"', text)


def test_same_game_correlation_chip() -> None:
    """2026-07-11 Task 5: two or more OPEN premium picks sharing an event_id
    each show a 'correlated: N picks this game' chip — display only, staking
    unchanged."""
    text = _text()
    assert "function correlatedPremiumCount" in text
    assert '"correlated: " + n + " picks this game"' in text
    assert "staking unchanged" in text


def test_tier_scorecard_premium_cohorts_and_mc_null_line() -> None:
    """2026-07-11 Tasks 1+6: the trusted-CLV scorecard reports the ADR-0022
    crit-3/4 premium pre-/post-selection-fix cohort rows (same entry shape,
    nulled below the floor) and the Buchdahl-MCoB zero-edge-null Monte Carlo
    line with its honest insufficient state."""
    text = _text()
    assert "premium_cohorts" in text
    assert "pre-fix, minted < 2026-07-07" in text
    assert "post-fix, minted ≥ 2026-07-07" in text
    assert '"Record vs zero-edge null: p = "' in text
    assert '"Record vs zero-edge null: n="' in text  # insufficient state


def test_claims_ledger_trusted_close_eta_line() -> None:
    """2026-07-11 Task 2: the trusted-sharp-closes tile answers "when will this
    move" — one muted line projected from the recent trusted-close rate and the
    open premium pipeline, with every component nulled honestly server-side."""
    text = _text()
    assert "trusted_close_eta" in text
    assert "open premium awaiting kickoff" in text
    assert "no honest ETA yet" in text


def test_promotion_readiness_rows_rendered() -> None:
    """2026-07-12 Task 1: ADR-0022 crit-2 promotion-readiness rows, one per
    accruing sport/market cell, with the honest not-yet-instrumented state for
    source agreement + freshness (null, never fabricated)."""
    text = _text()
    assert "promotion_readiness" in text
    assert '"Promotion readiness — "' in text
    assert '"CI>0 pending"' in text
    assert '"NOT READY"' in text
    assert "not yet instrumented" in text


def test_shrink_review_line_under_tier_scorecard() -> None:
    """2026-07-12 Task 2: ADR-0022 crit-5 uncertainty-shrink 30-day review —
    one muted line under the tier scorecard driven by /performance
    shrink_review (estimates arrive nulled below the n=10 floor)."""
    text = _text()
    assert "shrink_review" in text
    assert '"Shrink shadow review: "' in text
    assert "n_annotated" in text
    assert "review_due" in text


def test_kill_gate_progress_element() -> None:
    """2026-07-12 Task 3: ADR-0022 crit-3 kill/keep gate — post-fix premium
    trusted-close progress in the claims-ledger area, with the PROGRESS 95% CI
    rendered only once the server sends it (n >= 10)."""
    text = _text()
    assert '"Kill/keep gate: post-fix premium trusted closes "' in text
    assert "progress_ci_low" in text
    assert "progress_ci_high" in text


def test_close_age_histogram_rendered_with_capture_caveat() -> None:
    """2026-07-12 Task 4: per-close-anchor close-age histogram in the Close
    Quality panel, with the honest caveat that the age is capture-time vs
    kickoff, not the market's true close."""
    text = _text()
    assert "close_age_histogram" in text
    assert "capture time vs kickoff" in text
    assert "by_anchor" in text


def test_response_deadline_covers_streamed_body_and_caps_json_size() -> None:
    """Headers alone never clear the request deadline; the JSON body is read
    incrementally, byte-capped, parsed, and only then releases the guard."""
    text = _text()
    assert "const responseGuards = new WeakMap()" in text
    assert "const MAX_JSON_BYTES = 4 * 1024 * 1024" in text
    assert "async function readJsonBody" in text
    assert "res.body.getReader()" in text
    assert "received > MAX_JSON_BYTES" in text
    assert '"PayloadTooLargeError"' in text
    assert "releaseResponseGuard(res)" in text
    assert "const [premiumBodyR, volumeBodyR, perfBodyR, healthBodyR]" in text
    assert 'fetchGuarded("/picks?limit=200&tier=premium")' in text
    assert '.then((res) => readJson(res, (body) => validatePicksPayload(body, "premium")))' in text
    assert 'fetchGuarded("/games?limit=1000")' in text
    assert '.then((res) => readJson(res, (body) => expectArrayPayload(body, "Games")))' in text
    assert 'fetchGuarded("/health").then((res) => readHealthJson(res))' in text
    # One raw network primitive remains, inside fetchGuarded only.
    assert text.count("fetch(") == 1


def test_health_contract_and_cold_start_fail_closed() -> None:
    text = _text()
    assert "function validateHealthPayload" in text
    # 2026-08-02: backend emits ok|partial on 200 and degraded on 503; the
    # validator must accept "partial" and the redacted anonymous body while
    # still failing closed on status/HTTP disagreement (behavioral coverage
    # in tests/test_dashboard_health_validator.py).
    assert 'httpStatus === 200 && health.status === "degraded"' in text
    assert 'httpStatus === 503 && health.status !== "degraded"' in text
    assert (
        'health.status !== "ok" && health.status !== "partial" && health.status !== "degraded"'
        in text
    )
    assert "typeof health.has_completed_poll" in text
    assert 'health.mode !== "picks-only"' in text
    assert "function healthHasCompletedPoll" in text
    assert "finishedAt <= Date.now()" in text
    assert "health.newest_poll_age_seconds === null" in text
    assert "Health payload contains an invalid poll record." in text
    assert "if (!healthHasCompletedPoll(health)) return true" in text
    assert "function healthIsTrusted" in text
    assert "no completed poll cycle yet" in text


def test_actionability_requires_current_structural_and_temporal_evidence() -> None:
    text = _text()
    actionable = text[text.index("function isActionable") : text.index("function isRankable")]
    assert "p.structural_sane === true" in actionable
    assert "state.premiumErr === null" in actionable
    assert "state.premiumLastGoodAt !== null" in actionable
    assert "hasFutureKickoff(p)" in actionable
    assert "hasQualifyingEdgeNow(p, health)" in actionable
    assert "healthIsTrusted(health)" in actionable
    edge_group = text[text.index("function edgeGroupOf") : text.index("function edgeFloorOf")]
    assert 'if (isActionable(p, state.health)) return "actionable"' in edge_group
    assert "const stakeGated = !isActionable(p, state.health)" in text
    assert "const MAX_FUTURE_TIMESTAMP_MS = 0" in text
    assert "age >= -MAX_FUTURE_TIMESTAMP_MS" in text


def test_tier_failures_retain_last_good_rows_and_raise_global_degraded_state() -> None:
    text = _text()
    for token in (
        "premiumErr",
        "volumeErr",
        "premiumLastGoodAt",
        "volumeLastGoodAt",
        "gamesLastGoodAt",
        "perfLastGoodAt",
        "healthLastGoodAt",
        "globalDegraded",
    ):
        assert token in text
    assert "premiumRows || oldPremium" in text
    assert "volumeRows || oldVolume" in text
    assert "Could not refresh premium picks — showing the last loaded rows" in text
    assert "Could not refresh volume picks — showing the last loaded rows" in text
    assert "cached premium rows cannot qualify" in text


def test_qualified_kpi_counts_full_set_but_is_unavailable_without_trust() -> None:
    text = _text()
    today = text[text.index("function renderToday") : text.index("// ===== EDGES")]
    assert (
        "const qualificationAvailable = state.premiumErr === null && healthIsTrusted(health)"
        in today
    )
    assert today.count('qualificationAvailable ? String(qualified.length) : "—"') >= 2
    # The display list is capped separately; the KPI never reads its length.
    assert ".slice(0, 5)" in today
    assert 'String(actionable.length), "Qualified now"' not in today


def test_csv_export_neutralizes_spreadsheet_formulas() -> None:
    text = _text()
    assert "function csvSafeCell" in text
    assert re.search(r"\[=\+\\-@\]", text)
    assert 'cell = "\'" + cell' in text
    assert "cols.map(csvSafeCell)" in text
    assert 'lines.join("\\r\\n")' in text


def test_edges_deep_link_is_an_accessible_modal_route() -> None:
    text = _text()
    assert 'role="dialog" aria-modal="true" aria-labelledby="edge-detail-title"' in text
    assert 'id="edge-backdrop"' in text
    assert "function syncDrawerFromRoute" in text
    assert 'selectedId = boot.view === "edges" ? boot.id : null' in text
    assert "rememberDrawerOpener" in text
    assert "restoreDrawerOpener" in text
    assert "requestDrawerClose" in text
    assert 'if (ev.key !== "Tab") return' in text
    assert 'detail.setAttribute("aria-hidden", "false")' in text
    assert 'detail.setAttribute("aria-hidden", "true")' in text


def test_refresh_preserves_focus_and_pauses_while_hidden_or_editing() -> None:
    text = _text()
    assert "function captureFocusState" in text
    assert "function restoreFocusState" in text
    assert 'row.dataset.focusKey = "pick-" + String(p.id)' in text
    assert "function operatorIsEditingResult" in text
    assert 'form.dataset.dirty === "true"' in text
    assert 'form.dataset.dirty = "true"' in text
    editing_guard = text[
        text.index("function operatorIsEditingResult") : text.index("// ===== boot")
    ]
    assert 'detail.getAttribute("aria-hidden") !== "false"' in editing_guard
    assert '!detail.classList.contains("open")' in editing_guard
    assert "document.hidden || operatorIsEditingResult()" in text
    assert 'document.addEventListener("visibilitychange"' in text


def test_system_condition_distinguishes_health_and_partial_refresh_failures() -> None:
    text = _text()
    condition = text[text.index("function systemCondition") : text.index("function renderGlobal")]
    assert "state.healthErr !== null || !health" in condition
    assert 'health.status === "degraded"' in condition
    assert "coreRefreshHasErrors()" in condition
    assert 'label: "Health unknown"' in condition
    assert 'label: "Source Degraded"' in condition
    assert 'label: "Data refresh degraded"' in condition
    assert 'label: "Health unverified"' in condition

    pill_start = text.index("function renderPill")
    pill = text[pill_start : text.index('$("system-pill").addEventListener', pill_start)]
    assert "systemCondition(health)" in pill
    assert "state.globalDegraded" not in pill


def test_mobile_has_visible_heading_zoom_safe_inputs_and_touch_targets() -> None:
    text = _text()
    assert '<div class="topbar-brand">' in text
    assert "<h1>sharp-ev-picks</h1>" in text
    mobile = text[
        text.index("@media (max-width: 1080px)") : text.index("@media (max-width: 960px)")
    ]
    assert "input, select, textarea { font-size: 16px !important; }" in mobile
    assert "min-height: 44px" in mobile
    assert "min-width: 44px" in mobile


def test_result_form_supports_prefill_enter_schema_and_specific_errors() -> None:
    text = _text()
    form = text[
        text.index("function validateSettlementPayload") : text.index("function renderEdgeDetail")
    ]
    assert 'document.createElement("form")' in form
    assert 'submit.type = "submit"' in form
    assert 'form.addEventListener("submit"' in form
    assert "p.scraped_score.match" in form
    assert "validateSettlementPayload" in form
    assert "Result recorded — " in form
    assert "picks settled." in form
    assert "Could not record result. No answer within 15s." in form
    assert "Could not record result. (HTTP " in form
    assert "Could not record result. Network error." in form
    assert 'note.setAttribute("aria-live", "polite")' in form


def test_cached_core_and_lazy_panels_surface_refresh_failures() -> None:
    text = _text()
    for marker in ("radar-cache-notice", "lab-cache-notice", "sources-cache-notice"):
        assert f'id="{marker}"' in text
    for copy in (
        "Could not refresh review queue — showing last loaded data.",
        "Could not refresh promotion distance — showing last loaded data.",
        "Could not refresh bankroll — showing last loaded data.",
        "Could not refresh match ceiling — showing last loaded data.",
        "Could not refresh performance data — showing last loaded evidence",
        "Could not refresh games — showing last loaded fixtures",
    ):
        assert copy in text


def test_evidence_cells_panel_league_tier_gaps_and_bb_spreads_progress() -> None:
    """TASK DASH 1 (2026-07-26): the Lab EVIDENCE panel renders the ADDITIVE
    /performance live_evidence fields — basketball-spreads trusted-close
    progress toward the 100-close promotion-consideration bar, the
    major/non-major league-tier trusted-CLV split (95% CI + n, insufficient
    below the floor via the ONE shared CI formatter), and the per-(sport,
    market) trusted-close coverage gap cells — all with honest
    insufficient/empty states and NO new fetch."""
    text = _text()
    assert 'id="evidence-cells"' in text
    assert "function renderEvidenceCells" in text
    assert "trusted_close_gap_by_sport_market" in text
    assert "by_league_tier" in text
    assert "const BASKETBALL_SPREADS_PROMOTION_N = 100" in text
    assert "Basketball spreads — promotion progress" in text
    # honest states for every sub-block
    assert "No settled basketball-spreads picks yet." in text
    assert "Close-coverage cells not yet reported." in text
    assert "League-tier split not yet reported." in text
    # the split labels + shared CI-entry formatter (nulled below the floor)
    assert "Trusted CLV — major leagues" in text
    assert "Trusted CLV — non-major (obscure) leagues" in text
    assert "function ciEntryText" in text
    assert "missing trusted close" in text
    # never a promotion — measurement framing stays explicit
    assert "Progress toward the promotion-consideration bar only" in text


def test_gate_reason_widget_lazy_states_and_new_slugs() -> None:
    """TASK DASH 2 (2026-07-26): the 'why picks did not mint' widget is a lazy
    Lab loader targeting the proposed read-only GET /lab/gate-reasons (no
    backend route exposes candidate_evaluations reason counts yet — the widget
    renders an explicit honest state on HTTP 404 until it ships), never in the
    boot-time loadOnce() cycle, and decomposes no_sharp_anchor:* sub-reasons
    under their parent slug."""
    text = _text()
    assert 'id="gate-reasons"' in text
    assert "function renderGateReasons" in text
    assert "function loadGateReasons" in text
    assert re.search(r'fetchGuarded\("/lab/gate-reasons"\)', text)
    load_once = text[
        text.index("async function loadOnce") : text.index("async function loadOnce") + 2000
    ]
    assert "/lab/gate-reasons" not in load_once
    # honest states, incl. the explicit route-not-yet-deployed state on 404
    assert "Loading gate reasons…" in text
    assert "Could not load gate reasons." in text
    assert "Gate-reason telemetry endpoint is not available yet" in text
    assert "No candidate evaluations in the window." in text
    assert "Could not refresh gate reasons — showing last loaded data." in text
    # no_sharp_anchor:<cause> sub-reasons group under the parent slug
    assert '"no_sharp_anchor:"' in text
    # the 2026-07 reason slugs are documented at the render site
    for slug in (
        "draw_selection_demotion",
        "consensus_anchor_dropped",
        "tennis_game_line_unsettleable",
        "global_odds_ceiling",
    ):
        assert slug in text, slug
    # measurement-only framing — a demoted candidate still mints in shadow
    assert "still mints in the shadow tier" in text


def test_mint_lead_hours_on_card_and_ticket() -> None:
    """TASK DASH 3 (2026-07-26): mint lead (hours to kickoff at mint) on the
    ticket + the card kickoff cell's tooltip. Prefers the server-persisted
    hours_to_kickoff once /picks serializes it (the Pick column exists but is
    NOT in the payload yet); until then derived from created_at → starts_at
    and labelled derived — provenance is never silent."""
    text = _text()
    assert "function mintLeadHours" in text
    assert "hours_to_kickoff" in text
    assert '"Mint lead"' in text
    assert "before kickoff" in text
    assert "derived from mint and kickoff times" in text
    assert '"minted ~"' in text


def test_needs_attention_folds_unsettleable_league_gaps() -> None:
    """TASK DASH 4 (2026-07-26): the per-league unsettleable/results-gap
    backlog folds into the Today needs-attention queue, rendered ONLY when
    /health serializes it (unsettleable_leagues — the settlement engine's
    currently logs-only report); an absent field renders nothing, never a
    fabricated row."""
    text = _text()
    assert "unsettleable_leagues" in text
    assert '"Results gap — "' in text
    assert "picks past kickoff with no result source." in text
    assert "more leagues with results gaps." in text


def test_trusted_clv_odds_band_split_cells() -> None:
    """Live review 2026-08-02 (item 1): the Lab renders the trusted subset's
    odds-band split (< / >= threshold) beside the trusted-CLV hero via the ONE
    shared CI formatter — estimates arrive nulled at the source below the
    min-n floor, so a sub-floor band reads "n=X — insufficient"."""
    text = _text()
    assert "sharp_clv_odds_split" in text
    assert "threshold_odds" in text
    assert '"Trusted CLV — odds < " + thr' in text
    assert '"Trusted CLV — odds ≥ " + thr' in text
    assert "ciEntryText(oddsSplit.below || {})" in text
    assert "ciEntryText(oddsSplit.at_or_above || {})" in text
    assert "Odds-band split of the same trusted subset" in text


def test_blended_clv_labelled_non_evidential() -> None:
    """Live review 2026-08-02 (item 2): the blended (consensus-close) CLV is
    labelled NON-EVIDENTIAL unmissably and stays muted context — the old
    soft "context — not evidence" phrasing alone is retired."""
    text = _text()
    assert "NON-EVIDENTIAL (consensus closes; tautology-dominated)" in text
    assert "never compare it to the trusted figure above" in text
    assert "perf.blended_clv_evidential !== true" in text
    assert "All-closes CLV (context — not evidence):" not in text


def test_calibration_cell_states_drift_detector_status() -> None:
    """Live review 2026-08-02 (item 3): the Lab calibration cell states the
    walk-forward drift detector's state explicitly — fold count, sample
    requirement and the honest testable-by ETA when one exists; 0 folds is
    never a silent green."""
    text = _text()
    assert "calibration_drift" in text
    assert "Walk-forward drift detector: insufficient data (" in text
    assert "no drift verdict yet" in text
    assert "projected_testable_date" in text
    assert '"; testable ~" + cd.projected_testable_date' in text
    assert "Walk-forward drift detector: unavailable" in text
    assert "DRIFT — recalibration warranted OOS (" in text
    assert "identity calibration holds OOS (" in text


def test_anchor_restated_cohort_chip() -> None:
    """Live review 2026-08-02 (item 4): picks flagged anchor_restated=true by
    the /picks payload chip the drawer's evidence pane — the mint anchor_book
    was mislabeled sharp and restated to 10bet by the 2026-08-02 label audit."""
    text = _text()
    assert "p.anchor_restated === true" in text
    assert "ANCHOR RESTATED (was mislabeled sharp)" in text
    assert "2026-08-02 label audit" in text


def test_volume_demotion_cause_chips_canonical_vocabulary() -> None:
    """Live review 2026-08-02 (item 5): volume cards/drawers distinguish WHY
    they are volume with canonical demotion-cause chips mapped from the
    reason_summary segments already in the payload; shadow telemetry notes
    (steam(shadow)/shots(shadow)) are never presented as demotion causes."""
    text = _text()
    assert "DEMOTION_CHIP_MAP" in text
    for chip in (
        '"NO SHARP ANCHOR"',
        '"EXPERIMENTAL SPORT (shadow mandate)"',
        '"MARKET CAP (visibility-only)"',
        '"MARKET FLOOR"',
        '"ODDS CEILING"',
        '"STEAM"',
    ):
        assert chip in text, chip
    assert '["steam(shadow)", null]' in text
    assert '["shots(shadow)", null]' in text


def test_gate_reasons_overlap_counting_note() -> None:
    """Live review 2026-08-02 (item 6): the gate-reasons widget warns that the
    bare no_sharp_anchor slug and its :sub_reason slugs overlap (the same
    evaluation counts in both) so the operator never sums them."""
    text = _text()
    assert "Counting note: the bare no_sharp_anchor row and its " in text
    assert "do not sum them" in text


def test_closed_card_settled_pnl_has_own_non_truncating_line() -> None:
    """Live review 2026-08-02 (P1): settled P&L on closed Edges cards renders
    on its OWN full-width line (.er-pnl, nowrap, never ellipsized) — it was
    truncating to "P…" inside the selection cell; the selection keeps the
    ellipsis instead."""
    text = _text()
    assert 'pnlEl.className = "er-pnl mono"' in text
    assert "row.appendChild(pnlEl)" in text
    css = text[text.index(".er-pnl {") : text.index(".er-pnl {") + 300]
    assert "grid-column: 1 / -1" in css
    assert "white-space: nowrap" in css
    # the P&L text no longer rides the truncating .er-sel cell
    assert 'sel.appendChild(document.createTextNode(" · P&L' not in text


def test_settled_drawer_leads_with_mint_truth_not_live_tile() -> None:
    """Live review 2026-08-02 (P2): settled/superseded drawers lead with the
    mint edge/EV + result KPIs; the live "Edge (now)" tile and the stake tile
    are for open picks only, and the "not a recommended bet" stale-pricing
    banner is suppressed once settled/superseded."""
    text = _text()
    assert 'const postSettlement = p.status === "settled" || p.status === "superseded"' in text
    assert "if (untrusted && !postSettlement) {" in text
    assert '"Edge (at mint)"' in text
    assert 'mkKpi("Result"' in text
    # mint figures on a settled pick are archived truth — never muted
    assert "!postSettlement && (untrusted ||" in text


def test_drawer_copy_and_csv_carry_bookmaker() -> None:
    """Live review 2026-08-02 (P2): the fill bookmaker renders in the drawer
    Pricing block, the Copy summary, and as a CSV export column."""
    text = _text()
    assert 'metricEl("Bookmaker"' in text
    assert '"Book: " + fmt(p.bookmaker)' in text
    assert '"selection", "bookmaker", "decimal_odds"' in text


def test_radar_band_named_next_24h() -> None:
    """Live review 2026-08-02 (P3): the 2-24h radar band is a rolling window,
    not a calendar day — named "Next 24h", never "Today"."""
    text = _text()
    assert '["Next 24h", (ms) => ms > 2 * 3.6e6' in text
    assert '["Today"' not in text


def test_mobile_pill_age_and_ops_timestamp_and_ceiling_skeleton() -> None:
    """Live review 2026-08-02 (P3 cosmetics): the status pill's data-age rides
    a .pill-age span hidden on narrow phones (the condition label never
    truncates); review-queue Queued/Status timestamps format via the UTC ops
    helper (no raw microsecond ISO); the match-ceiling table shows skeleton
    rows while loading instead of a bare header."""
    text = _text()
    assert 'age.className = "pill-age"' in text
    assert ".pill-age { display: none; }" in text
    assert "function fmtUtcTs" in text
    assert "fmtUtcTs(r.created_at)" in text
    assert "fmtUtcTs(r.reviewed_at)" in text
    ceiling = text[
        text.index("function renderMatchCeiling") : text.index("function loadMatchCeiling")
    ]
    assert "skeleton-line" in ceiling
    # Lab evidence rows + recent results wrap at word boundaries, no mid-word
    # ellipsis / ragged wraps
    assert ".lab-board .kickoff-row .kr-s" in text
    assert "#recent-results .kr-s" in text


def test_first_load_retries_once_on_timeout_only() -> None:
    """Live review 2026-08-02 (P4): the boot-time core fetches retry exactly
    once on a timeout-class failure (first-load /performance //picks aborts),
    never on HTTP/schema errors — bounded by construction."""
    text = _text()
    assert "function retryOnTimeout" in text
    assert "isTimeoutErr(e) ? makeReq() : Promise.reject(e)" in text
    for url in (
        '"/picks?limit=200&tier=premium"',
        '"/picks?limit=200&tier=volume"',
        '"/performance"',
        '"/health"',
        '"/games?limit=1000"',
    ):
        assert "retryOnTimeout(() => fetchGuarded(" + url in text, url


def test_csv_formula_guard_exempts_bare_numbers() -> None:
    """Live review 2026-08-02 (P4, OWASP): the CSV formula-injection guard
    applies only to formula-leading STRINGS — a bare number like -0.045 is a
    number to spreadsheets and must not be apostrophe-prefixed."""
    text = _text()
    assert "const plainNumber = /" in text
    assert "!plainNumber &&" in text


def test_coverage_incomplete_notice_never_stacked_twice() -> None:
    """2026-08-02: during coverage-only partial status the SAME copy rendered in
    BOTH the global degraded banner and the stale banner directly below it. The
    stale banner must suppress itself when the global banner already carries the
    coverage-incomplete notice; every other stale-banner case keeps the existing
    fail-closed gating unchanged."""
    text = _text()
    # Single shared constant — the copy never forks into duplicated literals.
    assert text.count("Source coverage incomplete") == 1
    assert "globalCoverageNoticeShown" in text
    assert "duplicateOfGlobal" in text


def test_value_gone_chip_on_open_premium_failing_edge_gate() -> None:
    """Operator item 2 (2026-08-02): an OPEN premium pick whose live edge fails
    the qualifying-edge gate (hasQualifyingEdgeNow) must carry an explicit
    "VALUE GONE" chip adjacent to the tier chip on the card AND the drawer —
    a bare PREMIUM chip on a −2.3%-edge row (e.g. pick 946692) is misleading.
    Display only: built from the existing hasQualifyingEdgeNow/edgeFloorOf
    gates; isActionable/gating byte-identical; settled/superseded unchanged."""
    text = _text()
    # ONE shared chip literal (never forked copies) rendered from exactly TWO
    # call sites: the card row and the drawer ticket head.
    assert text.count("VALUE GONE — no longer qualifies") == 1
    # One builder definition + exactly two call sites (card row, drawer head).
    assert text.count("function valueGoneChipEl") == 1
    assert text.count("valueGoneChipEl") == 3
    assert "function valueGone(" in text
    # The predicate reuses the existing gate, never a re-derived edge rule.
    assert "hasQualifyingEdgeNow(p, health)" in text
    # Never on settled/superseded/started rows: the predicate is scoped to
    # open (alerted, pre-kickoff) premium rows only.
    assert re.search(r"function valueGone\([^)]*\)\s*\{[^}]*\"alerted\"", text)
