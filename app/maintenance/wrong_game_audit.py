"""Wrong-game safety-net audit for live Pinnacle sharp anchors.

The go-live flip (resolve_pinnacle_close_snaps -> match_event_hardened) puts the
precision-hardened cross-source matcher on the LIVE anchor path. A wrong-game
Pinnacle close is fake CLV — the project's cardinal sin — so this module is the
standing net: it INDEPENDENTLY re-verifies recently-accepted anchors against the
same-game invariants and surfaces any violation as an ERROR through the existing
self-audit / health-monitor alert channel.

Two halves, mirroring app.maintenance.self_audit:
- ``verify_same_game`` — PURE (no DB / no clock): given a pick's teams+kickoff and
  the matched anchor event's teams+kickoff, return an ``Anomaly`` iff the pair is
  NOT the same fixture (marker conflict, unrelated names, or kickoff out of
  window), else None. It re-derives the same-game predicate from the public
  resolution helpers rather than trusting the matcher that accepted the anchor.
- ``audit_live_pinnacle_anchors`` — the thin READ-ONLY DB wrapper that samples
  recent picks, finds the live anchor the matcher resolves for each, and runs the
  pure verifier over it. Non-destructive: it writes nothing and changes no pick.

Bound into the scheduled ``self_audit_job`` so a production wrong-game anchor logs
ERROR on the same cadence as the other runtime invariants.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from app.maintenance.self_audit import Anomaly
from app.storage.models import Event, League, Pick, Sport, Team

logger = logging.getLogger(__name__)

# Same two-tier fuzzy accept thresholds the hardened matcher uses (kept in sync
# with app.resolution.matching). The audit re-derives the same-game name predicate
# so a genuine fuzzy recovery PASSES while an unrelated-name merge is FLAGGED.
_JW_ACCEPT = 0.92
_TOKEN_SORT_ACCEPT = 90.0

# Default live-anchor kickoff ACCEPT window (minutes) — kept in sync with the
# matcher's tight accept bound (``app.resolution.matching._ACCEPT_MINUTE_DRIFT``,
# 6h). The wrong-game fix (2026-06-24) made the matcher REJECT any anchor whose
# kickoff is beyond this bound; the independent audit verifies the SAME predicate,
# so a same-teams rematch / two-leg / doubleheader leg outside the accept window
# (Gigantes/Cangrejeros BSN, 48h earlier) is FLAGGED here too rather than passed
# by a stale 36h tolerance. A few hours of cross-source timezone/rounding noise on
# the SAME game still verifies clean.
_DEFAULT_MAX_MINUTE_DRIFT = 6 * 60


def _names_same_game(a: str, b: str) -> bool:
    """True when two team names are the SAME side: alias-canonical BASE equality,
    OR the matcher's two-tier fuzzy bar (JW>=0.92 AND token_sort>=90) with NO
    disambiguating-token-only difference. Re-derived from the public resolution
    helpers so this is an INDEPENDENT check, not the matcher vouching for itself."""
    from app.resolution.matching import (
        _DISAMBIGUATING_TOKENS,
        default_aliases,
        jaro_winkler,
        strip_markers,
        token_sort_ratio,
    )

    aliases = default_aliases()
    base_a = aliases.canonical(strip_markers(a))
    base_b = aliases.canonical(strip_markers(b))
    if not base_a or not base_b:
        return False
    if base_a == base_b:
        return True
    # Two base names differing ONLY by a club-disambiguating token (United/City/
    # Sociedad/B/II/...) are DIFFERENT clubs — never the same game.
    tokens_a, tokens_b = set(base_a.split()), set(base_b.split())
    diff = tokens_a ^ tokens_b
    if diff and diff <= _DISAMBIGUATING_TOKENS:
        return False
    # Token containment: a display name with extra NON-disambiguating noise tokens
    # (sponsor/stadium tails — "Bayern Munich Allianz Sponsor" vs "Bayern Munich")
    # is the SAME club. Bounded by the disambiguating-token veto above, so it can
    # never merge Man Utd / Man City; disjoint names (Lazio vs Inter — no shared
    # token) are NOT contained and stay rejected. The SHORTER base must itself carry
    # >=2 tokens: a single-token shorter name ("Real" < "Real Madrid", "America" <
    # "America Mineiro") is too ambiguous to confirm a club by containment, so it
    # falls through to the stricter JW + token-sort bar instead. This keeps the
    # audit from false-alarming on the very noise that forced the loader to its slug
    # fallback, without blessing a one-word prefix as a whole different club.
    if (tokens_a <= tokens_b or tokens_b <= tokens_a) and min(len(tokens_a), len(tokens_b)) >= 2:
        return True
    return jaro_winkler(base_a, base_b) >= _JW_ACCEPT and (
        token_sort_ratio(base_a, base_b) >= _TOKEN_SORT_ACCEPT
    )


def verify_same_game(
    pick_home: str,
    pick_away: str,
    anchor_home: str,
    anchor_away: str,
    pick_kickoff: datetime,
    anchor_kickoff: datetime,
    *,
    ordered: bool = True,
    max_minute_drift: int = _DEFAULT_MAX_MINUTE_DRIFT,
) -> Anomaly | None:
    """Re-verify ONE accepted anchor is the SAME fixture as the pick. Returns an
    ERROR ``Anomaly`` on any same-game violation, else None.

    Three independent rules (any failure => wrong game):
      1. MARKER CONSISTENCY — ``distinguishing_markers`` (women/youth/reserve) must
         agree on BOTH sides (a one-sided marker is a reserve-vs-senior /
         women-vs-men different fixture).
      2. NAME RELATEDNESS — both team pairs are same-game related (base equality or
         the two-tier fuzzy bar). ``ordered`` events match forward only; unordered
         (tennis) also accept the home/away swap.
      3. KICKOFF WINDOW — the anchor kickoff is within ``max_minute_drift`` of the
         pick kickoff (an out-of-window capture is a different round / in-play row).
    """
    from app.resolution.matching import distinguishing_markers

    detail = (
        f"pick={pick_home!r} v {pick_away!r} @ {pick_kickoff.isoformat()} "
        f"!= anchor={anchor_home!r} v {anchor_away!r} @ {anchor_kickoff.isoformat()}"
    )

    def flag(reason: str) -> Anomaly:
        return Anomaly("ERROR", "wrong_game_anchor", f"{reason}: {detail}")

    # 3. kickoff window
    if abs((anchor_kickoff - pick_kickoff).total_seconds()) > max_minute_drift * 60:
        return flag("anchor kickoff outside window")

    # 1+2 forward orientation
    markers_ok_fwd = distinguishing_markers(pick_home) == distinguishing_markers(
        anchor_home
    ) and distinguishing_markers(pick_away) == distinguishing_markers(anchor_away)
    names_ok_fwd = _names_same_game(pick_home, anchor_home) and _names_same_game(
        pick_away, anchor_away
    )
    if markers_ok_fwd and names_ok_fwd:
        return None

    # Unordered (tennis): the home/away swap is the same fixture too.
    if not ordered:
        markers_ok_swap = distinguishing_markers(pick_home) == distinguishing_markers(
            anchor_away
        ) and distinguishing_markers(pick_away) == distinguishing_markers(anchor_home)
        names_ok_swap = _names_same_game(pick_home, anchor_away) and _names_same_game(
            pick_away, anchor_home
        )
        if markers_ok_swap and names_ok_swap:
            return None

    if not markers_ok_fwd:
        return flag("distinguishing-marker conflict")
    return flag("team names are not the same fixture")


async def audit_live_pinnacle_anchors(
    session_factory: async_sessionmaker[AsyncSession],
    now: datetime | None = None,
    *,
    lookback: timedelta = timedelta(days=3),
    horizon: timedelta = timedelta(days=3),
    sample_limit: int = 200,
) -> list[Anomaly]:
    """READ-ONLY: sample picks for near-term fixtures, resolve the live Pinnacle
    anchor the way the pick-time loader does, and re-verify each accepted anchor is
    the SAME game.

    Population is by FIXTURE kickoff (``Event.starts_at`` in
    ``[now - lookback, now + horizon]``) — a live anchor is attached for an
    upcoming/recent fixture, and kickoff is a robust recency signal (pick
    created_at is the persist wall-clock, not controllable in replay). Returns one
    ``wrong_game_anchor`` ERROR ``Anomaly`` per mismatch (empty == clean).
    Non-destructive — attaches no close, writes no row, changes no pick. Mirrors the
    resolution the live loader uses (resolve_pinnacle_close_snaps), so "accepted"
    here means exactly what the live anchor path accepts.
    """
    from app.resolution import (
        EventCandidate,
        default_aliases,
        distinguishing_markers,
        match_event_hardened,
        oddsportal_slug_names,
    )
    from app.resolution.shadow import arcadia_base_sport
    from app.resolution.tennis_names import canonical_tennis_name

    now = now or datetime.now(tz=UTC)
    anomalies: list[Anomaly] = []
    home_t, away_t = aliased(Team), aliased(Team)
    async with session_factory() as session:
        pick_rows = (
            await session.execute(
                select(
                    Sport.key,
                    League.key,
                    home_t.name,
                    away_t.name,
                    Event.starts_at,
                    Event.external_ref,
                )
                .select_from(Pick)
                .join(Event, Pick.event_id == Event.id)
                .join(Sport, Event.sport_id == Sport.id)
                .join(League, Event.league_id == League.id, isouter=True)
                .join(home_t, Event.home_team_id == home_t.id)
                .join(away_t, Event.away_team_id == away_t.id)
                .where(
                    Event.starts_at.is_not(None),
                    Event.starts_at >= now - lookback,
                    Event.starts_at <= now + horizon,
                )
                .order_by(Event.starts_at.desc())
                .limit(sample_limit)
            )
        ).all()
        if not pick_rows:
            return []

        aliases = default_aliases()
        window = timedelta(days=2)
        # FETCH window: the live loader's wide (max_day_drift+1)-day candidate bound
        # — passed as max_minute_drift so the matcher gathers every same-teams leg
        # for ambiguity detection. ACCEPTANCE is gated SEPARATELY by the matcher's
        # tight default (_ACCEPT_MINUTE_DRIFT, 6h), so a same-teams rematch 48h away
        # is rejected, exactly as the live path now does. The independent re-verify
        # below uses the tight ACCEPT bound, not this fetch window.
        minute_drift = 2 * 24 * 60
        arc_home, arc_away = aliased(Team), aliased(Team)
        for sport_key, _league_key, home, away, kickoff, ext_ref in pick_rows:
            base = arcadia_base_sport(sport_key)
            is_tennis = base == "tennis"
            arc_rows = (
                await session.execute(
                    select(arc_home.name, arc_away.name, Event.starts_at)
                    .select_from(Event)
                    .join(Sport, Event.sport_id == Sport.id)
                    .join(arc_home, Event.home_team_id == arc_home.id)
                    .join(arc_away, Event.away_team_id == arc_away.id)
                    .where(
                        Sport.key == f"pinnacle_{base}",
                        Event.starts_at.is_not(None),
                        Event.starts_at >= kickoff - window,
                        Event.starts_at <= kickoff + window,
                    )
                )
            ).all()
            candidates = [
                EventCandidate(
                    ref=str(i),
                    home=canonical_tennis_name(h) if is_tennis else h,
                    away=canonical_tennis_name(a) if is_tennis else a,
                    kickoff=ko,
                )
                for i, (h, a, ko) in enumerate(arc_rows)
            ]
            if not candidates:
                continue
            qh = canonical_tennis_name(home) if is_tennis else home
            qa = canonical_tennis_name(away) if is_tennis else away
            matched = match_event_hardened(
                qh,
                qa,
                kickoff,
                candidates,
                aliases=aliases,
                ordered=not is_tennis,
                league=None,
                candidate_leagues=None,
                max_minute_drift=minute_drift,
            )
            # The pick names to RE-VERIFY against — the display by default. When the
            # primary fails and the live loader's SLUG fallback resolves the anchor,
            # the loader matches on the slug names: a DEFECTIVE slug (one that names a
            # DIFFERENT team than the display) attaches a wrong-game close, so the
            # audit MUST cover the slug path. It verifies the ANCHOR against the
            # DISPLAY names (the pick the close is re-keyed to), which is exactly what
            # flags a Lazio-pick / Inter-anchor mismatch.
            if matched is None:
                slug = oddsportal_slug_names(ext_ref)
                if slug is None:
                    continue
                sh = canonical_tennis_name(slug[0]) if is_tennis else slug[0]
                sa = canonical_tennis_name(slug[1]) if is_tennis else slug[1]
                # Mirror the loader's marker guard: it only tries the slug when the
                # slug RETAINS every distinguishing marker the display carries.
                display_markers = distinguishing_markers(home) | distinguishing_markers(away)
                slug_markers = distinguishing_markers(sh) | distinguishing_markers(sa)
                if not (display_markers <= slug_markers):
                    continue
                matched = match_event_hardened(
                    sh,
                    sa,
                    kickoff,
                    candidates,
                    aliases=aliases,
                    ordered=not is_tennis,
                    league=None,
                    candidate_leagues=None,
                    max_minute_drift=minute_drift,
                )
                if matched is None:
                    continue
            # Re-verify the ACCEPTED anchor independently. Verify on the RAW pick
            # DISPLAY names (not the tennis-canonicalized or slug form) so the close,
            # which is re-keyed onto the pick's display fixture, is checked to be the
            # SAME game — and the marker check sees any women/youth/reserve token the
            # display name carries.
            anomaly = verify_same_game(
                home,
                away,
                matched.home,
                matched.away,
                kickoff,
                matched.kickoff,
                ordered=not is_tennis,
                # Verify against the TIGHT accept bound (verify_same_game's default,
                # in sync with the matcher's _ACCEPT_MINUTE_DRIFT) — NOT the wide fetch
                # window. An anchor whose kickoff is beyond the accept bound is the
                # wrong-game leg the matcher now rejects; the audit flags it the same.
            )
            if anomaly is not None:
                anomalies.append(anomaly)
    return anomalies
