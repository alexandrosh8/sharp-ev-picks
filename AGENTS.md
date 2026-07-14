# AGENTS.md — Manual-Betting +EV Picks Platform (betting-ai)

A picks-only decision-support system for Football/Soccer, NBA, NFL, and tennis.
It ingests read-only market/sports data, estimates probabilities, strips vig,
detects +EV edges, recommends informational fractional-Kelly stakes, alerts,
settles results, and tracks ROI/CLV. The user reviews picks and places any bet
personally outside this system.

## Start here on every device

1. Read `docs/CODEX_DEVICE_HANDOFF.md`.
2. Run `bash scripts/bootstrap_codex.sh --check`.
3. Run `bash scripts/bootstrap_codex.sh` after prerequisites are present.
4. Review and trust the repository hooks with `/hooks` in Codex.
5. Before committing, run `bash scripts/verify_codex_workspace.sh`.

The Codex workspace scripts and hooks support macOS, Linux, and WSL; use
WSL rather than native Windows. All required project skills and specialist
agents are versioned in this repository. Never copy a previous device's `~/.codex/config.toml`, auth state,
session database, credentials, or `.env` into Git.

## Hard safety rules — never override

1. **This system never places bets.**
2. Any code path that could place a bet is a build-breaking defect.
   `scripts/safety_audit.sh` must exit 0.
3. Market-data integrations are read-only operations. HTTP sources use GET by
   default. The sole protocol exception is the Betfair read-only JSON-RPC client,
   which uses POST only for the explicit operation allowlist documented in
   `betfair-api-validator`; order/account operations remain forbidden.
4. Safety settings in `app/config.py` are locked and startup must fail if they
   are changed from these values:

   | Setting                 | Locked value |
   | ----------------------- | ------------ |
   | `PICKS_ONLY`            | `true`       |
   | `MANUAL_BETTING_ONLY`   | `true`       |
   | `AUTO_BETTING`          | `false`      |
   | `BET_EXECUTION_ENABLED` | `false`      |
   | `READ_ONLY_MARKET_DATA` | `true`       |
   | `PAPER_TRADING`         | `false`      |

5. Stakes, edge, EV, ROI, and CLV are informational. Never imply guaranteed
   profit.
6. If a requested change appears to enable automated betting, stop and clarify.

## Current project context

- Python 3.12, uv, httpx async, pydantic v2, numpy/scipy.
- SQLAlchemy 2 async + asyncpg + Alembic, PostgreSQL, Redis.
- FastAPI, APScheduler, Docker Compose, Playwright.
- Local development: app on host; PostgreSQL/Redis in Compose.
- Production: deployed Ubuntu VPS Compose stack behind the public site. Never
  introduce macOS-only runtime behavior.
- Architecture: `docs/architecture.md`; schema: `docs/db-schema.md`; decisions:
  `docs/adr/`; current continuation state: `docs/CODEX_DEVICE_HANDOFF.md`.

## Canonical development commands

```bash
uv sync --frozen --all-extras --all-groups
docker compose up -d --wait postgres redis
.venv/bin/alembic upgrade head
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check app tests scripts alembic tools
.venv/bin/python -m ruff format --check app tests scripts alembic tools
.venv/bin/python -m mypy app tests
bash scripts/safety_audit.sh
gitleaks git --no-banner --redact
```

Use `.venv/bin/python` for the project. The full reproducible gate is
`bash scripts/verify_codex_workspace.sh`.

## Python and architecture rules

- Type hints on every function; mypy must pass. Use pathlib, f-strings, and
  explicit modular boundaries.
- pydantic v2 models: frozen; `extra="forbid"` internally and `extra="ignore"`
  for upstream payloads. All datetimes are UTC-aware.
- `app/probabilities/`, `app/edge/`, `app/risk/`, and
  `app/backtesting/clv.py` are pure math: no environment, DB, HTTP, logging, or
  import-time side effects. Policies enter as frozen dataclasses.
- Environment reads belong only in `app/config.py`.
- Async-first; never block the event loop. Odds/money use Decimal or NUMERIC at
  boundaries; float is restricted to numerical kernels.
- Dashboard authoring lives in `app/api/dashboard_src/`; the generated,
  committed runtime artifact is `app/api/dashboard.html`.

## Shell and git rules

- Never use `&&`; run commands separately.
- Never use bare `rm`; use `git rm` for tracked files or a platform trash
  command (`trash`, `trash-put`, or `gio trash`) for untracked files.
- Scripts derive their absolute repository root from their own location and use
  absolute paths internally. Quote all paths.
- Feature branch before changes. Commit `checkpoint` before a large refactor.
- Never commit untested code. Keep commits small and focused; squash merge.
- Retry the same failing self-heal action at most three times; after the third
  failure, stop and report the blocker.
- Never delete, overwrite, or commit `.env`. Deployment must preserve the
  server-side `.env` in place.

## Specialist agent routing (`.codex/agents/`)

| Work                                    | Agent                          |
| --------------------------------------- | ------------------------------ |
| Devig, edge, EV, CLV math               | `vig-edge-math-engineer`       |
| Sharp/soft anchor and fill semantics    | `sharp-soft-market-engineer`   |
| Kelly sizing and exposure caps          | `risk-kelly-engineer`          |
| Odds/stat clients and rate limits       | `odds-ingestion-engineer`      |
| HTML/embedded-JSON fetch and parsing    | `html-json-ingestion-engineer` |
| Dashboard HTML/CSS/JS/build performance | `dashboard-frontend-engineer`  |
| FastAPI/async/composition wiring        | `python-backend-engineer`      |
| PostgreSQL/Alembic/query design         | `database-architect`           |
| Data flows and normalization            | `data-engineer`                |
| Football modeling                       | `football-modeling-engineer`   |
| NBA modeling                            | `nba-modeling-engineer`        |
| Training/calibration/registry           | `ml-engineer`                  |
| Tests and regression coverage           | `test-engineer`                |
| Secrets/logging/safety                  | `security-reviewer`            |
| Docker/CI/deployment                    | `docker-devops-engineer`       |
| Literature/market research              | `quant-sports-researcher`      |
| GitHub repository evaluation            | `repo-researcher`              |
| ADRs/runbooks/docs                      | `documentation-writer`         |

Delegate independent read-heavy audits/tests in parallel. Keep write ownership
separate to avoid conflicts.

## Repository skill routing (`.agents/skills/`)

The clone is self-contained; required work must not depend on globally installed
skills.

- Backend/data: `python-fastapi`, `async-ingestion`, `postgres-schema`,
  `docker-deployment`, `security-review`.
- Quant/modeling: `odds-math`, `sports-modeling`, `backtesting`, `penaltyblog`,
  `shadow-strategy-engineer`, `pick-quality-researcher`.
- Sharp/soft evidence: `sharp-soft-market-analysis`, `sharp-anchor-auditor`,
  `clv-evidence-reviewer`, `canonical-matcher-verifier`,
  `betfair-api-validator`.
- Web/scraping: `html-json-ingestion`, `vanilla-dashboard-architecture`,
  `webapp-testing`.
- Research: `github-research`.

## Memory and documentation

- Tracked project memory remains under `.claude/memory/` for cross-harness
  compatibility. `MEMORY.md` is the index.
- Living Codex handoff: `docs/CODEX_DEVICE_HANDOFF.md`.
- Significant decisions require an ADR. Memory points to ADRs rather than
  duplicating them. Portable Codex workspace decisions are in ADR-0026.
- Memory/docs never contain secrets, account identifiers, cookies, proxy URLs,
  tokens, passwords, private hosts, or `.env` values.
- `docs/HANDOFF-2026-07-03.md` is historical only and must not drive current
  branch or deployment decisions.

## Security and test rules

- `.env` is gitignored and mode 0600; `.env.example` contains only names and
  safe local defaults.
- The Codex-issued commit hook fails closed if `jq` or `gitleaks` is
  unavailable. Terminal/IDE commits bypass Codex hooks, so the manual verify
  script and CI remain mandatory.
- Never log URLs or stringified HTTP exceptions when query strings can contain
  keys. Log exception type/status and sanitize payloads.
- TDD for odds math. Required invariants: devig sums to 1 within 1e-9 and
  preserves order; Kelly is nonnegative and capped; every gate reports its
  named reason.
- Tests use synthetic fixtures, `httpx.MockTransport`, and fakeredis. No live
  network calls.
- Model evaluation is temporal/walk-forward only. Closing odds never enter
  features. Spent evaluation domains stay spent; see the tracked consumption
  ledger under `docs/backtesting/consumption/`.

## Data sources and deployment

- Free-first source policy: ADR-0010/0012 and
  `docs/research/free-odds-sources.md`.
- Default live odds spine: OddsPortal/OddsHarvester; optional The Odds API.
- Football pricing: penaltyblog Dixon-Coles.
- **API-Football is suspended:** never call it or add a key.
- Production deploys use the runbooks under `docs/deployment/`, preserve the
  existing server `.env`, run migrations through the entrypoint, and verify
  `/live`, `/ready`, `/health`, logs, and migration head after recreation.
