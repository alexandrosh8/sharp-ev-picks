"""Pull LICENSE-CLEAN (CC0) club + national-team aliases for the on-slate teams.

WHY (Rank 1.5 part B)
  The shadow match-rate report shows ZERO coverage gaps and a large ALIAS gap:
  the off-season slate is dominated by EXOTIC leagues (Iceland, Gibraltar, Swedish
  lower divisions, Australian NPL, Chinese cups, US lower/MLS Next Pro/USL) and
  national-team fixtures the top-5 datasets never covered. Pinnacle DOES carry
  these fixtures, so a bigger DETERMINISTIC alias table — the matcher stays
  exact-on-normalized — lifts the match rate. Scoped to the ON-SLATE soccer teams
  (not all of Wikidata).

SOURCE  Wikidata (CC0). Two-step, precision-guarded:
  (1) wbsearchentities resolves a feed name to candidate Q-ids;
  (2) wbgetentities pulls labels+aliases+P31, gated on P31 ∈ {association football
      club Q476028, national association football team Q6979593}. A candidate is
      ACCEPTED only if one of its English label / aliases NORMALIZES EXACTLY to the
      feed name (so the resolution itself is exact-on-normalized — NEVER a fuzzy
      mint), and only its OTHER labels/aliases that normalize DIFFERENTLY are
      emitted as new aliases. Women's classes are excluded; the marker filter is a
      second guard.

DOCTRINE (identical to import_alias_datasets.py)
  DATA-only, MARKER-FILTERED, collision-checked downstream by import_alias_datasets.

OUTPUT  /tmp/wikidata_aliases.json  {canonical: [alias,...]}  (review, then merge).

  uv run python scripts/research/import_wikidata_aliases.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.resolution.matching import distinguishing_markers, normalize_name  # noqa: E402

_OUT = Path("/tmp/wikidata_aliases.json")
_API = "https://www.wikidata.org/w/api.php"
_UA = "betting-ai-alias-research/1.0 (decision-support; read-only)"
_FOOTBALL_P31 = frozenset({"Q476028", "Q6979593"})  # club, national team (men/mixed)
# Alias languages worth keeping (latin feeds + the native scripts the feeds use).
_ALIAS_LANGS = "en|es|pt|sv|de|et|lt|lv|fi|ar|zh|fr|it|nl|no|da"


def _get(params: dict[str, str]) -> dict:
    for _ in range(4):
        try:
            r = httpx.get(_API, params=params, headers={"User-Agent": _UA}, timeout=40.0)
        except httpx.HTTPError:
            time.sleep(3.0)
            continue
        if r.status_code == 200 and r.text.lstrip().startswith("{"):
            return r.json()
        time.sleep(3.0)
    return {}


async def _slate_team_names() -> list[str]:
    """Read-only: distinct marker-clean soccer team names on picks with a known
    kickoff. Scopes the pull to the slate (not all of Wikidata)."""
    from sqlalchemy import select
    from sqlalchemy.orm import aliased

    from app.config import get_settings
    from app.database import create_engine, create_session_factory
    from app.storage.models import Event, Pick, Sport, Team

    settings = get_settings()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            home_t, away_t = aliased(Team), aliased(Team)
            rows = (
                await session.execute(
                    select(home_t.name, away_t.name)
                    .select_from(Pick)
                    .join(Event, Pick.event_id == Event.id)
                    .join(Sport, Event.sport_id == Sport.id)
                    .join(home_t, Event.home_team_id == home_t.id)
                    .join(away_t, Event.away_team_id == away_t.id)
                    .where(Sport.key == "soccer", Event.starts_at.is_not(None))
                )
            ).all()
    finally:
        await engine.dispose()
    names: set[str] = set()
    for home, away in rows:
        for n in (home, away):
            if n and not distinguishing_markers(n):
                names.add(n.strip())
    return sorted(names)


def _resolve(name: str) -> list[str]:
    d = _get(
        {
            "action": "wbsearchentities",
            "search": name,
            "language": "en",
            "type": "item",
            "limit": "8",
            "format": "json",
        }
    )
    return [it["id"] for it in d.get("search", [])]


def _entities(qids: list[str]) -> dict:
    if not qids:
        return {}
    d = _get(
        {
            "action": "wbgetentities",
            "ids": "|".join(qids),
            "props": "labels|aliases|claims",
            "languages": _ALIAS_LANGS,
            "format": "json",
        }
    )
    return d.get("entities", {})


def _aliases_for(name: str) -> tuple[str, list[str]] | None:
    """Resolve a feed name to (english_canonical, [new_alias,...]) ONLY when a
    football-club/national-team entity has a label/alias that normalizes EXACTLY to
    the feed name. Marker-carrying surface forms are dropped. None when no exact-
    normalized football entity is found (NEVER a fuzzy guess)."""
    nn = normalize_name(name)
    ents = _entities(_resolve(name))
    for _q, e in ents.items():
        p31 = {
            c["mainsnak"].get("datavalue", {}).get("value", {}).get("id")
            for c in e.get("claims", {}).get("P31", [])
        }
        if not (p31 & _FOOTBALL_P31):
            continue
        en = e.get("labels", {}).get("en", {}).get("value", "")
        alts = [a["value"] for lang in e.get("aliases", {}).values() for a in lang]
        surfaces = [s for s in [en, *alts] if s]
        if not any(normalize_name(s) == nn for s in surfaces):
            continue  # exact-normalized guard: this entity really IS the feed team
        if distinguishing_markers(en):
            continue
        canon_norm = normalize_name(en)
        new = sorted(
            {
                s
                for s in surfaces
                if normalize_name(s) != canon_norm and not distinguishing_markers(s)
            }
        )
        return (en, new) if new else (en, [name] if nn != canon_norm else [])
    return None


def main() -> None:
    names = asyncio.run(_slate_team_names())
    print(f"distinct marker-clean soccer team names on the slate : {len(names)}")
    pairs: dict[str, set[str]] = {}
    for i, name in enumerate(names):
        try:
            res = _aliases_for(name)
        except Exception as exc:  # noqa: BLE001 — research script: log + continue
            print(f"  {name!r}: {type(exc).__name__} — skipped")
            res = None
        if res:
            canon, alts = res
            bucket = pairs.setdefault(canon, set())
            bucket.update(a for a in alts if a)
        time.sleep(0.4)  # polite to the public API
        if (i + 1) % 20 == 0:
            print(f"  ...{i + 1}/{len(names)} processed, {len(pairs)} canonicals so far")

    emit = {k: sorted(v) for k, v in pairs.items() if v}
    _OUT.write_text(json.dumps(emit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    n_alias = sum(len(v) for v in emit.values())
    print(f"\ncanonicals with >=1 new alias : {len(emit)}")
    print(f"candidate alias->canonical    : {n_alias}")
    print(f"wrote {_OUT}")


if __name__ == "__main__":
    main()
