"""ValuePolicy pure helpers: the empty policy is a strict no-op; overrides
match the line-qualified market key first, then the market family."""

from app.edge.value_policy import (
    ValuePolicy,
    devig_method_for,
    distinct_book_count,
    is_major_league,
    market_lookup_keys,
    min_books_for,
    min_edge_for,
    normalize_league,
    odds_in_bands,
)
from app.probabilities.devig import DevigMethod


def test_empty_policy_is_a_strict_noop() -> None:
    policy = ValuePolicy()
    assert min_edge_for(policy, "h2h", "1x2", default=0.03) == 0.03
    assert min_books_for(policy, "h2h", "1x2") == 0
    assert odds_in_bands(1.01, policy.odds_bands) is True
    assert odds_in_bands(1000.0, policy.odds_bands) is True


def test_detail_key_beats_family_key() -> None:
    policy = ValuePolicy(min_edge_by_market=(("totals", 0.04), ("over_under_2_5", 0.06)))
    assert min_edge_for(policy, "totals", "over_under_2_5", default=0.03) == 0.06
    # a different line of the same family falls back to the family entry
    assert min_edge_for(policy, "totals", "over_under_3_5", default=0.03) == 0.04


def test_family_key_applies_when_detail_is_absent() -> None:
    policy = ValuePolicy(min_edge_by_market=(("h2h", 0.05),))
    assert min_edge_for(policy, "h2h", None, default=0.03) == 0.05
    # unlisted market keeps the global default
    assert min_edge_for(policy, "btts", None, default=0.03) == 0.03


def test_lookup_keys_are_normalized_and_ordered() -> None:
    assert market_lookup_keys("H2H", " 1X2 ") == ("1x2", "h2h")
    assert market_lookup_keys("totals", None) == ("totals",)
    assert market_lookup_keys("totals", "  ") == ("totals",)
    # detail equal to family is deduplicated
    assert market_lookup_keys("h2h", "h2h") == ("h2h",)


def test_min_books_lookup_mirrors_min_edge_lookup() -> None:
    policy = ValuePolicy(min_books_by_market=(("over_under_1_5", 5), ("totals", 3)))
    assert min_books_for(policy, "totals", "over_under_1_5") == 5
    assert min_books_for(policy, "totals", "over_under_3_5") == 3
    assert min_books_for(policy, "h2h", "1x2") == 0


def test_devig_method_empty_policy_always_returns_global_default() -> None:
    # FEATURE A: empty map => every market keeps the global VALUE_DEVIG (no-op).
    policy = ValuePolicy()
    for market, detail in (("h2h", "1x2"), ("totals", "over_under_2_5"), ("h2h", None)):
        assert (
            devig_method_for(policy, market, detail, DevigMethod.DIFFERENTIAL_MARGIN)
            is DevigMethod.DIFFERENTIAL_MARGIN
        )


def test_devig_method_override_routes_detail_before_family() -> None:
    policy = ValuePolicy(
        devig_by_market=(
            ("totals", DevigMethod.PROBIT),
            ("over_under_2_5", DevigMethod.SHIN),
            ("h2h", DevigMethod.MULTIPLICATIVE),
        )
    )
    # most specific (line detail) wins
    assert (
        devig_method_for(policy, "totals", "over_under_2_5", DevigMethod.POWER) is DevigMethod.SHIN
    )
    # a different line of the same family falls back to the family entry
    assert (
        devig_method_for(policy, "totals", "over_under_3_5", DevigMethod.POWER)
        is DevigMethod.PROBIT
    )
    # 2-way market routes to its own override
    assert devig_method_for(policy, "h2h", "1x2", DevigMethod.POWER) is DevigMethod.MULTIPLICATIVE
    # an unlisted market keeps the global default
    assert devig_method_for(policy, "btts", None, DevigMethod.POWER) is DevigMethod.POWER


def test_odds_bands_are_inclusive_and_unioned() -> None:
    bands = ((1.8, 2.6), (3.0, 4.2))
    assert odds_in_bands(1.8, bands) is True  # inclusive lo
    assert odds_in_bands(2.6, bands) is True  # inclusive hi
    assert odds_in_bands(2.9, bands) is False  # between bands
    assert odds_in_bands(3.5, bands) is True  # second band
    assert odds_in_bands(4.3, bands) is False  # above all bands
    assert odds_in_bands(1.6, bands) is False  # below all bands


def test_distinct_book_count_normalizes_names_across_selections() -> None:
    prices = {
        "Home": {"Pinnacle": 2.5, "SoftBook": 2.9},
        "Draw": {"pinnacle ": 3.3},  # same book, different casing/spacing
        "Away": {"OtherBook": 3.1},
    }
    assert distinct_book_count(prices) == 3


def test_normalize_league_strips_accents_punctuation_and_case() -> None:
    assert normalize_league("Premier League") == "premier league"
    assert normalize_league("  ENGLAND - Premier League ") == "england premier league"
    assert normalize_league("Série A") == "serie a"
    assert normalize_league("LaLiga") == "laliga"
    assert normalize_league("") == ""


def test_major_league_gate_disabled_when_unset_is_a_strict_noop() -> None:
    # Empty major_leagues = gate OFF: every league is "major" (current behavior,
    # nothing demoted). This is the non-breaking default.
    policy = ValuePolicy()
    assert is_major_league(policy, "Premier League") is True
    assert is_major_league(policy, "Obscure Regional Div 3") is True
    assert is_major_league(policy, "") is True


def test_major_league_matches_on_normalized_name() -> None:
    policy = ValuePolicy(major_leagues=("Premier League", "LaLiga", "Série A"))
    # case / whitespace / accent insensitive, and tolerant of country prefixes
    # that normalize to the same token set is NOT required — exact normalized
    # membership only (no fuzzy substring, mirroring the strict matcher).
    assert is_major_league(policy, "premier league") is True
    assert is_major_league(policy, "  LA LIGA ") is False  # "la liga" != "laliga"
    assert is_major_league(policy, "LaLiga") is True
    assert is_major_league(policy, "Serie A") is True  # accent-folded match


def test_non_major_league_is_demoted_when_gate_enabled() -> None:
    policy = ValuePolicy(major_leagues=("Premier League",))
    assert is_major_league(policy, "Australia NPL Victoria") is False
    assert is_major_league(policy, "Brazil Serie A") is False


def test_blank_league_is_not_major_when_gate_enabled() -> None:
    # Gate ON + no scraped league name => cannot CONFIRM it is a covered major
    # league => not major (demoted). Honest default: only alert what we can
    # place in a known, sharp-covered league.
    policy = ValuePolicy(major_leagues=("Premier League",))
    assert is_major_league(policy, "") is False
    assert is_major_league(policy, "   ") is False
