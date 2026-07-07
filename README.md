<div align="center">

<img src="docs/assets/logo.svg" alt="sharp-ev-picks — picks-only sports-market analytics" width="560">

**A picks-only sports-market analytics platform.**

Read-only odds ingestion · sharp-anchor devig & line shopping · trusted-CLV and source-quality tracking · evidence-first dashboard.
You review every pick and place any bet yourself — the system never does.

[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![FastAPI](https://img.shields.io/badge/FastAPI-async-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-async-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org)
[![Docker](https://img.shields.io/badge/Docker-compose-2496ED?logo=docker&logoColor=white)](Dockerfile)
[![Lint: Ruff](https://img.shields.io/badge/lint-ruff-261230?logo=ruff&logoColor=white)](https://docs.astral.sh/ruff/)
[![CI](https://github.com/alexandrosh8/sharp-ev-picks/actions/workflows/ci.yml/badge.svg)](https://github.com/alexandrosh8/sharp-ev-picks/actions/workflows/ci.yml)
[![Safety: picks-only · no auto-bet](https://img.shields.io/badge/safety-picks--only%20%C2%B7%20no%20auto--bet-22c55e)](#-safety--read-this-first)

[Install](#install--run) · [How it works](#how-it-works) · [Sports](#sports-coverage) · [Validation](#validation-protocol-adr-0019) · [Dashboard](#dashboard--signaldesk) · [Configuration](#configuration) · [Architecture](#architecture) · [Docs](#documentation)

</div>

---

## 🔒 Safety — read this first

> **This system never places bets.** It surfaces candidate picks for manual review; **you** decide and place any bet personally, on your own accounts.
>
> There is **no** bet-execution path, **no** bookmaker login automation, **no** stored betting credentials, and **no** auto-betting flag — by design. Every market-data integration is **read-only (GET)**. A CI safety audit (`scripts/safety_audit.sh`) fails the build if a bet-placement path ever appears. Recommended stakes, edges and EV are informational only — betting involves risk and nothing here is a guarantee of profit.

## How it works

The doctrine, in order of importance:

1. **Standalone models are not trusted just because they produce picks.** The project's own backtesting found a goals model alone does not beat the market.
2. **Sharp-vs-soft line shopping is the core pricing idea.** Fair value is priced from the sharpest available book (Pinnacle; optionally Betfair Exchange), the vig is stripped (8 named, 6 distinct parity-tested devig methods), and a candidate exists only when a softer book's price materially beats that fair value.
3. **Candidates are gated by evidence and freshness.** A premium candidate without a real sharp anchor can be demoted to the shadow tier (`VALUE_REQUIRE_SHARP_ANCHOR`); exchange anchors must clear a liquidity floor; odds older than the freshness window are **discarded, never used** (fail-closed).
4. **CLV proves or disproves edge over time — and only *trusted* CLV counts.** A closing line anchored by a pick's own fill book, an unmoved (tautological) line, a circular same-market close, or a fabricated/implausible close is **excluded from evidence**, not averaged in. Small-sample ROI is noise; all-row CLV is not proof. The quality bar for any claim is trusted CLV with a sufficient sample, acceptable freshness, source agreement, and reliable settlement.

**Historical backtests** (labeled historical — not live proof): an 18-league, 7-season sweep with a pre-registered holdout showed positive held-out CLV for the sharp-vs-soft method, with documented limitations (gross-fill optimism, correlated-sample SEs — since corrected to cluster-robust). That holdout is **spent**, and a later pre-registered single-shot on early-2026 data failed its sample-size bar due to a data-coverage anomaly, neither validating nor refuting the strategy. Details and caveats: [`docs/backtesting/`](docs/backtesting/) and [ADR-0019](docs/adr/). **Live trusted-CLV accrual is the primary evidence path today, and it is not yet conclusive.**

> **On the OddsChecker default (added 2026-07-05):** those historical backtests are *method-level* and were measured on OddsPortal-era book coverage and team-name forms. Switching the default odds provider to OddsChecker does **not** inherit that evidence — its book set, matching, and closing-line capture differ, so its live trusted-CLV starts accruing from zero. A provider-specific backtest is not possible until settled results and closing lines accrue on the new source; until then OddsChecker picks are treated as unvalidated (shadow-first), exactly like any new source. No historical claim on this README implies the OddsChecker-sourced live system is validated.

## Sports coverage

A sport is *shown* once it is scrapeable; it *mints premium picks* only where the pipeline is enabled — and any promotion beyond that requires an evidence review (trusted CLV, sample size, freshness, source agreement, settlement reliability), never an env flip.

| Sport | Status | Notes |
| --- | --- | --- |
| **Football / Soccer** | ✅ Live pick source (benchmark) | The enabled pipeline. Historical held-out evidence supports the method; **live trusted CLV is still accruing and not yet conclusive** — stated on the dashboard, not hidden. |
| **Basketball** (incl. NBA) | ⚠️ Shadow — evidence accruing | Closest shadow candidate on current exploratory reports; **not promotable** (sample marginal, source-agreement coverage thin). |
| **Tennis** (ATP / WTA) | 🚧 Shadow / display-only | Settlement now follows the `pinnacle_one_set` convention (see below); sharp-close capture evidence still accruing; no picks minted. |
| **American football** (NFL) | 🚧 Display-only | Too little event volume to evaluate yet. |

## Install & run

Both supported paths run the **same code** and serve the dashboard at **http://localhost:8000/**.

### Option 1 — Your own PC (Windows or Mac)

**Docker Desktop** runs the whole stack (app + Postgres + Redis) with one command:

```bash
git clone https://github.com/alexandrosh8/sharp-ev-picks.git
cd sharp-ev-picks
cp .env.example .env          # Windows PowerShell: Copy-Item .env.example .env
docker compose --profile prod up -d --build
```

Open **http://localhost:8000/**. On first launch a one-time **setup screen** creates your admin password (stored hashed). Stop with `docker compose --profile prod down` (data survives in a Docker volume); logs via `docker compose --profile prod logs -f app`.

### Option 2 — Ubuntu VPS (always-on, 24/7)

The same Docker stack with `restart: unless-stopped`:

```bash
sudo apt install -y docker.io docker-compose-v2 git
sudo git clone https://github.com/alexandrosh8/sharp-ev-picks.git /opt/sharp-ev-picks
sudo chown -R $USER /opt/sharp-ev-picks
cd /opt/sharp-ev-picks
cp .env.example .env
chmod 600 .env
# edit .env (COMPOSE_PROFILES=prod, TELEGRAM_*); create the /setup password
# over an SSH tunnel BEFORE exposing the port
docker compose up -d --build
```

Reach it over an SSH tunnel (`ssh -L 8000:127.0.0.1:8000 <vps>`), or on the VPS IP once dashboard auth is on. Full runbook: [`docs/deployment/openclaw-ubuntu.md`](docs/deployment/openclaw-ubuntu.md).

### Mac / Linux — run natively (no Docker)

```bash
brew install postgresql@16 redis
brew services start postgresql@16
brew services start redis
createdb betting_ai

uv sync --extra football --extra backfill    # NBA: also --extra nba --extra models --extra ml
uv run alembic upgrade head
uv run uvicorn app.main:app --reload         # http://localhost:8000/
```

Prefer not to install the databases? `docker compose up -d postgres redis` and keep the app native. First-time commands live in [`docs/HOW_TO_RUN.md`](docs/HOW_TO_RUN.md). Dev tasks:

```bash
uv run pytest -q                          # 2,400+ tests (no network)
uvx ruff check .                          # lint
uvx ruff format --check app tests scripts # formatting (CI-gated separately)
uv run mypy app tests                     # types
uv run alembic heads                      # single migration head
bash scripts/safety_audit.sh              # no-autobet + secret-leak greps (CI-gated)
```

Evidence/validation tooling (read-only; outputs refuse to overwrite prior runs):

```bash
uv run python scripts/research/sport_quality_report.py --days 30   # per-sport trusted-CLV / freshness / agreement report
uv run python scripts/research/devig_comparison.py                 # validation-only devig method comparison
uv run python scripts/bsp_inventory.py                             # BSP archive/cache inventory + readiness
uv run python scripts/arcadia_anchor_export.py export --from 2026-07-01 --to 2026-12-31
uv run python scripts/arcadia_anchor_export.py preflight --dataset <exported.csv>   # prints PASS or DO-NOT-RUN
```

## Validation protocol (ADR-0019)

The next strategy validation is **pre-registered, signed, and armed — and deliberately has not run**:

- **H2 protocol (signed):** a *pure prospective single-shot* — no train side, no selection on any 2026 data, evaluated once against the pre-registered frozen configuration (hash-pinned) using the project's own Pinnacle ARCADIA capture as the independent sharp anchor and a future Betfair BSP archive as the close.
- **H6 agreement gate (signed, tolerance 0.02):** a validation/shadow-only variant requiring the sharp anchor to agree with an independent multi-book consensus; it records pass/fail/excluded reasons, never silently drops rows, and **is not a live gate or promotion switch**.
- **Guards:** the run is impossible until the future BSP data exists and a coverage preflight passes — the preflight prints **`DO-NOT-RUN`** while data is incomplete (it currently does, correctly), spent-slate sha256 guards block every previously-read dataset, and a frozen-config-hash check stops the run if live settings drift.
- **No validation run has been performed early.** Exploratory readouts are labeled exploratory/spent and are never used to tune frozen thresholds.

Evidence machinery accruing in the background: freshness-stratified trusted-CLV telemetry (anchor-age × mint-to-kickoff × sport × market buckets), monthly per-sport quality reports (coverage, agreement, freshness, settlement, sample sufficiency), and a pre-registered anchor-freshness bound (H8) that stays shadow-only. These reports exist to accrue meaningful samples over time, not to create same-day narratives.

## Dashboard — sharp-ev-picks

A single self-contained page (`app/api/dashboard.html`; no framework, no CDN, installable PWA) styled as a trading-intelligence console. Five workspaces:

- **Today** — command screen: qualified picks, a derived "needs attention" queue (source degraded, low evidence, staleness), next kickoffs, recent results, and a one-line evidence position.
- **Edges** — master-detail pick console (stream → detail → evidence panes on desktop; full-screen sheets on mobile) with explicit trust states: Premium vs **Shadow — tracked, informational** (never styled actionable), stale, weak match, missing anchor, and per-pick close/CLV trust.
- **Radar** — market coverage by kickoff proximity, with DISPLAY-ONLY tags for unvalidated sports.
- **Lab** — the evidence workspace: a claims ledger ("can claim / cannot claim yet"), the **trusted sharp-close CLV** headline kept strictly separate from all-closes context CLV, close-quality exclusions (tautological / circular / fabricated), calibration, **Sport Readiness** (per-sport shadow status and blockers), and sample-size warnings everywhere.
- **Sources** — source-health matrix showing the **active odds provider** (OddsChecker / OddsPortal / The Odds API) and **where the Betfair anchor comes from** under it, plus ARCADIA, Betfair API *monitor-only*, and a proxy-pool panel (redacted) that auto-flags dead/quarantined slots and spare-capacity headroom; staleness monitor, review-queue counts, plain-language H2 validation readiness, and the H6 status line.

No performance claims appear on the dashboard; low-evidence and shadow items are visually incapable of looking actionable.

## Configuration

All secrets live in `.env` only (copy from `.env.example`; `0600`, gitignored — **never commit it**). Every key ships with a safe default — the app works with none of them set. The keys that matter most:

| Key | Default | What it does |
| --- | --- | --- |
| `ODDS_SOURCE` | `oddschecker` | Odds provider (switchable): `oddschecker` (free OddsChecker scrape, **default** — Betfair Exchange inline, all four sports, all markets), `oddsportal` (free OddsPortal scrape), or `odds_api` (The Odds API). |
| `ODDSCHECKER_SPORTS` | `soccer,basketball,tennis,american_football` | Which sports the OddsChecker feed polls (csv). |
| `ODDSCHECKER_CAPTURE_SHARP_MARKETS` | `true` | Also capture every sharp-anchored (Betfair Exchange) prop/period/combo market as odds history (never priced or settled — pure capture). |
| `DASHBOARD_AUTH_ENABLED` | `true` in `.env.example` | First-run `/setup` creates the admin password (stored hashed). `false` = no login. |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | empty | Pick alerts. Blank just disables alerts; the dashboard still works. |
| `VALUE_REQUIRE_SHARP_ANCHOR` | `true` | When `true` (default since 2026-07-07), a premium pick without a real Pinnacle/Betfair anchor demotes to shadow. |
| `SCRAPER_PROXY_POOL` | empty | Optional rotating proxies for the scrape *and* the Pinnacle close capture (see below). |
| `BETFAIR_EXCHANGE_ENABLED` | `false` | Optional read-only Betfair Exchange BACK-odds capture as a second sharp anchor. |
| `ODDSPORTAL_USE_JSON_FEED` | `false` | `true` swaps the per-match Playwright render for a faster `curl_cffi` JSON-feed reader. |

The full key reference (scrape tuning, timeouts, results-settlement cadence) is documented inline in [`.env.example`](.env.example).

**Scrape proxies.** The default **OddsChecker** source is Cloudflare-walled on datacenter-direct egress, so it **requires** a rotating pool to fetch at all; the OddsPortal scrape otherwise runs from your host IP, which can be throttled and only lists your region's books. A rotating pool (`host|port|user|pass` quads, comma-separated) widens coverage and speeds a full slate. The dashboard's proxy-pool panel now flags dead/quarantined slots automatically and shows spare-capacity headroom against the active source's fetch concurrency. The same pool serves as egress for the free **Pinnacle ARCADIA close capture**, which rejects datacenter/direct IPs. Read-only either way; credentials never leave `.env`. On heavy slates, limited capture/proxy capacity can constrain coverage — the freshness gate then **discards** stale candidates rather than minting stale picks.

**Betfair Exchange.** An off-by-default, read-only capture ([ADR-0015](docs/adr/adr-0015-betfair-exchange-back-odds-capture.md)) binds Betfair BACK odds inline on the same canonical event as the soft books. Exchange anchors are liquidity-gated. The separate Betfair API staleness comparison is **monitor-only** — it records verdicts and never demotes live picks.

## Architecture

Proven open-source engines bound into one pipeline:

- **Ingestion** — pluggable odds provider (`ODDS_SOURCE`): the default **OddsChecker** reader (`app/ingestion/oddschecker.py`, read-only `curl_cffi`/Hypernova-JSON, GET-only — football/basketball/tennis/American football, all devig-sound markets, with Betfair Exchange + Sportsbook inline so the sharp anchor travels with the provider; sharp-anchored props/period captured as odds history), or the OddsHarvester-based **OddsPortal** scrape (`app/ingestion/oddsportal.py`, Playwright render or `curl_cffi` JSON feed), or **The Odds API**; free Pinnacle ARCADIA close capture (`app/ingestion/pinnacle_arcadia.py`); optional dedicated Betfair Exchange BACK odds (OddsPortal source only); Betfair BSP archive tooling for validation. All read-only. **OddsChecker is Cloudflare-walled on datacenter-direct egress, so it requires `SCRAPER_PROXY_POOL`.**
- **Pricing** — penaltyblog Dixon-Coles for football (`app/models/football_dc.py`); an 8-method devig (`app/probabilities/devig.py` — multiplicative, additive, power, Shin closed-form, probit, odds-ratio, logarithmic, differential-margin; 6 distinct estimators once proven equivalences are collapsed; parity-tested, with cross-library golden vectors).
- **Edge & risk** — edge/EV gating (`app/edge/value.py`) with sharp/consensus anchor grading and an exchange-liquidity floor; fractional-Kelly sizing (informational) with per-pick and daily exposure caps (`app/risk/`).
- **Resolution / matching** — a precision-hardened cross-source matcher (`app/resolution/`): marker-aware (women/youth/reserve/B sides never collapse onto the senior team), two-tier Jaro-Winkler over a curated alias seed, tennis surname-initial veto, tight kickoff windows, fail-closed on ambiguity, plus a read-only wrong-game self-audit each cycle. **Aliases are applied only through a sanctioned evidence process**: distinct-fixture co-occurrence evidence, dry-run patch review, regression tests, and a matcher differential proving zero unintended merges — generic-base aliases are never forced.
- **Settlement** — automatic from free results feeds with wrong-game guards; tennis follows the declared `pinnacle_one_set` convention (below); anything unclassifiable is left for manual entry, never guessed.
- **Evidence & validation** — trusted-CLV true-up with independence/tautology/circularity exclusions; the pre-registered ARCADIA/BSP validation harness (`app/backtesting/`, `scripts/arcadia_anchor_export.py`) with preflight DO-NOT-RUN and spent-data guards; per-sport quality reporting.
- **Persistence & serving** — Postgres warehouse (SQLAlchemy 2.0 async + Alembic); APScheduler drives polling, settlement, CLV true-up and sharp-close captures; FastAPI serves SignalDesk.
- **Agent/skills discipline** — project skills under `.claude/skills/` encode the research, shadow-engineering, matching and CLV-evidence procedures used to maintain the repo.

**Stack:** Python 3.12 · FastAPI · SQLAlchemy 2.0 async + asyncpg · APScheduler · Redis · PostgreSQL · Playwright (Chromium) · Docker Compose. Pure-math modules (`probabilities`, `edge`, `risk`) take no env/DB/HTTP — policies enter as frozen dataclasses at the composition root.

**Tennis settlement convention** (`pinnacle_one_set`, matching the sharp-anchor book's rule): a walkover or any abnormal ending before one completed set voids all markets; a retirement after at least one completed set grades the moneyline to the advancing player and voids other markets; unclassifiable cases remain unsettled for manual entry. This improves settlement reliability — it does not promote tennis.

## Status — monitor-and-accrue

The project is in **monitor-and-accrue** mode: production is monitored, the validation machinery is signed and armed, and evidence is accruing. Clean monitoring rounds with no changes are the expected outcome. No user-side action is needed unless operational capture capacity changes.

**Current limitations (honest):** trusted-CLV samples are still accruing everywhere and are not yet conclusive for any sport, including the live football pipeline; basketball is the closest shadow candidate but not promotable; NFL lacks volume; tennis settlement is fixed but capture evidence is thin; Betfair staleness stays monitor-only; the H2 validation waits for future BSP data and a preflight PASS; heavy slates can exceed capture capacity, in which case freshness gates fail closed.

**Next evidence milestones:** monthly sport-quality reports; trusted-CLV accrual per sport/market/freshness bucket; source-agreement coverage; settlement reliability confirmation (first live tennis retirement case); the H2 prospective validation when its data exists.

## For future agents

Use the local repository as the source of truth. Do not loosen gates, promote sports without an evidence review, run the H2 validation early, make performance claims, or add any bet-execution path. Prefer tests and evidence over speculative code. The quality bar is trusted CLV, freshness, source agreement, sample size, and settlement reliability. A clean monitoring round with no changes is a valid, successful outcome.

## Documentation

| Path | Contents |
| --- | --- |
| [`docs/adr/`](docs/adr/) | Architecture decision records, including ADR-0019 (pre-registered validation protocol, signed amendments) |
| [`docs/runbooks/`](docs/runbooks/) | Operational runbooks, including the H2 validation single-shot procedure |
| [`docs/research/`](docs/research/) | Research logs, sport-quality reports, BSP readiness, devig comparisons |
| [`docs/backtesting/`](docs/backtesting/) | Historical backtesting methodology & results (spent/exploratory — see caveats inline) |
| [`docs/deployment/`](docs/deployment/) | Mac dev + Ubuntu/OpenClaw deployment guides |
| [`docs/security/`](docs/security/) | Security notes & reviews |
| [`docs/HOW_TO_RUN.md`](docs/HOW_TO_RUN.md) | End-to-end verify-the-backtest & live-picks commands |

## License

[MIT](LICENSE) © 2026 alexandrosh8 — free to use, modify, and distribute; provided "as is", without warranty.

---

<div align="center"><sub>Picks-only decision support · read-only market data · never places bets.</sub></div>
