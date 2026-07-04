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
    assert "SignalDesk" in text


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


def test_proxy_pool_rendered_from_health_payload() -> None:
    text = _text()
    assert "function renderProxyRow" in text
    assert re.search(r"renderProxyRow\(\s*health\s*&&\s*health\.proxy_pool", text)
    assert "Proxy pool degraded" in text
    assert "Proxy pool healthy" in text


def test_401_redirects_with_authentication_required_message() -> None:
    text = _text()
    assert "Authentication required." in text
    assert "function authRequired" in text
    assert 'window.location.assign("/login")' in text


def test_stale_notice_copy() -> None:
    assert "Odds data is stale. Picks should not be treated as current." in _text()


def test_required_empty_states() -> None:
    text = _text()
    assert "Nothing needs attention." in text
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
        "DO-NOT-RUN",
        "Source Degraded",
        "model not validated — informational only",
        "This system never places bets",
        "informational",
    ]
    for s in required:
        assert s in text, s


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


def test_mobile_breakpoints_and_dock_touch_targets() -> None:
    text = _text()
    assert "@media (max-width:" in text
    dock_block = text[text.index(".dock button {") : text.index(".dock button {") + 400]
    assert "min-height: 44px" in dock_block


def test_no_hardcoded_timezone() -> None:
    assert "Asia/Nicosia" not in _text()


def test_fmt_helper_and_missing_value_dash() -> None:
    text = _text()
    assert "function fmt(v)" in text
    assert '"—"' in text
