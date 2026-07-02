# HOW TO RUN — verify the platform end to end

Everything below is read-only market data and informational picks.
**The system never places bets; you review and bet manually if you choose.**

## 0) One-time setup (Mac, ~5 minutes)

```bash
cd "/Users/alexis/code/Betting Picks Bot"
cp .env.example .env                  # safe defaults; no keys required
docker compose up -d postgres redis   # local infra on ports 5433/6380
uv sync --extra football --extra backfill
uv run playwright install chromium    # for the free OddsPortal live scrape
uv run alembic upgrade head           # create the 14-table warehouse
```

## 1) Prove the strategy (re-runnable backtest, ~3 minutes)

```bash
uv run python scripts/value_backtest.py
```

Downloads 7 seasons × 18 leagues × 2 markets (~46k matches) from
football-data.co.uk, sweeps devig × threshold on TRAIN seasons only, then
evaluates the chosen combo ONCE on held-out 2024-26. Expected output ends
with the computed verdict (historically: shin devig, edge ≥ 0.03 → holdout
n=62, ROI +22.4%, incremental CLV +0.1066 > 2SE). The verdict is computed
from the data — if the edge ever disappears, the script will say so.

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
uv run python scripts/value_picks.py --league world-cup --min-edge 0.03
# more volume at the thinner validated tier:
uv run python scripts/value_picks.py --league world-cup --min-edge 0.015
```

Scrapes free multi-book OddsPortal odds, anchors fair value on the sharpest
book (or ≥3-book median consensus), prints each value pick with the exact
bookmaker, price, edge, and recommended fractional-Kelly stake.

## 3) Run the full platform (scheduler + DB + alerts + API)

```bash
uv run uvicorn app.main:app
```

What runs (defaults from `.env`/`app/config.py` — the v3-validated config:
`PICK_STRATEGY=value`, `VALUE_DEVIG=shin`, `VALUE_MIN_EDGE=0.03`):

- every 5 min: scrape OddsPortal → find value picks → persist → alert
  (Telegram/webhook if configured in `.env`)
- every 30 min: CLV true-up — refreshes each open pick's closing fair
  probability and `clv_log` (the live proof of edge)

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
  is down or its first multi-league cycle is still running. `GET /health`
  shows per-sport poll timestamps (`polls`) and upstream release checks.
- Picks come from the **best price across all scraped bookmakers** (~16
  books per market on OddsPortal) — the named book held the best price at
  scrape time; it is not a single-bookie feed.

Or raw JSON:

```bash
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
uv run pytest -q              # 173 tests, all green
uvx ruff check app tests      # lint
uv run mypy app tests         # types
bash scripts/safety_audit.sh  # proves no bet-placement code path exists
```

## What to watch over time

The discipline that keeps this honest is **live CLV**: every pick's
`clv_log` is trued-up until kickoff and frozen at settlement. The strategy
version is only trusted while its stake-weighted CLV stays positive — that
is the same number the backtest validated (incremental CLV > 2SE), now
measured on your own picks. ROI on small samples is noise; CLV is signal.
