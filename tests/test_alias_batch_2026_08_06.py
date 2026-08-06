"""Regression for the 2026-08-06 alias batch: the OddsChecker slug typo
'botic-van-de-zandschulo' (single-letter final-token typo of Van de Zandschulp,
recurring across events 14908/15093/20455/21539/22178 since 2026-07-14).

Evidence: BOTH sharp sources corroborate the correct spelling on actively-linked
rows for the SAME fixture (event_source_links 345777 pinnacle_arcadia ref
1633360522 and 344185 betfair_api ref 35904471, both jw_two_tier 0.96 on
canonical event 21539 Medvedev v Zandschulo/Zandschulp). Locks BOTH directions
of the wrong-game-safety contract: the vetted pair now strict-matches, and the
alias NEVER crosses a women/youth/reserve marker. Also locks the wrong-game
self-audit false-positive fix: the audit's tennis cross-form comparison is
alias-canonical, so the typo'd display no longer fires 'wrong_game_anchor'
against the correctly-spelled sharp anchor (the 43-repeat ERROR cascade)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.maintenance.wrong_game_audit import _names_same_game, verify_same_game
from app.resolution import EventCandidate, default_aliases, match_event, match_event_hardened

# (feed form, pinnacle/canonical form, real opponent from the observed fixture)
# Both tennis-canonical shapes of the typo unify at the existing canonical
# 'zandschulp b' (seed entry already carries 'vandezandschulp b').
_BATCH: list[tuple[str, str, str]] = [
    ("zandschulo b", "zandschulp b", "hurkacz h"),
    ("vandezandschulo b", "zandschulp b", "medvedev d"),
]

_KO = datetime(2026, 8, 5, 23, 0, tzinfo=UTC)


@pytest.mark.parametrize(("feed", "pinnacle", "opponent"), _BATCH)
def test_alias_fixes_the_match(feed: str, pinnacle: str, opponent: str) -> None:
    aliases = default_aliases()
    assert aliases.canonical(feed) == aliases.canonical(pinnacle)
    cand = EventCandidate(ref="x", home=pinnacle, away=opponent, kickoff=_KO)
    assert match_event(feed, opponent, _KO, [cand], aliases=aliases) is cand


@pytest.mark.parametrize("marker", ["Women", "W", "U19", "U20", "II", "Reserves"])
@pytest.mark.parametrize(("feed", "pinnacle", "opponent"), _BATCH)
def test_alias_never_crosses_a_marker(feed: str, pinnacle: str, opponent: str, marker: str) -> None:
    aliases = default_aliases()
    cand = EventCandidate(ref="x", home=f"{pinnacle} {marker}", away=opponent, kickoff=_KO)
    assert match_event_hardened(feed, opponent, _KO, [cand], aliases=aliases, ordered=False) is None


def test_wrong_game_audit_accepts_typo_display_vs_correct_anchor() -> None:
    """The exact live shape of the 43x false ERROR: pick DISPLAY carries the
    slug typo ('Botic Van de Zandschulo'), the accepted anchor candidate is the
    tennis-canonical CORRECT spelling ('zandschulp b'). With the reviewed alias
    the audit's cross-form comparison unifies them — no anomaly."""
    assert _names_same_game("Botic Van de Zandschulo", "zandschulp b") is True
    anomaly = verify_same_game(
        "Daniil Medvedev",
        "Botic Van de Zandschulo",
        "medvedev d",
        "zandschulp b",
        datetime(2026, 8, 5, 23, 0, tzinfo=UTC),
        datetime(2026, 8, 5, 22, 20, tzinfo=UTC),
        ordered=False,
    )
    assert anomaly is None


def test_wrong_game_audit_still_flags_a_different_player() -> None:
    """The alias must not loosen the audit: a first-initial mismatch (a
    genuinely different player) and an unrelated surname still flag."""
    assert _names_same_game("Botic Van de Zandschulo", "zandschulp a") is False
    anomaly = verify_same_game(
        "Daniil Medvedev",
        "Botic Van de Zandschulo",
        "medvedev d",
        "hurkacz h",
        datetime(2026, 8, 5, 23, 0, tzinfo=UTC),
        datetime(2026, 8, 5, 22, 20, tzinfo=UTC),
        ordered=False,
    )
    assert anomaly is not None
    assert anomaly.code == "wrong_game_anchor"
