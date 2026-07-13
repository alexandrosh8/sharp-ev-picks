# HOW TO RUN — verify the platform end to end

Everything below is read-only market data and informational picks.
**The system never places bets; you review and bet manually if you choose.**

## 0) One-time setup (Mac, ~5 minutes)

```bash
cd "/Users/alexis/Documents/Codex/sharp-ev-picks"
cp .env.example .env                  # loopback-only local defaults
docker compose up -d postgres redis   # local infra on ports 5433/6380
uv sync --extra football --extra backfill
.venv/bin/playwright install chromium    # for the free OddsPortal live scrape
.venv/bin/alembic upgrade head         # create/upgrade the warehouse schema
```

## 1) Prove the strategy (re-runnable backtest, ~3 minutes)

```bash
.venv/bin/python scripts/value_backtest.py
```

Downloads the declared historical football-data.co.uk sample, performs the
train-only sweep, and reports the frozen held-out result with clustered
uncertainty. This is a reproducibility check of spent historical evidence, not
a new validation run or a live profitability claim. Current validation status
and caveats are maintained in `README.md` and ADR-0019.

Honesty caveats (audit 2026-07-01): that historical headline fills at the
gross Max across ALL books (exchanges included) and its ">2SE" treated
same-match 1X2+OU picks as independent. The script now (a) gates the verdict
on a cluster-robust by-match SE (the i.i.d. SE stays printed for comparison)
and (b) offers `--fill-universe soft` — best NAMED soft book only, exchange
prices only net of commission — which is closer to a live fill. Treat the
max-book gross number as an upper bound; a defensible re-anchored headline
awaits fresh 2026 data (the 2025 holdout is spent — ADR-0019).

## 2) Get live picks right now (one-shot, no DB needed)

```bash
# World Cup 2026 (or any league slug from oddsportal.com)
.venv/bin/python scripts/value_picks.py --league world-cup --min-edge 0.03
# lower informational threshold (more candidates):
.venv/bin/python scripts/value_picks.py --league world-cup --min-edge 0.015
```

Scrapes free multi-book OddsPortal odds, anchors fair value on the sharpest
book (or ≥3-book median consensus), prints each value pick with the exact
bookmaker, price, edge, and recommended fractional-Kelly stake.

## 3) Run the full platform (scheduler + DB + alerts + API)

```bash
.venv/bin/python -m uvicorn app.main:app
```

What runs (current defaults from `.env`/`app/config.py`:
`ODDS_SOURCE=oddsportal`, `PICK_STRATEGY=value`, `VALUE_DEVIG=power`,
`VALUE_MIN_EDGE=0.03`):

- every 5 min: scrape OddsPortal → find value picks → persist → alert
  (Telegram/webhook if configured in `.env`)
- each completed poll: CLV true-up/revalidation on that cycle's bounded,
  fresh snapshots; stale or unknown-kickoff candidates never mint picks

Check it — **open the dashboard in your browser**:

```
http://localhost:8000/
```

Crystal view of every pick: match, kickoff (your local time), market,
selection, book, odds, edge, recommended stake, CLV badge, status — with
search, status filter, summary cards, and 60s auto-refresh.

**Reading the dashboard** (so a quiet screen isn't mistaken for a broken
one — and a stale one isn't mistaken for healthy):

- It lists **value picks, not the fixture schedule**. A game with no pick
  means no book beat the sharp fair price by ≥ the edge gate — typical for
  heavily-traded matches (e.g. a World Cup opener). Off-season leagues
  yield nothing until they resume.
- Each cycle scrapes **today + tomorrow (UTC)** per configured league
  (`ODDSPORTAL_DAYS_AHEAD`); far-future fixtures are skipped by design.
- Every row shows **"picked Xh ago — verify price"**: always re-check the
  book's current price before acting; soft-book prices move.
- A ⚠ banner appears when **no odds poll finished in 45 min** — the engine
  is down or its first multi-league cycle is still running. authenticated `GET /health` shows per-sport poll timestamps (`polls`) and
  upstream release checks; anonymous health responses are redacted.
- Picks come from the **best price across all scraped bookmakers** (~16
  books per market on OddsPortal) — the named book held the best price at
  scrape time; it is not a single-bookie feed.

Or raw JSON:

```bash
curl localhost:8000/live             # process liveness only
curl localhost:8000/ready            # readiness status; login for component detail
curl localhost:8000/health
curl localhost:8000/picks            # picks with book, price, edge, stake
```

Record a result you bet manually (informational tracking):

```bash
curl -X POST localhost:8000/picks/<pick_id>/result \
  -H 'content-type: application/json' \
  -d '{"pick_id":"<pick_id>","outcome":"won","bet_placed":true,
       "actual_stake":"10","actual_odds":2.1,
       "settled_at":"2026-06-10T20:00:00Z"}'
```

Useful env overrides (in `.env`):

```bash
ODDSPORTAL_FOOTBALL_LEAGUES=brazil-serie-a   # csv of oddsportal slugs
VALUE_MIN_EDGE=0.015                          # volume tier (more picks)
TELEGRAM_BOT_TOKEN=... / TELEGRAM_CHAT_ID=... # to receive alerts
```

## 4) Verify the codebase health (what CI runs)

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check app tests scripts alembic tools
.venv/bin/python -m ruff format --check app tests scripts alembic tools
.venv/bin/python -m mypy app tests
bash scripts/safety_audit.sh
```

## What to watch over time

The discipline that keeps this honest is **live CLV**: every pick's
`clv_log` is trued-up until kickoff and frozen at settlement. The strategy
version is only trusted while its stake-weighted CLV stays positive — that
is the same number the backtest validated (incremental CLV > 2SE), now
measured on your own picks. ROI on small samples is noise; CLV is signal.
