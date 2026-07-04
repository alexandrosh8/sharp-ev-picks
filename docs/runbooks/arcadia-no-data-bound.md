# ARCADIA "no data for <sport> this cycle" — the benign bound

`app/ingestion/pinnacle_arcadia.py` logs `WARNING ... no data for <sport> this
cycle (PinnacleArcadiaError)` when a single ARCADIA poll returns nothing for one
sport. This is **classified benign** — but "benign" only holds inside an explicit
bound. This file records that bound so the next person watching does not have to
re-derive it.

## Why the warning is expected at all

ARCADIA polls every `arcadia_poll_interval_seconds` (default **120 s**,
`app/config.py`) across four sports (soccer, basketball, tennis,
american_football). A poll can transiently return empty for one sport because:

- the Pinnacle `/sports` discovery endpoint hiccups for one request (the loader
  then falls back to "capturing all configured sports" — self-heals next cycle);
- a sport genuinely has **no in-window fixtures right now** (off-season / small
  slate — american_football carries only a handful of events most of the year).

Neither is a fault. The capture is fail-closed: an empty cycle mints nothing and
the next cycle recovers.

## The bound (what makes it benign)

The warning stays benign as long as **all three** hold:

1. **Rate.** No-data warnings are a small fraction of cycles — the bound is
   **≤ 5 % of that sport's cycles over a rolling 24 h** (≈ 720 cycles/sport/day
   at the 120 s interval). Above that, the sport is not merely hiccuping — treat
   it as a capture regression.
2. **Rows still flow.** The affected sport still writes ARCADIA rows within the
   same window (`odds_snapshots` joined to `sports.key = 'pinnacle_<sport>'`).
   A sport that warns **and** writes zero rows for hours is NOT benign.
3. **Coverage context.** If the sport has upcoming events in the Pinnacle
   namespace, some cycles must capture them. Zero rows while events exist =
   investigate. Zero rows with zero upcoming events = off-season, benign.

## Measured baseline (2026-07-04, healthy)

| Signal | Value | Bound | Verdict |
|---|---|---|---|
| No-data warnings, all sports, last 24 h | 3 | ≤ ~5 %/sport/day (~36/day/sport) | inside |
| ARCADIA rows last 2 h — basketball | 856 | > 0 | flowing |
| ARCADIA rows last 2 h — american_football | 48 | > 0 | flowing |
| ARCADIA rows last 2 h — soccer / tennis | 3417 / 336 | > 0 | flowing |
| Upcoming events (7 d) — basketball / NFL | 67 / 2 | context | events exist, captured |

All four sports — including basketball and NFL, the ones this warning names most
often — were capturing rows while the occasional single-cycle warning appeared.
Confirmed benign.

## Checks (read-only prod)

```bash
# warning rate by sport, last 24h
docker compose --profile prod logs --since 24h app | grep -oE "no data for [a-z_]+ this cycle" | sort | uniq -c

# are rows still flowing per arcadia sport, last 2h
docker exec betting-ai-postgres-1 psql -U betting_ai -d betting_ai -c "SELECT s.key, count(*), max(os.ingested_at) FROM odds_snapshots os JOIN events e ON e.id=os.event_id JOIN sports s ON s.id=e.sport_id WHERE s.key LIKE 'pinnacle%' AND os.ingested_at >= now()-interval '2 hours' GROUP BY 1;"

# upcoming events per arcadia sport (is a zero-row sport simply out of fixtures?)
docker exec betting-ai-postgres-1 psql -U betting_ai -d betting_ai -c "SELECT s.key, count(*) FROM events e JOIN sports s ON s.id=e.sport_id WHERE s.key LIKE 'pinnacle%' AND e.starts_at >= now() AND e.starts_at < now()+interval '7 days' GROUP BY 1;"
```

## If the bound is breached

A sport warning **> 5 % of cycles/day AND writing no rows while it has upcoming
events** is a real capture regression (proxy/egress fault, discovery-endpoint
change, or namespace drift — see [[pinnacle-arcadia-needs-proxy]]). Not benign;
investigate the ARCADIA loader and proxy pool.

## Noted, not built

`self_audit` could enforce bound (1) automatically — a per-sport no-data-rate
evaluator alongside `evaluate_proxy_headroom` / `evaluate_listing_recovery` —
firing WARN when a sport crosses the 5 %/24 h line while events exist. Cheap and
in-pattern; deferred until the manual check proves insufficient.
