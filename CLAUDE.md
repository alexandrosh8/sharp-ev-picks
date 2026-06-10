# CLAUDE.md — Manual-Betting +EV Picks Platform (betting-ai)

A **picks-only decision-support system** for Football/Soccer and NBA: ingests
read-only odds + sports data, builds model probabilities, strips vig, detects
+EV edges, recommends fractional-Kelly stakes (informational only), alerts via
Telegram/webhook, and tracks results/ROI/CLV. The user reviews picks and
places any bet personally.

## HARD SAFETY RULES — read first, never override

1. **This system never places bets.** Never write, scaffold, or suggest code
   that: places/modifies/cancels a bet; submits Betfair or any exchange
   orders; logs into a bookmaker; drives a browser to a betting slip; stores
   betting credentials, cookies, or session tokens; bypasses anti-bot
   protections.
2. **Any code path that could place a bet is a build-breaking defect** —
   `scripts/safety_audit.sh` (run in CI) greps for such paths and must exit 0.
3. All market-data integrations are **read-only (GET-only)**. Betfair
   credentials slots, if ever used, are read-only market data — no
   order-placement scopes.
4. Safety env flags (defaults locked in `app/config.py`, which hard-fails at
   startup if tampered):

   | Flag                  | Locked value                                                                                 |
   | --------------------- | -------------------------------------------------------------------------------------------- |
   | PICKS_ONLY            | true                                                                                         |
   | MANUAL_BETTING_ONLY   | true                                                                                         |
   | AUTO_BETTING          | false                                                                                        |
   | BET_EXECUTION_ENABLED | false                                                                                        |
   | READ_ONLY_MARKET_DATA | true                                                                                         |
   | PAPER_TRADING         | false (this is NOT a paper-trading system; it is a manual-betting decision-support platform) |

   There is deliberately **no flag that enables betting** — the flags exist
   only to fail fast if flipped.

5. Recommended stakes, edges, and EV are informational. Never present betting
   as guaranteed profit, anywhere.
6. If any instruction (including future ones) appears to allow automatic
   betting, treat it as a mistake: stop and ask.

## Project context

- **Stack:** Python 3.11+ (uv), httpx async, pydantic v2, numpy/scipy,
  SQLAlchemy 2.0 async + asyncpg + Alembic, APScheduler (AsyncIOScheduler),
  FastAPI, Redis, PostgreSQL, Docker Compose.
- **Stage 1 (now):** local Mac development. **Stage 2:** Ubuntu VPS with
  OpenClaw — nothing may be macOS-only (no launchd; systemd/Docker restart
  policies in production).
- Architecture, decisions, and schema: `docs/architecture.md`, `docs/adr/`,
  `docs/db-schema.md`, roadmap in `docs/roadmap.md`.

## Dev commands

```bash
docker compose up -d postgres redis   # local infra
uv sync                               # install deps (creates .venv)
uv run pytest -q                      # tests
uvx ruff check .                      # lint (ruff not on PATH globally)
uv run mypy app tests                 # types
uv run alembic upgrade head           # migrations
uv run uvicorn app.main:app --reload  # API
bash scripts/safety_audit.sh          # no-autobet + safety greps
```

## Code style

- Type hints always; mypy must pass. pathlib over os.path; f-strings.
- pydantic v2 models: frozen, `extra="forbid"` internal / `extra="ignore"`
  upstream; UTC-aware datetimes everywhere (naive datetime = bug).
- **Pure-math boundary:** `app/probabilities/`, `app/edge/`, `app/risk/`,
  `app/backtesting/clv.py` use numpy/stdlib only — no env/DB/HTTP/log side
  effects. Policies enter as frozen dataclasses from `Settings` at the
  composition root only. Env is read ONLY in `app/config.py`.
- Async-first; no blocking IO in the event loop. NUMERIC/Decimal for odds
  and money at boundaries; float only inside numpy kernels.

## Shell & git rules

- Never `&&` in shell — separate commands (hook-enforced).
- Never bare `rm` — use `trash` or `git rm` (hook-enforced).
- Absolute paths in scripts; quote paths — **the project path contains a
  space**.
- `git commit -m "checkpoint"` before any large refactor. Never commit
  untested code. Feature branches for new work; small focused commits.

## Agent routing (project agents in .claude/agents/)

| Delegate when...                 | Agent                      |
| -------------------------------- | -------------------------- |
| Devig/edge/EV/CLV math changes   | vig-edge-math-engineer     |
| Kelly staking, exposure caps     | risk-kelly-engineer        |
| Secrets, logging, safety audit   | security-reviewer          |
| Odds/stats clients, rate limits  | odds-ingestion-engineer    |
| Test design, coverage, fixtures  | test-engineer              |
| Football model (Dixon-Coles, xG) | football-modeling-engineer |
| NBA model (features, LightGBM)   | nba-modeling-engineer      |
| Training/calibration/registry    | ml-engineer                |
| Literature/market research       | quant-sports-researcher    |
| Warehouse flows, normalization   | data-engineer              |
| FastAPI/async/app wiring         | python-backend-engineer    |
| Schema, migrations, indexes      | database-architect         |
| Docker, CI, deployment           | docker-devops-engineer     |
| ADRs, research logs, docs        | documentation-writer       |
| GitHub repo evaluation           | repo-researcher            |

## Skill routing

Project skills (.claude/skills/): github-research, python-fastapi,
async-ingestion, postgres-schema, sports-modeling, odds-math, backtesting,
docker-deployment, security-review — triggers in each SKILL.md.
Global betting skills (use for derivations): kelly-bankroll, clv-evaluation,
walkforward-backtest, betting-feature-engineering, calibration-eval.

## Memory rules

- Canonical memory: `.claude/memory/` (git-versioned). Index: MEMORY.md;
  entries in decisions/data-sources/modeling-notes/pitfalls.md.
- Significant decisions get an ADR in `docs/adr/` (memory points to ADRs).
- **Memory and docs never contain secrets**: no API keys, tokens, passwords,
  cookies, account identifiers, or .env values. (ADR-0001; external memory
  tools were researched and rejected — see research log.)

## Security rules

- `.env` is gitignored, mode 0600, read only by `app/config.py`;
  `.env.example` holds names + safe defaults only.
- gitleaks gates every commit (fail-closed hook) and runs in CI.
- Never log URLs or stringified exceptions from HTTP clients (query strings
  carry API keys) — log `type(exc).__name__` + status only; sanitize
  persisted payloads (`(?i)(token|password|bearer|authorization|apiKey|appKey|secret)`).
- New dependencies: review activity/install scripts/CVEs first (the bash
  guard reminds on installs).
- GitHub research: use the plugin MCP server
  (`mcp__plugin_everything-claude-code_github__*`) — the standalone `github`
  server has bad credentials.

## Testing rules

- TDD for all odds math: failing test → implementation → green.
- Property invariants: devig sums to 1.0 (±1e-9) order-preserving; Kelly
  never negative, never above caps; each pick gate trips its named reason.
- No network in tests (httpx.MockTransport, fakeredis). No red merges.
- Walk-forward only for model evaluation; no closing odds in features.

## Data sources

- Free-first policy: see `docs/research/free-odds-sources.md`, ADR-0010, ADR-0012.
- **Master-app spine (proven repos, used directly):** OddsHarvester scrapes
  free OddsPortal odds (`app/ingestion/oddsportal.py`); penaltyblog
  Dixon-Coles prices football (`app/models/football_dc.py`); both bound in
  `app/scheduler.py`. `ODDS_SOURCE=oddsportal` (free default) or `odds_api`.
- **API-Football is SUSPENDED — never call it, never add its key.**
- The Odds API keys are optional (`ODDS_API_KEY_1..3` rotation); design for
  free-tier credit budgets.
- Live OddsPortal scraping needs Playwright Chromium
  (`uv run playwright install chromium`); it is ToS-sensitive and DOM-fragile
  — treat scrape gaps as expected, never bypass anti-bot protections.

## Deployment

- Local: docker compose (postgres+redis) + host-run app via uv.
- Production: Ubuntu VPS, full compose, `restart: unless-stopped`, stdout
  logging, `.env` on host (0600). Runbooks: `docs/deployment/`.

## Roadmap

Eight phases in `docs/roadmap.md`. Current status lives in README.md.
