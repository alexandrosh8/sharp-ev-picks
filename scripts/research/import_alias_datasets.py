"""Import LICENSE-CLEAN club/team name aliases into the cross-source alias seed.

WHY
  The shadow match-rate harness shows the binding constraint on Pinnacle<->
  canonical linkage is the TINY alias table (~147 surface forms), not coverage:
  games 100% overlap, only ~20-35% match by exact name. Nickname/abbreviation
  resolution is a LOOKUP problem ("Spurs"->Tottenham shares no chars), so the
  highest-ROI, lowest-risk lift is a bigger DETERMINISTIC alias table.

SOURCES (CC0/MIT only — see PROVENANCE below; NON-COMMERCIAL sets are EXCLUDED)
  - openfootball/clubs (CC0)            -> /tmp/final_aliases.json (men's senior)
  - pretrehr/Sports-betting (MIT)       -> merged into /tmp/final_aliases.json
  - swar/nba_api static teams (MIT)     -> /tmp/nba_aliases.json (NBA tricodes)
  - withqwerty/reep (CC0)               -> already imported by
                                           import_reep_soccer_aliases.py
  Reserve hand-maps (parent name differs from reserve name) are added inline.

DOCTRINE (read before editing) — identical to import_reep_soccer_aliases.py
  - DATA-only. The matcher stays exact-on-normalized; this script NEVER mints a
    fuzzy pair. Every emitted pair is an EXACT alias whose normalize_name()
    differs from its canonical's.
  - MARKER-FILTERED. Any canonical OR alias carrying a women/youth/reserve
    marker (distinguishing_markers != {}) is DROPPED — conflating a women's/
    youth/reserve side with the senior one is the wrong-game CLV defect. The
    reserve HAND-MAPS use names that carry no marker token (Castilla, Jong, B)
    and are added explicitly.
  - COLLISION-SAFE. A new alias->canonical pair is SKIPPED (and logged) if its
    normalized alias already maps to a DIFFERENT canonical, or if a new canonical
    would collapse onto an existing DISTINCT canonical's normalized form. A
    source canonical that normalizes to one we already have MERGES under the
    existing label (no accent-variant duplicate key).

USAGE
  uv run python scripts/research/import_alias_datasets.py            # dry-run
  uv run python scripts/research/import_alias_datasets.py --write    # rewrite seed
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from app.resolution.matching import _SEED_PATH, distinguishing_markers, normalize_name

_FOOTBALL_SRC = Path("/tmp/final_aliases.json")
_NBA_SRC = Path("/tmp/nba_aliases.json")
# Rank 1.5 part B exotic/lower-league sources (CC0): the fixture-CONFIRMED
# exotic hand-map (scripts/research/exotic_slate_aliases.py) and the Wikidata
# altLabel pull (scripts/research/import_wikidata_aliases.py). Both write to /tmp.
_EXOTIC_SRC = Path("/tmp/exotic_slate_aliases.json")
_WIKIDATA_SRC = Path("/tmp/wikidata_aliases.json")

# Reserve teams whose name DIFFERS from the parent (so a plain alias would be
# wrong). These carry NO women/youth/reserve marker token, so they survive the
# marker filter and are intentionally DISTINCT canonical entries from the senior
# side (Real Madrid != Real Madrid Castilla).
_RESERVE_HAND_MAPS: dict[str, list[str]] = {
    "Real Madrid Castilla": ["Real Madrid B"],
    "Real Sociedad B": ["Sanse"],
    "Athletic Bilbao B": ["Bilbao Athletic"],
    "Barcelona Atletic": ["Barca B", "Barcelona B"],
    "Jong Ajax": ["Ajax B"],
    "Jong PSV": ["PSV B"],
    "Jong AZ": ["AZ B"],
}


def _has_marker(name: str) -> bool:
    return bool(distinguishing_markers(name))


def _load_source(path: Path) -> dict[str, list[str]]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {str(k): [str(a) for a in v] for k, v in raw.items()}


def _load_existing_seed() -> dict[str, list[str]]:
    data = json.loads(_SEED_PATH.read_text(encoding="utf-8"))
    teams: dict[str, list[str]] = data.get("teams", {})
    return teams


def build_import(
    sources: dict[str, list[str]],
    existing_seed: dict[str, list[str]],
) -> tuple[dict[str, list[str]], list[tuple[str, str, str]], int]:
    """Return (new_pairs_by_canonical, collisions, markered_dropped).

    new_pairs_by_canonical: {canonical_label -> [alias_label, ...]} to ADD.
    collisions: (alias, attempted_canonical, blocking_canonical) skipped pairs.
    markered_dropped: count of source surface forms dropped for carrying a marker.
    """
    alias_to_canon: dict[str, str] = {}
    canon_label_by_norm: dict[str, str] = {}
    for canonical, aliases in existing_seed.items():
        canon_norm = normalize_name(canonical)
        if not canon_norm:
            continue
        canon_label_by_norm.setdefault(canon_norm, canonical)
        for surface in [canonical, *aliases]:
            s = normalize_name(surface)
            if s:
                alias_to_canon[s] = canon_norm

    new_pairs: dict[str, list[str]] = defaultdict(list)
    collisions: list[tuple[str, str, str]] = []
    markered = 0
    for src_canonical in sorted(sources):
        aliases = sources[src_canonical]
        # MARKER FILTER on the canonical: a women/youth/reserve canonical is a
        # different fixture class — never seed it from the bulk sources.
        if _has_marker(src_canonical):
            markered += 1
            continue
        canon_norm = normalize_name(src_canonical)
        if not canon_norm:
            continue
        # A source canonical that collapses onto an EXISTING DIFFERENT canonical
        # is a collision (would conflate two clubs) -> skip.
        prior = alias_to_canon.get(canon_norm)
        if prior is not None and prior != canon_norm:
            collisions.append((src_canonical, src_canonical, prior))
            continue
        canonical = canon_label_by_norm.setdefault(canon_norm, src_canonical)
        alias_to_canon.setdefault(canon_norm, canon_norm)
        for alias in aliases:
            if _has_marker(alias):  # MARKER FILTER on each alias surface form
                markered += 1
                continue
            a_norm = normalize_name(alias)
            if not a_norm or a_norm == canon_norm:
                continue
            existing_canon = alias_to_canon.get(a_norm)
            if existing_canon is not None:
                if existing_canon != canon_norm:
                    collisions.append((alias, canonical, existing_canon))
                continue
            alias_to_canon[a_norm] = canon_norm
            if alias not in new_pairs[canonical]:
                new_pairs[canonical].append(alias)
    return {k: v for k, v in new_pairs.items() if v}, collisions, markered


def merge_into_seed(
    existing_seed: dict[str, list[str]], new_pairs: dict[str, list[str]]
) -> dict[str, list[str]]:
    merged: dict[str, list[str]] = {k: list(v) for k, v in existing_seed.items()}
    for canonical, aliases in new_pairs.items():
        bucket = merged.setdefault(canonical, [])
        for alias in aliases:
            if alias not in bucket:
                bucket.append(alias)
    return dict(sorted(merged.items()))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="rewrite aliases_seed.json")
    args = parser.parse_args()

    sources: dict[str, list[str]] = {}
    for src in (
        _load_source(_FOOTBALL_SRC),
        _load_source(_NBA_SRC),
        _load_source(_EXOTIC_SRC),
        _load_source(_WIKIDATA_SRC),
        _RESERVE_HAND_MAPS,
    ):
        for canonical, aliases in src.items():
            sources.setdefault(canonical, [])
            for a in aliases:
                if a not in sources[canonical]:
                    sources[canonical].append(a)

    existing_seed = _load_existing_seed()
    new_pairs, collisions, markered = build_import(sources, existing_seed)
    n_new_aliases = sum(len(v) for v in new_pairs.values())

    print(f"source canonicals (merged)        : {len(sources)}")
    print(f"existing seed canonicals          : {len(existing_seed)}")
    print(f"canonicals gaining aliases        : {len(new_pairs)}")
    print(f"NEW alias->canonical pairs        : {n_new_aliases}")
    print(f"markered surface forms dropped    : {markered}")
    print(f"collisions skipped                : {len(collisions)}")
    for alias, attempted, blocking in collisions[:40]:
        print(f"  SKIP collision: {alias!r} -> {attempted!r} (already -> {blocking!r})")
    if len(collisions) > 40:
        print(f"  ... and {len(collisions) - 40} more")

    if args.write and new_pairs:
        data = json.loads(_SEED_PATH.read_text(encoding="utf-8"))
        data["teams"] = merge_into_seed(existing_seed, new_pairs)
        _SEED_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"\nwrote {_SEED_PATH} ({len(data['teams'])} canonicals)")


if __name__ == "__main__":
    main()
