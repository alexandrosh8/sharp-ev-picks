"""Fixture-CONFIRMED exotic/lower-league alias hand-map (Rank 1.5 part B).

Every pair here was confirmed from `probe_hardened_misses.py` against the LIVE
slate: the OddsPortal feed name and the Pinnacle archive name appeared in the
SAME fixture (same kickoff day, the OTHER side also resolving), so the mapping is
GROUND-TRUTH-confirmed against Pinnacle's own feed — not a fuzzy guess. This is
strictly more precise than a label-search: a wrong canonical would have to share
an opponent AND a kickoff with the real fixture.

LICENSE: club/team NAMES are facts (not copyrightable); the specific cross-feed
mappings are derived from our own warehouse (OddsPortal display vs Pinnacle
archive) — no third-party dataset is copied. CC0-clean.

DOCTRINE (import_alias_datasets.py): DATA-only, MARKER-FILTERED (no W/youth/
reserve — those are DIFFERENT fixtures), collision-checked on merge. Reserve
sides (Kauno Zalgiris II, Shandong Taishan II, Levadia U19) are DELIBERATELY
EXCLUDED — Rank 1.5 part A's reserve-marker veto keeps them off the senior line.

These pairs are emitted to /tmp/exotic_slate_aliases.json for import_alias_datasets
to merge + collision-check into aliases_seed.json.
"""

from __future__ import annotations

import json
from pathlib import Path

_OUT = Path("/tmp/exotic_slate_aliases.json")

# canonical (the fuller / Pinnacle-archive form) -> [feed alias, ...]
# Confirmed same-fixture pairs only. Grouped by pattern for auditability.
CONFIRMED: dict[str, list[str]] = {
    # --- Scandinavian lower divisions: genitive -s / suffix drop --------------
    "Lidköpings FK": ["Lidkoping", "Lidkopings"],
    "Vasalunds IF": ["Vasalund", "Vasalunds"],
    "Jönköpings Södra IF": ["Jonkoping", "Jonkopings Sodra"],
    "Hammarby Talang FF": ["Hammarby TFF", "Hammarby Talang"],
    # --- abbreviation / "United"/initial expansions --------------------------
    "Lambton Jaffas FC": ["Lambton J.", "Lambton Jaffas"],
    "Edgeworth Eagles FC": ["Edgeworth E.", "Edgeworth"],
    "Tampere United": ["Tampere Utd"],
    # ("Jazz Pori"->FC Jazz OMITTED: "FC Jazz" normalizes to "jazz", which already
    #  maps to the NBA "Utah Jazz" — a cross-sport collision the import correctly
    #  REJECTS. Leaving it out keeps the soccer side off the NBA canonical.)
    "New England FC": ["NEFC"],
    "Kinondoni Municipal Council FC": ["Kinondoni MC", "KMC"],
    "Welwalo Adigrat University FC": ["Welwalo Adigrat"],
    "FK Babrungas Plungė": ["Babrungas", "Babrungas Plunge"],
    "FK Smiltene/BJSS": ["FK Smiltene", "Smiltene/Bjss"],
    "Beijing Institute of Technology FC": ["Beijing Technology"],
    # ("Shanghai Second"/"Shanghai Segenda" intentionally OMITTED — a "Second"/
    #  Segunda side is reserve-flavoured and its canonical is ambiguous; the
    #  reserve veto (part A) is the right tool, not a senior alias.)
    # --- Spanish-/Portuguese-form expansions ---------------------------------
    "Deportes La Serena": ["La Serena"],
    "Deportes Concepción": ["D. Concepcion", "Deportes Concepcion"],
    "Atlético de Rafaela": ["Atl. Rafaela", "Atletico Rafaela"],
    "Serrano FC": ["Serrano RJ"],
    "Paksi FC": ["Paks", "Paksi"],
    "Titanes del Distrito Nacional": ["Titanes Del Licey"],
    "Cañeros del Este": ["Caneros", "Caneros del Este"],
}


def main() -> None:
    _OUT.write_text(json.dumps(CONFIRMED, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    n = sum(len(v) for v in CONFIRMED.values())
    print(f"wrote {_OUT}: {len(CONFIRMED)} canonicals, {n} confirmed alias surface forms")


if __name__ == "__main__":
    main()
