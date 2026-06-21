# Historical odds datasets for backtesting — web + GitHub hunt (2026-06-21)

5-agent sweep for FREE historical datasets that carry **closing odds + results**
(results-only sets are useless for ROI/CLV — most public sports data is exactly
that). Goal: extend the validated football backtest (see
value-backtest-2026-06-21.md) and find a sharp close for the other sports.

## ADOPT — best new sources

### Football — `jokecamp/FootballData` (MIT) — top pick

- **What:** a NAMED **Pinnacle** 1X2 + Asian-Handicap close for **22 countries**,
  many leagues football-data.co.uk does NOT cover (Austria, China, Finland,
  Iceland, Israel, Norway, Poland, Sweden, Switzerland, Czech, Denmark, + top-5).
- **Depth:** 86,337 odds-bearing rows, 2004-2016 (frozen). `all.csv`/`all.db`
  committed in-repo, MIT.
- **Odds quality:** close-GRADE — betexplorer's last-displayed Pinnacle line, a
  single near-close snapshot (not a documented closing tick, no open->close pair).
  No soft best-price column -> pair its Pinnacle close against our own captured
  best-price or football-data's Max for the same fixture.
- **Ingest:** new `app/ingestion/jokecamp_football.py` mirroring `football_data.py`
  — one read-only httpx GET of raw `all.csv`, frozen model, team-name normalization.

### Football (secondary) — `BeatTheBookie` (arXiv 1710.02824 / Kaggle)

- **~880k matches, 2000-2015, 912 leagues worldwide** — an order of magnitude
  beyond football-data.co.uk. `avg`/`max`/`top_bookie` columns (best-price-vs-
  consensus), **NOT** a named Pinnacle close. For best-price-edge-vs-consensus ROI
  screening only. Academic/Kaggle data terms (mirror repo has no license — cite the paper).

### NFL — `flancast90/sportsbookreview-scraper` (MIT)

- `nfl_archive_10Y.json`: SportsbookReview consensus close **with a real open->close
  pair** on spread+total — better than nflverse's single snapshot, still consensus
  (not Pinnacle). Extend `nfl_data.py` with `parse_sbr_nfl`. **NFL stays
  visibility-only** until forward CLV >2 SE (ADR-0017).

### NBA — `erichqiu/nba-odds-and-scores` (Kaggle CC0)

- Isolates **Pinnacle + 4 soft books** across all 3 markets — best free NBA
  structure — but a single per-game snapshot (no open/close), 2012-2019, provenance
  unverified. A HISTORICAL backtest base only (arcadia handles forward capture).
  `cviaxmiwnptr/nba-betting-data` (CC0, 2007-2024) = consensus+results for ROI
  screening (no sharp leg; segment at the 2023 source seam).

## Honest gaps (the recurring truth)

- **Nothing replicates football-data.co.uk's free Pinnacle OPEN+CLOSE pairing for
  any sport other than football.** Every other "sharp" find is a single close-grade
  snapshot or a consensus line.
- **NBA has no free historical Pinnacle CLOSE** — only snapshots/consensus; the
  project's own arcadia capture remains the sole true NBA sharp close.
- **Tennis adds ZERO** — every GitHub/Kaggle tennis-odds repo mirrors
  tennis-data.co.uk (already ingested; PSW/PSL = Pinnacle).
- **Most public sports data is results-only** (e.g. `schochastics/football-data`,
  1.24M games 1888-2023 — no odds -> rejected for CLV/ROI).
- **All adopts are FROZEN backfills** (jokecamp 2016, BeatTheBookie 2015, flancast
  2021, erichqiu 2019) — they extend the historical backtest's BREADTH, not recency
  or forward capture.
- **Team-name normalization is a real cross-join risk for every adopt** — a wrong
  match silently corrupts results; build + test a name map before any join.
- Rejected: paid-Gumroad teaser sets (50-row samples), unlicensed mirrors, and
  repos that describe Pinnacle data but ship none (MatejFrnka -> Hopsworks only).

## Recommended next step

Adopt **jokecamp/FootballData** first — the only free NAMED-Pinnacle close beyond
football-data.co.uk's leagues — via a read-only backfill loader, then re-run the
value backtest across the combined league set to test whether the held-out
+22% ROI / CLV >2 SE edge holds on the wider, independent sample.
