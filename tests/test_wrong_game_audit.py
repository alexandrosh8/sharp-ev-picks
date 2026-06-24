"""Wrong-game safety-net audit for live Pinnacle anchors (pure verifier — no DB).

The go-live flip puts the precision-hardened matcher on the live anchor path. The
standing safety net is an INDEPENDENT re-verification of every recently-accepted
anchor: a wrong-game Pinnacle close = fake CLV (the cardinal sin), so an anchor
that the matcher accepted is re-checked here against the same-game invariants —

  1. distinguishing markers (women / youth / reserve) agree on BOTH sides
     (a one-sided marker = a different fixture: reserve vs senior, women vs men);
  2. the two team names are same-game related (alias-canonical base equality OR
     the matcher's two-tier fuzzy accept) — a genuinely unrelated pair is a merge;
  3. the kickoff is inside the accepted window.

A failure on ANY rule is a wrong-game anchor -> the audit reports it ERROR so a
production wrong-game merge is immediately visible. The pure verifier carries the
logic (tested here, no DB); the DB sampler is the thin read wrapper.
"""

from datetime import UTC, datetime, timedelta

from app.maintenance.wrong_game_audit import verify_same_game

KO = datetime(2026, 6, 20, 18, 0, tzinfo=UTC)


def test_correct_exact_anchor_passes() -> None:
    # An exact same-game anchor (identical teams, same kickoff) verifies clean.
    assert (
        verify_same_game(
            "Manchester United",
            "Chelsea",
            "Manchester United",
            "Chelsea",
            KO,
            KO,
        )
        is None
    )


def test_correct_alias_anchor_passes() -> None:
    # The pick spelled an alias ("Man Utd"); the anchor is the canonical name.
    # Same game -> no anomaly.
    assert verify_same_game("Man Utd", "Chelsea", "Manchester United", "Chelsea", KO, KO) is None


def test_correct_exotic_fuzzy_anchor_passes() -> None:
    # The go-live recall lift: a fuzzy spelling variant ("Bayer Leverkussen") that
    # the hardened matcher accepted is SAME-GAME by the two-tier fuzzy bar, so the
    # audit must NOT false-alarm on it.
    assert (
        verify_same_game(
            "Bayer Leverkussen",
            "Werder Bremen",
            "Bayer Leverkusen",
            "Werder Bremen",
            KO,
            KO,
        )
        is None
    )


def test_reserve_vs_senior_anchor_is_flagged() -> None:
    # WRONG GAME: a senior pick wearing the reserve side's close ("Real Madrid B").
    a = verify_same_game("Real Madrid", "Getafe", "Real Madrid B", "Getafe", KO, KO)
    assert a is not None
    assert a.severity == "ERROR"
    assert a.code == "wrong_game_anchor"


def test_women_vs_men_anchor_is_flagged() -> None:
    # WRONG GAME: a women's pick attached to the men's fixture close.
    a = verify_same_game("Arsenal Women", "Chelsea Women", "Arsenal", "Chelsea", KO, KO)
    assert a is not None
    assert a.severity == "ERROR"


def test_unrelated_teams_anchor_is_flagged() -> None:
    # WRONG GAME: a genuine merge of two different fixtures (unrelated names).
    a = verify_same_game("Alpha", "Beta", "Gamma", "Delta", KO, KO)
    assert a is not None
    assert a.code == "wrong_game_anchor"


def test_kickoff_outside_window_is_flagged() -> None:
    # WRONG GAME: same teams, but the anchor kickoff is well outside the window
    # (an in-play / different-round capture mis-attached as the close).
    far = KO + timedelta(minutes=600)
    a = verify_same_game("Alpha", "Beta", "Alpha", "Beta", KO, far, max_minute_drift=240)
    assert a is not None
    assert a.code == "wrong_game_anchor"


def test_kickoff_inside_window_passes() -> None:
    near = KO + timedelta(minutes=30)
    assert (
        verify_same_game("Alpha", "Beta", "Alpha", "Beta", KO, near, max_minute_drift=240) is None
    )


def test_same_teams_rematch_two_days_apart_is_flagged() -> None:
    # The LIVE defect (Gigantes/Cangrejeros BSN): the anchor is the SAME teams but
    # the leg 48h earlier with home/away SWAPPED. Even though the names are the same
    # club pair, the kickoff is far outside the tight accept bound -> WRONG GAME.
    pick_ko = datetime(2026, 6, 23, 0, 0, tzinfo=UTC)
    anchor_ko = datetime(2026, 6, 21, 0, 0, tzinfo=UTC)
    a = verify_same_game(
        "Gigantes de Carolina",
        "Cangrejeros",
        "Cangrejeros de Santurce",
        "Gigantes de Carolina",
        pick_ko,
        anchor_ko,
    )
    assert a is not None
    assert a.severity == "ERROR"
    assert a.code == "wrong_game_anchor"


def test_same_teams_same_day_timezone_noise_passes() -> None:
    # RECALL guard for the audit: the SAME game with a few hours of cross-source
    # kickoff noise (date-only 00:00 archive vs a real wall-clock pick) verifies
    # clean — the tight accept default (6h) still admits it.
    pick_ko = datetime(2026, 6, 23, 0, 0, tzinfo=UTC)
    anchor_ko = pick_ko + timedelta(hours=3)
    assert (
        verify_same_game(
            "Gigantes de Carolina",
            "Cangrejeros de Santurce",
            "Gigantes de Carolina",
            "Cangrejeros de Santurce",
            pick_ko,
            anchor_ko,
        )
        is None
    )


def test_noisy_display_name_with_sponsor_tail_passes() -> None:
    # A display name carrying extra NON-disambiguating noise (sponsor/stadium tail)
    # over the anchor is the SAME club (token containment, shorter base >=2 tokens).
    assert (
        verify_same_game(
            "Bayern Munich Allianz Sponsor",
            "Borussia Dortmund",
            "Bayern Munich",
            "Borussia Dortmund",
            KO,
            KO,
        )
        is None
    )


def test_single_token_prefix_is_not_same_game() -> None:
    # A one-word name that is merely a PREFIX of a different club ("Real" vs "Real
    # Madrid", "America" vs "America Mineiro") must NOT pass by containment — it is
    # too ambiguous to confirm a club. The audit flags such a pair.
    assert verify_same_game("Real", "Getafe", "Real Madrid", "Getafe", KO, KO) is not None
    assert (
        verify_same_game("America", "Flamengo", "America Mineiro", "Flamengo", KO, KO) is not None
    )
