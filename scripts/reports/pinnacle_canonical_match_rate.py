r"""SHADOW match-rate report: canonical OddsPortal slate vs the pinnacle_<sport>
archive, through the matcher — BEFORE (strict+slug) vs AFTER (+hardened fuzzy),
with a NEW-match sample for a false-merge spot-check. Writes NOTHING to the DB.

This is the slate-level instrument the match-rate-lift plan asks be checked
before any change touches live picks. It reads READ-ONLY TSV dumps produced via
`docker exec` psql (the same access path the prompt specifies); each TSV row is
external_ref, home, away, starts_at (UTC YYYY-MM-DDTHH:MM:SS), league_key. Dump
one file per sport into /tmp/slate/<sport>.tsv with a COPY ... TO STDOUT of those
five columns for events with a non-null starts_at.

Cross-source league names use DIFFERENT taxonomies (OddsPortal "Premier League"
vs Pinnacle "England - Premier League"), so the hardened matcher's STAGE-0 league
block is passed None here (incomparable) and precision rests on the marker veto +
two-tier name accept + UTC-window + ambiguity reject.

USAGE
  uv run python scripts/reports/pinnacle_canonical_match_rate.py [--sample N]
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from app.resolution.matching import (
    AliasTable,
    EventCandidate,
    default_aliases,
    distinguishing_markers,
    jaro_winkler,
    match_event,
    match_event_hardened,
    oddsportal_slug_names,
    strip_markers,
)

SLATE = Path("/tmp/slate")
PAIRS = (
    ("soccer", "pinnacle_soccer"),
    ("basketball", "pinnacle_basketball"),
    ("tennis", "pinnacle_tennis"),
)


def _load(key: str) -> list[tuple[str, str, str, datetime, str]]:
    rows: list[tuple[str, str, str, datetime, str]] = []
    path = SLATE / f"{key}.tsv"
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        parts += [""] * (5 - len(parts))
        ext, home, away, ts, league = parts[:5]
        rows.append((ext, home, away, datetime.fromisoformat(ts).replace(tzinfo=UTC), league))
    return rows


def _strict_or_slug(
    home: str,
    away: str,
    ext: str,
    ko: datetime,
    in_window: list[EventCandidate],
    aliases: AliasTable,
    *,
    ordered: bool,
) -> EventCandidate | None:
    m = match_event(home, away, ko, in_window, aliases=aliases, ordered=ordered)
    if m is not None:
        return m
    slug = oddsportal_slug_names(ext)
    if slug is None:
        return None
    dm = distinguishing_markers(home) | distinguishing_markers(away)
    smk = distinguishing_markers(slug[0]) | distinguishing_markers(slug[1])
    if dm <= smk:
        return match_event(slug[0], slug[1], ko, in_window, aliases=aliases, ordered=ordered)
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=int, default=20, help="max NEW matches to print")
    args = parser.parse_args()
    aliases = default_aliases()

    print(f"{'sport':12s} {'total':>6s} {'before':>8s} {'after':>8s}  new")
    grand = {"total": 0, "before": 0, "after": 0}
    new_samples: list[str] = []
    for canon_key, pin_key in PAIRS:
        ordered = canon_key != "tennis"
        canon = _load(canon_key)
        pin = _load(pin_key)
        archive = [
            EventCandidate(ref=str(i), home=h, away=a, kickoff=ko)
            for i, (_e, h, a, ko, _lg) in enumerate(pin)
        ]
        pinmap = {str(i): (h, a) for i, (_e, h, a, _ko, _lg) in enumerate(pin)}
        before = after = 0
        for ext, home, away, ko, _lg in canon:
            in_window = [c for c in archive if abs((c.kickoff.date() - ko.date()).days) <= 1]
            m = _strict_or_slug(home, away, ext, ko, in_window, aliases, ordered=ordered)
            if m is not None:
                before += 1
                after += 1
                continue
            h2 = match_event_hardened(
                home,
                away,
                ko,
                in_window,
                aliases=aliases,
                ordered=ordered,
                league=None,
                candidate_leagues=None,
            )
            if h2 is not None:
                after += 1
                if len(new_samples) < args.sample:
                    ph, pa = pinmap[h2.ref]
                    jh = jaro_winkler(
                        aliases.canonical(strip_markers(home)), aliases.canonical(strip_markers(ph))
                    )
                    ja = jaro_winkler(
                        aliases.canonical(strip_markers(away)), aliases.canonical(strip_markers(pa))
                    )
                    new_samples.append(
                        f"  [{canon_key}] OP {home} v {away}  ==  PIN {ph} v {pa}"
                        f"  (jwH={jh:.3f} jwA={ja:.3f})"
                    )
        total = len(canon)
        br = before / total if total else 0.0
        ar = after / total if total else 0.0
        print(f"{canon_key:12s} {total:6d} {br:8.1%} {ar:8.1%}  +{after - before}")
        grand["total"] += total
        grand["before"] += before
        grand["after"] += after
    gt = grand["total"]
    print(
        f"{'OVERALL':12s} {gt:6d} "
        f"{grand['before'] / gt if gt else 0:8.1%} {grand['after'] / gt if gt else 0:8.1%}  "
        f"+{grand['after'] - grand['before']}"
    )
    print("\nNEW matches added by the hardened fuzzy layer (spot-check for wrong-game merges):")
    for s in new_samples:
        print(s)


if __name__ == "__main__":
    main()
