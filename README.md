<div align="center">

<img src="docs/assets/logo.svg" alt="betting-ai — manual-betting +EV picks platform" width="560">

**A picks-only +EV decision-support platform for football &amp; basketball.**

Sharp-vs-soft line shopping · vig-stripped edges · fractional-Kelly sizing · live Closing Line Value tracking.
You review every pick and place any bet yourself — the system never does.

[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![FastAPI](https://img.shields.io/badge/FastAPI-async-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-async-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org)
[![Docker](https://img.shields.io/badge/Docker-compose-2496ED?logo=docker&logoColor=white)](Dockerfile)
[![Lint: Ruff](https://img.shields.io/badge/lint-ruff-261230?logo=ruff&logoColor=white)](https://docs.astral.sh/ruff/)
[![CI](https://github.com/alexandrosh8/betting-picks-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/alexandrosh8/betting-picks-bot/actions/workflows/ci.yml)
[![Safety: picks-only · no auto-bet](https://img.shields.io/badge/safety-picks--only%20%C2%B7%20no%20auto--bet-22c55e)](#-safety--read-this-first)

[Install](#install--run) · [How it works](#how-it-works-backtested-positive-clv) · [Sports](#sports-coverage) · [Configuration](#configuration) · [Architecture](#architecture) · [Docs](#documentation)

</div>

---

## 🔒 Safety — read this first

> **This system never places bets.** It surfaces +EV picks for manual review; **you** decide and place any bet personally, on your own accounts.
>
> There is **no** bet-execution path, **no** bookmaker login automation, **no** stored betting credentials, and **no** auto-betting flag — by design. Every market-data integration is **read-only (GET)**. A CI safety audit (`scripts/safety_audit.sh`) fails the build if a bet-placement path ever appears. Recommended stakes, edges and EV are informational only — betting involves risk and nothing here is a guarantee of profit.

## How it works (backtested positive CLV)

The honest result of backtesting (`docs/backtesting/`): a goals model (Dixon-Coles) does **not** beat the market — negative CLV. But **sharp-vs-soft line shopping does** — price fair value from the sharpest book (Pinnacle), then bet a softer book whose price beats it.

The v3 maximal-data run (18 leagues × 7 seasons × two markets, **46k matches**; devig × edge threshold swept on TRAIN only, then a single pre-registered holdout) chose **shin devig, edge ≥ 0.03**. Held-out 2024–26:

| Tier | n | ROI | Incremental CLV | Notes |
| --- | --- | --- | --- | --- |
| **Premium** (live default) | 62 | **+22.4%** | **+0.107** ( > 2 SE ) | positive even vs the Max-of-books close; 1X2 and OU2.5 each positive |
| Volume (shadow) | 379 | +2.5% | +0.019 | tracked, never alerted |

The number to trust is the **CLV** — small-sample ROI is noisy. A sport only earns **alerting picks** after its own held-out incremental CLV clears **> 2 SE**; everything else is visibility-only (scraped, shown, tracked — but pick-free and exposure-free, enforced in both the scheduler and the warehouse path).

The strategy is wired into the running app as the default (`PICK_STRATEGY=value`): the scheduler polls odds, strips vig (7 methods, parity-tested), gates +EV edges, sizes fractional Kelly, alerts, and a 30-minute **CLV true-up** refreshes each open pick's closing-line value — the live discipline that proves (or disproves) edge over time.

```bash
uv run python scripts/value_backtest.py     # reproduce the backtest
uv run python scripts/value_picks.py --league world-cup --min-edge 0.015
```

## Sports coverage

| Sport | Status | Notes |
| --- | --- | --- |
| **Football / Soccer** | ✅ Pick source *(validated)* | The held-out CLV edge lives here — sharp-vs-soft line shopping. |
| **Basketball** (NBA / EuroLeague) | ✅ Pick source | Moneyline + main totals; same devig → edge gate. |
| **Tennis** (ATP / WTA) | 👁 Visibility-only | Scraped and shown `UNVALIDATED`; mints **no** picks until matched closing-line volume clears the CLV bar (data-short today). |
| **American football** (NFL / NCAA / CFL) | 👁 Visibility-only | In-season games shown; a free Pinnacle close is forward-captured so it can eventually be CLV-graded. |

> **Getting odds ≠ getting picks.** A sport is shown the moment we can scrape it, but it only mints picks once its *own* closing-line evidence proves an edge. Tennis and American football have odds flowing but not yet enough matched sharp closes to graduate.

## Install &amp; run

Both supported paths run the **same code** and serve the picks dashboard at **http://localhost:8000/**.

### Option 1 — Your own PC (Windows or Mac)

**Docker Desktop** runs the whole stack (app + Postgres + Redis) with one command — no Python, no database to install.

1. Install **[Docker Desktop](https://www.docker.com/products/docker-desktop/)** and start it.
2. Get the code and a config file:

   ```bash
   git clone https://github.com/alexandrosh8/betting-picks-bot.git
   cd betting-picks-bot
   cp .env.example .env          # Windows PowerShell: Copy-Item .env.example .env
   ```

3. Build and start (the first build downloads Chromium — a few minutes):

   ```bash
   docker compose --profile prod up -d --build
   ```

4. Open **http://localhost:8000/**.

Stop with `docker compose --profile prod down` (data is kept in a Docker volume); restart with `docker compose --profile prod up -d`. Logs: `docker compose --profile prod logs -f app`.

On first launch the dashboard shows a one-time **setup screen** to create your admin password (stored hashed, never in the file). Prefer no login on your own PC? Set `DASHBOARD_AUTH_ENABLED=false` in `.env`.

### Option 2 — Ubuntu VPS / OpenClaw (always-on, 24/7)

The same Docker stack on a server, with `restart: unless-stopped` so it survives reboots and crashes:

```bash
sudo apt install -y docker.io docker-compose-v2 git      # if Docker is missing
sudo git clone https://github.com/alexandrosh8/betting-picks-bot.git /opt/betting-ai
sudo chown -R $USER /opt/betting-ai
cd /opt/betting-ai
cp .env.example .env
chmod 600 .env
# edit .env: uncomment COMPOSE_PROFILES=prod, set TELEGRAM_*; create the /setup
# password over an SSH tunnel BEFORE exposing the port. Public IP? set APP_HOST_BIND=0.0.0.0
docker compose up -d --build
```

Reach it over an SSH tunnel (`ssh -L 8000:127.0.0.1:8000 <vps>`, then http://localhost:8000/), or on the VPS IP once dashboard auth is on. Full runbook — every `.env` key, public-IP hardening, logs, backups, troubleshooting: **[`docs/deployment/openclaw-ubuntu.md`](docs/deployment/openclaw-ubuntu.md)**.

### Developer mode (Mac / Linux, host Python)

Hot-reload for development — the app runs on the host, only Postgres/Redis are containerized:

```bash
docker compose up -d postgres redis
uv sync --extra football --extra backfill
uv run playwright install chromium
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

New here? **[`docs/HOW_TO_RUN.md`](docs/HOW_TO_RUN.md)** has the exact verify-the-backtest and live-picks commands. Common dev tasks:

```bash
uv run pytest -q                 # tests (no network; httpx.MockTransport + fakeredis)
uvx ruff check .                 # lint
uv run mypy app tests            # types
bash scripts/safety_audit.sh     # no-autobet + secret-leak greps (CI-gated)
```

## Configuration

All secrets live in `.env` only (copy from `.env.example`; `.env` is `0600` and gitignored — **never commit it**). Highlights:

| Key | Default | What it does |
| --- | --- | --- |
| `ODDS_SOURCE` | `oddsportal` | Free OddsPortal scrape (default) or `odds_api` (The Odds API, includes Pinnacle). |
| `DASHBOARD_AUTH_ENABLED` | `true` | First-run `/setup` creates the admin password (stored hashed). `false` = no login. |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | empty | Pick alerts. Blank just disables alerts; the dashboard still works. |
| `SCRAPER_PROXY_POOL` | empty | Optional rotating proxy pool for the scrape — see below. |
| `BETFAIR_EXCHANGE_ENABLED` | `false` | Optional read-only Betfair Exchange BACK-odds capture — see below. |

### Rotating scrape proxies (more soft books, no IP throttle)

The free OddsPortal scrape runs from your host IP — which OddsPortal can throttle (it starts returning empty pages) and which only shows the books available in *your* region (often a thin, crypto-heavy set). A proxy pool rotates the outbound IP (no single IP gets throttled) and, via a deeper-market region, surfaces far more soft books to shop — a **UK** exit lists ~18 mainstream books (Sky Bet, Paddy Power, William Hill, BetVictor, Betfred, Betway, bet365…) vs ~5 from a region-restricted IP.

```bash
# .env — comma-separated host|port|user|pass quads. Empty = scrape from the host IP.
SCRAPER_PROXY_POOL=host1|port1|user1|pass1,host2|port2|user2|pass2
```

- **Rotation + failover** — the scraper tries proxies in turn and fails over on an error *or* a zero-match result (the throttle signature), capped so an empty slate never burns the whole pool.
- **Read-only + safe** — the proxy only changes the outbound IP of GET odds requests; no login, cookies, or order path. Credentials reach the browser as separate fields (never in a logged URL) and live only in `.env`. These are *infrastructure* proxies, not betting accounts.

> A UK exit hides **Pinnacle** (UK-restricted); the primary sharp anchor is the free Pinnacle **ARCADIA** close (a geo-independent read-only feed), not the scrape.

### Betfair Exchange capture (optional second sharp anchor)

OddsPortal *does* list **Betfair Exchange** — in a separate "Betting exchanges" table that the default scrape doesn't parse. An optional, **off-by-default**, read-only reader (`app/ingestion/betfair_exchange.py`, [ADR-0015](docs/adr/adr-0015-betfair-exchange-back-odds-capture.md)) captures its BACK odds (fractional → decimal, liquidity-gated) as a second free sharp reference, stored in an **isolated** `betfair_<sport>` namespace that mints nothing — mirroring the Pinnacle ARCADIA pattern. Coverage is liquidity-gated (major matches yes, obscure ones no). Set `BETFAIR_EXCHANGE_ENABLED=true` to enable.

## Architecture

The live spine uses proven open-source engines directly, bound into one pipeline:

- **OddsHarvester** scrapes free pre-match odds from oddsportal.com → `app/ingestion/oddsportal.py` (read-only; OddsPortal is an aggregator, not a bookmaker).
- **penaltyblog** Dixon-Coles prices football, fitted on free football-data.co.uk history → `app/models/football_dc.py`.
- The app owns the **+EV core**: a 7-method devig (`app/probabilities/devig.py`, parity-tested 1e-8), edge/EV gate (`app/edge/value.py`), fractional-Kelly sizing with exposure caps (`app/risk/`), and a strict no-fuzzy cross-source matcher (`app/resolution/`) for CLV resolution.
- **Sharp anchors:** the free Pinnacle ARCADIA close (`app/ingestion/pinnacle_arcadia.py`), and optionally Betfair Exchange BACK odds — both isolated, both read-only.
- Picks persist to **Postgres** (SQLAlchemy 2.0 async + Alembic, 14-table warehouse) and serve via `GET /picks`; **APScheduler** drives polling, settlement, CLV true-up, and the sharp-anchor captures; **FastAPI** serves the "PICKS TERMINAL" dashboard.

**Stack:** Python 3.12 · FastAPI · SQLAlchemy 2.0 async + asyncpg · APScheduler · Redis · PostgreSQL · Playwright (Chromium) · Docker Compose. Pure-math modules (`probabilities`, `edge`, `risk`) take no env/DB/HTTP — policies enter as frozen dataclasses at the composition root.

```bash
export ODDS_SOURCE=oddsportal
export ODDSPORTAL_FOOTBALL_LEAGUES=brazil-serie-a
export FOOTBALLDATA_NEW_LEAGUE_CODE=BRA          # train DC on Brazil history
uv run uvicorn app.main:app
open http://localhost:8000/                       # the picks dashboard
```

## Project status

- [x] Validated pick finder — sharp-vs-soft value strategy, v3 maximal-data backtest (46k matches, holdout incremental CLV > 2 SE), wired as the default live pipeline with 30-min CLV true-up (800+ tests).
- [x] Settlement engine — soccer auto-settles from free results feeds; NBA/EuroLeague settle manually; the dashboard SETTLED view shows the final **Score** + result + P&amp;L + CLV, and the manual settle prompt **pre-fills** the scraped final score when available.
- [x] Sharp anchors — free Pinnacle ARCADIA close (forward-captured) + optional isolated Betfair Exchange BACK-odds capture (off-by-default).
- [x] Multisport visibility — Tennis (ATP/WTA) + American football (NFL/NCAA/CFL) as visibility-only feeds (scraped, shown `UNVALIDATED`, no picks); per-sport CLV-readiness probe; tennis surname-initial name reconciliation + CC0 cross-source alias seed for soccer coverage.
- [x] Dashboard "PICKS TERMINAL" — proof-led redesign (desktop + mobile, 1–5★ confidence, clickable sorting, segmented LIVE / UNVERIFIED / CLOSED / SETTLED tabs, first-run `/setup` admin password stored hashed).
- [x] Rotating scrape proxies — optional `SCRAPER_PROXY_POOL` (rotation + capped failover, off by default) widening soft-book coverage (~18 UK books vs ~5 from a region-restricted IP). Read-only; creds in `.env` only.
- [ ] Next: bankroll tracking (phase 6) + a validated NBA model (phase 5).

## Documentation

| Path | Contents |
| --- | --- |
| [`docs/adr/`](docs/adr/) | Architecture decision records |
| [`docs/research/`](docs/research/) | Repository &amp; data-source research logs |
| [`docs/backtesting/`](docs/backtesting/) | Backtesting methodology &amp; results |
| [`docs/deployment/`](docs/deployment/) | Mac dev + Ubuntu/OpenClaw deployment guides |
| [`docs/security/`](docs/security/) | Security notes &amp; reviews |
| [`docs/HOW_TO_RUN.md`](docs/HOW_TO_RUN.md) | End-to-end verify-the-backtest &amp; live-picks commands |

---

<div align="center"><sub>Picks-only decision support · read-only market data · never places bets.</sub></div>
