"""Every configured oddsportal market key verified against the INSTALLED
oddsharvester 0.3.0 — its sport_market_constants.py is the documentation of
record (ADR-0012).

Three guarantees per configured key, parametrized over the exact config
default strings so config drift breaks loudly:
1. the key exists in the sport's upstream market enums;
2. the key is registered in the upstream SportMarketRegistry (what the
   scraper actually dispatches on);
3. the upstream odds labels for the key are EXACTLY the labels our
   _selections mapping reads from match dicts — a silent label mismatch
   would drop every snapshot of that market.

Needs the backfill extra (same skip rule as test_oddsportal_patches.py).
"""

from collections.abc import Callable
from typing import Any

import pytest

pytest.importorskip(
    "oddsharvester",
    reason="oddsharvester not installed — run 'uv sync --extra backfill' to cover these checks",
)

from app.config import Settings  # noqa: E402
from app.ingestion.oddsportal import _selections  # noqa: E402

CONFIGURED_FOOTBALL_KEYS = tuple(
    Settings.model_fields["oddsportal_football_markets"].default.split(",")
)
# JSON-feed-only WILDCARD families: one GET enumerates every half-line
# (oddsportal_json). They are DELIBERATELY not oddsharvester upstream keys — the
# Playwright fallback does not scrape them (basketball there is moneyline-only) —
# so they're excluded from the upstream-existence guarantees below and asserted
# separately in test_json_wildcard_families_are_configured_but_not_upstream.
_JSON_WILDCARD_KEYS = frozenset({"over_under_games", "asian_handicap_games"})
CONFIGURED_BASKETBALL_KEYS = tuple(
    k
    for k in Settings.model_fields["oddsportal_basketball_markets"].default.split(",")
    if k not in _JSON_WILDCARD_KEYS
)


def _football_upstream_keys() -> set[str]:
    from oddsharvester.utils.sport_market_constants import (
        FootballAsianHandicapMarket,
        FootballEuropeanHandicapMarket,
        FootballMarket,
        FootballOverUnderMarket,
    )

    return {
        member.value
        for enum_cls in (
            FootballMarket,
            FootballOverUnderMarket,
            FootballAsianHandicapMarket,
            FootballEuropeanHandicapMarket,
        )
        for member in enum_cls
    }


def _basketball_upstream_keys() -> set[str]:
    from oddsharvester.utils.sport_market_constants import (
        BasketballAsianHandicapMarket,
        BasketballMarket,
        BasketballOverUnderMarket,
    )

    return {
        member.value
        for enum_cls in (BasketballMarket, BasketballOverUnderMarket, BasketballAsianHandicapMarket)
        for member in enum_cls
    }


def _registered_mapping(sport: str) -> dict[str, Callable[..., Any]]:
    from oddsharvester.core.sport_market_registry import (
        SportMarketRegistrar,
        SportMarketRegistry,
    )

    SportMarketRegistrar.register_all_markets()  # idempotent (dict update)
    return SportMarketRegistry.get_market_mapping(sport)


def _upstream_odds_labels(market_lambda: Callable[..., Any]) -> list[str]:
    """odds_labels captured in the registry lambda's closure — the exact
    label list the upstream parser emits per bookmaker row."""
    closure = market_lambda.__closure__ or ()
    free = dict(zip(market_lambda.__code__.co_freevars, closure, strict=True))
    labels = free["odds_labels"].cell_contents
    assert labels is not None, "registry lambda has no odds_labels"
    return list(labels)


@pytest.mark.parametrize("key", CONFIGURED_FOOTBALL_KEYS)
def test_configured_football_key_exists_upstream_with_matching_labels(key: str) -> None:
    assert key in _football_upstream_keys()
    mapping = _registered_mapping("football")
    assert key in mapping
    ours = [label for label, _ in _selections(key, "Home", "Away")]
    assert ours == _upstream_odds_labels(mapping[key])


@pytest.mark.parametrize("key", CONFIGURED_BASKETBALL_KEYS)
def test_configured_basketball_key_exists_upstream_with_matching_labels(key: str) -> None:
    assert key in _basketball_upstream_keys()
    mapping = _registered_mapping("basketball")
    assert key in mapping
    ours = [label for label, _ in _selections(key, "Home", "Away")]
    assert ours == _upstream_odds_labels(mapping[key])


def test_json_wildcard_families_are_configured_but_not_upstream_keys() -> None:
    """The bare wildcard families enumerate every half-line from ONE JSON-feed GET
    (oddsportal_json), so they are JSON-feed-only and DELIBERATELY not oddsharvester
    upstream keys — the Playwright fallback does not scrape them (basketball there is
    moneyline-only). Guard all three facts so the divergence stays explicit, not silent."""
    from app.ingestion.oddsportal_json import _WILDCARD_FAMILIES

    configured = set(Settings.model_fields["oddsportal_basketball_markets"].default.split(","))
    assert configured >= _JSON_WILDCARD_KEYS  # they ARE the configured basketball markets
    assert set(_WILDCARD_FAMILIES) >= _JSON_WILDCARD_KEYS  # recognized JSON wildcard families
    assert not (_JSON_WILDCARD_KEYS & _basketball_upstream_keys())  # but NOT upstream keys
