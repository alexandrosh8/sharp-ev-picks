# Ubuntu / OpenClaw Production Runbook (Stage 2)

Target: Ubuntu LTS VPS already running OpenClaw. The whole platform ships as
Docker Compose with `restart: unless-stopped` on every service — that policy
(plus the systemd-managed Docker daemon) is the survivability story and the
entire reason for containerizing: host-run instances kept dying with the
terminal. No launchd, nothing macOS-specific.

Safety reminder: this is a picks-only decision-support system. Nothing in
this stack places bets, and no deployment step requires betting credentials.

## 1. One-time setup

### Prerequisites

The VPS may already have Docker (OpenClaw base images often do) — check first:

```bash
docker --version
docker compose version
```

If missing, install from the Ubuntu archive (`docker-compose-v2` is Ubuntu's
package for the compose plugin — `docker-compose-plugin` ships only from
Docker CE's own apt repo and FAILS on stock Ubuntu):

```bash
sudo apt update
sudo apt install -y docker.io docker-compose-v2 git
sudo usermod -aG docker $USER
# log out/in for the group to apply
```

(Alternative: add Docker CE's official apt repository and install
`docker-ce docker-compose-plugin` — either works; pick one.)

### Clone and configure

```bash
sudo git clone https://github.com/alexandrosh8/sharp-ev-picks.git /opt/sharp-ev-picks
sudo chown -R $USER /opt/sharp-ev-picks
cd /opt/sharp-ev-picks
cp .env.example .env
chmod 600 .env
```

Edit `.env` (it stays on the host, mode 0600, never enters the image —
`.dockerignore` excludes it; compose injects it at runtime):

| Key                                      | Required?               | Notes                                                                                                                                       |
| ---------------------------------------- | ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | optional                | pick alerts; blank disables Telegram                                                                                                         |
| `COMPOSE_PROFILES=prod`                  | **REQUIRED on the VPS** | uncomment it — see below                                                                                                                    |
| `POSTGRES_PASSWORD`                      | **REQUIRED**            | unique strong value; Compose and production validation fail closed when it is unset, blank, or a known default                              |
| `DASHBOARD_AUTH_ENABLED=true`            | **REQUIRED**            | production rejects anonymous dashboard/manual-settlement access                                                                              |
| `DASHBOARD_AUTH_PASSWORD_HASH`           | **REQUIRED**            | pre-provision a PBKDF2 hash; production disables first-run `/setup`; store the hash in single quotes                                         |
| `DASHBOARD_SESSION_SECRET`               | **REQUIRED**            | generate at least 32 random bytes; never reuse the database password                                                                          |
| `APP_HOST_BIND`                          | fixed loopback          | keep `127.0.0.1`; production rejects public binds, so TLS proxy/SSH owns the public interface                                                 |
| `ODDS_API_KEY_1..3`                      | optional                | only for `ODDS_SOURCE=odds_api`; default `oddsportal` is free, no key                                                                       |
| `WEBHOOK_URL`                            | optional                | secondary alert channel                                                                                                                     |

Keys that must **NEVER** be set or changed from the `.env.example` defaults —
the safety flags. There is deliberately no flag that enables betting; these
exist only to fail fast, and `app/config.py` aborts startup if tampered:

```
PICKS_ONLY=true            MANUAL_BETTING_ONLY=true
AUTO_BETTING=false         BET_EXECUTION_ENABLED=false
READ_ONLY_MARKET_DATA=true PAPER_TRADING=false
```

Never add `API_FOOTBALL_KEY` (provider suspended) or any bookmaker login.

### The profile switch (read this twice)

The `app` service sits behind the compose profile `prod` so that on a dev Mac
a plain `docker compose up -d` starts only postgres/redis. On the VPS,
**uncomment `COMPOSE_PROFILES=prod` in `.env`** — compose reads the project
`.env` automatically, making plain `docker compose up -d` include the app.

If you skip this, `docker compose up -d` starts only postgres/redis **with
success output and no error** — the classic 3am mistake. Symptom: `docker
compose ps` shows two services, no `app`.

### First boot

```bash
cd /opt/sharp-ev-picks
docker compose up -d --build
```

The image builds natively on the VPS (linux/amd64) — no cross-build concerns.
The app entrypoint (`scripts/docker_entrypoint.sh`) runs
`alembic upgrade head` before uvicorn on every boot, so the schema is always
migrated before the scheduler polls; no manual migration step exists anymore.

**One instance only — never `docker compose up --scale app=2`.** Two replicas
would race the boot migration, double-scrape OddsPortal, and split the
in-memory daily exposure ledger across processes (ADR-0007).

## 2. Verify

```bash
docker compose ps                          # all three services "healthy"
curl -s http://127.0.0.1:8000/health       # {"status":"ok","mode":"picks-only",...}
```

Dashboard default: the app binds to `127.0.0.1` only. Access it via SSH tunnel
from your machine:

```bash
ssh -L 8000:127.0.0.1:8000 <vps>
# then open http://localhost:8000/ locally
```

For non-local access, keep `APP_HOST_BIND=127.0.0.1` and terminate TLS at
a same-host reverse proxy. Production deliberately rejects `0.0.0.0` and other
public binds; do not expose port 8000 directly. Dashboard authentication remains
mandatory behind the proxy. Keep Postgres and Redis loopback-only and never
expose ports `5433` or `6380`.

The single quotes around `DASHBOARD_AUTH_PASSWORD_HASH` matter: Docker Compose
interpolates unquoted `$` characters, and PBKDF2 hashes use `$` separators.
`/live`, `/ready`, and `/health` remain read-only; anonymous readiness/health
responses expose only status and mode. Per-component checks and diagnostics
require an authenticated session.

What "healthy" means: the compose healthcheck hits process-only `GET /live`
(interval 30s, start period 60s), so a dependency outage or the initial long
scrape cannot put the container into a false restart loop. `GET /ready` checks
DB, Redis, scheduler, exposure seeding, and expected polls; its HTTP code stays
public while the component map requires authentication. `/health` carries the
authenticated diagnostics. Watchdog suggestion: monitor `/ready` for service
readiness and `/live` separately for process failure.

## 3. Logs

```bash
docker compose logs -f app
docker compose logs --since 1h app
```

All services log UTC to stdout (12-factor; no files in containers). The
compose file caps the json-file driver at 50 MB × 5 files per service, so
logs can never fill the VPS disk. (A host-wide alternative is
`/etc/docker/daemon.json` `log-opts` — not required since the per-service cap
is committed.)

## 4. Update / upgrade

```bash
cd /opt/sharp-ev-picks
git pull
docker compose up -d --build
```

Migrations run automatically on boot (idempotent — no-op at head). Dependency
bumps go through `scripts/upgrade_deps.sh` (the gated path) on a dev machine,
get committed, then ship through the same `git pull` + rebuild.

After any `oddsharvester` or Playwright bump, rerun the hardened container
smoke test. The application strips upstream `--no-sandbox` and disabled-site-
isolation switches, forces `chromium_sandbox=True`, and fails closed if the
pinned upstream launch contract changes.

## 4b. ML value-filter artifacts (optional)

The value-filter meta-model (`docs/research/ml-value-filter.md`, verdict
ADOPT) annotates picks with a calibrated score and — only when
`VALUE_ML_FILTER=true` — demotes sub-threshold premium picks to the volume
tier. Its two artifacts are **deliberately not in git** (`/data/` is
gitignored: large/binary, and the manifest pins a dataset hash the repo
doesn't carry). Without them the app runs exactly as before; the loader
logs "value-filter artifacts not found" and scoring stays off.

To enable scoring on the VPS:

1. **Train on a dev machine** (one-shot holdout protocol — never on the VPS):

   ```bash
   uv sync --extra ml
   uv run python scripts/ml/build_value_dataset.py
   uv run python scripts/ml/train_value_filter.py --final
   ```

   The loader refuses any manifest whose `verdict` is not `ADOPT`.

2. **Copy ONLY the two runtime artifacts to the host** (not the parquet
   caches):

   ```bash
   scp "data/ml/value_filter_manifest.json" "data/ml/value_filter_model.txt" \
       <vps>:/opt/sharp-ev-picks/data/ml/
   ```

3. **Mount them into the app container** (read-only) via
   `docker-compose.override.yml` on the host — the image does not COPY
   `data/` and must not (artifacts would be baked stale into every build):

   ```yaml
   services:
     app:
       volumes:
         - ./data/ml:/srv/betting-ai/data/ml:ro
   ```

4. **Add the ML deps to the image.** The production Dockerfile installs
   only `--extra football --extra backfill`; scoring additionally needs the
   `ml` extra (lightgbm/pandas). Append `--extra ml` to BOTH `uv sync` lines
   in the Dockerfile, then `docker compose up -d --build`. Skipping this
   step is safe: the loader logs "lightgbm is not installed" and the
   pipeline runs unfiltered.

5. Verify in the logs:

   ```bash
   docker compose logs app | grep value-filter
   # value-filter meta-model loaded (manifest 2026-06-12T..., q*=0.725, 14 features)
   ```

`VALUE_ML_FILTER` stays at its default (`false`) until score-stratified LIVE
CLV confirms the holdout evidence — scores then show on the dashboard rows
("ML 0.xx") without changing any pick behavior. Flipping it to `true` in
`.env` is the deliberate, evidence-backed step that activates demotion.

## 5. Backups

The odds-snapshot archive is the irreplaceable asset (NBA closing lines
cannot be re-fetched — ADR-0010).

Backups are handled by `scripts/backup_db.sh` (nightly `pg_dump -Fc` of the
compose postgres service, UTC-timestamped files, 14-day rotation with a
guarded delete, `--verify` via `pg_restore --list`). Full runbook —
manual run, crontab line, restore-into-scratch-then-swap procedure,
retention, same-host warning: **[db-backup.md](db-backup.md)**.

Quick reference (host crontab, 03:17 UTC nightly):

```cron
17 3 * * * /usr/bin/env bash /opt/sharp-ev-picks/scripts/backup_db.sh >> /opt/sharp-ev-picks/backups/backup.log 2>&1
```

Push dumps off-box (rsync/rclone to anywhere) — the VPS disk is a single
point of failure.

## 6. Troubleshooting

| Symptom                                            | Meaning / fix                                                                                                                                                                 |
| -------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `docker compose ps` shows no `app` service         | `COMPOSE_PROFILES=prod` missing from the VPS `.env` (see §1)                                                                                                                  |
| App container restarts repeatedly at boot          | entrypoint retries `alembic upgrade head` 3×; check `compose logs app` and fix database readiness/credentials before restarting                                       |
| Container OOM-killed / Chromium crashes mid-scrape | the app is capped at `mem_limit: 3g`; lower `ODDSPORTAL_CONCURRENCY` or raise the cap only after measuring host headroom |
| Scrape gaps / partial cycles                       | **expected** — OddsPortal scraping is ToS-sensitive and DOM-fragile; gaps are tolerated by design, never bypass anti-bot protections                                          |
| Dashboard shows ENGINE OFFLINE right after deploy  | `/health` `polls` is empty until the FIRST full cycle completes — a cycle takes 20-40 min. Don't page on an empty polls dict in the first hour after boot                     |
| Dashboard shows ENGINE OFFLINE in steady state     | the scheduler stopped polling (check `compose logs app` for per-cycle errors) or the container is down (`compose ps`)                                                         |
| Duplicate Telegram alerts after a redis crash      | redis runs AOF (`--appendonly yes`) to minimize this; residual duplicates are an annoyance, not a safety issue — nothing places bets                                          |
| "Executable doesn't exist" for Chromium            | the image bakes Chromium at `/ms-playwright` at build time; this error means a stale image — `docker compose up -d --build`                                                   |

What a restart costs (safe by design): the daily exposure ledger re-seeds
from today's persisted picks; one duplicate odds-snapshot row per live key;
poll liveness empty until the first cycle completes; alert dedupe survives in
redis. If exposure-ledger seeding fails, startup aborts rather than running with an
empty limit ledger.

## 7. OpenClaw coexistence

- Host port bindings: app 8000 stays on `127.0.0.1`; production rejects a
  public `APP_HOST_BIND`. Postgres 5433 and Redis
  6380 stay `127.0.0.1` only (5432/6379 left free for other stacks). If
  OpenClaw claims any host port, change the HOST side of the mapping in
  `docker-compose.yml`; container ports stay standard.
- Resources are capped in compose (app: 3 GB RAM, 2 CPUs, `init: true` to
  reap zombie Chromium helpers) — committed defaults, tune in place.

## 8. Safety in production

- `.env` is 0600, never in the image (`.dockerignore`) and never committed
  (gitleaks gates every commit and runs in CI).
- `scripts/safety_audit.sh` runs in CI on every push: the image cannot ship
  bet-placement code paths.
- Betfair credentials, if ever added, are read-only market-data keys only.
