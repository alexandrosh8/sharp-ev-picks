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
sudo git clone <repo> /opt/betting-ai
sudo chown -R $USER /opt/betting-ai
cd /opt/betting-ai
cp .env.example .env
chmod 600 .env
```

Edit `.env` (it stays on the host, mode 0600, never enters the image —
`.dockerignore` excludes it; compose injects it at runtime):

| Key                                      | Required?               | Notes                                                                                                                                       |
| ---------------------------------------- | ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | **REQUIRED**            | pick alerts                                                                                                                                 |
| `COMPOSE_PROFILES=prod`                  | **REQUIRED on the VPS** | uncomment it — see below                                                                                                                    |
| `POSTGRES_PASSWORD`                      | strongly recommended    | set BEFORE first boot: pgdata is initialized with the first password; changing later needs `ALTER USER` inside postgres                     |
| `DASHBOARD_AUTH_ENABLED=true`            | required for public IP  | required before `APP_HOST_BIND=0.0.0.0` or a public reverse proxy                                                                           |
| `DASHBOARD_AUTH_PASSWORD_HASH`           | required for public IP  | generate with `uv run python -c "from app.api.auth import hash_password; print(hash_password('YOUR_PASSWORD'))"`; store it in single quotes |
| `DASHBOARD_SESSION_SECRET`               | required for public IP  | generate with `uv run python -c "import secrets; print(secrets.token_urlsafe(48))"`                                                         |
| `APP_HOST_BIND`                          | optional                | default `127.0.0.1`; set `0.0.0.0` only after dashboard auth is enabled                                                                     |
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
cd /opt/betting-ai
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

External-IP access: enable dashboard auth first, then expose only the app port.
In `.env`:

```dotenv
DASHBOARD_AUTH_ENABLED=true
DASHBOARD_AUTH_PASSWORD_HASH='<generated_pbkdf2_hash>'
DASHBOARD_SESSION_SECRET=<generated_random_secret>
APP_HOST_BIND=0.0.0.0
```

The single quotes around `DASHBOARD_AUTH_PASSWORD_HASH` matter: Docker Compose
interpolates unquoted `$` characters, and PBKDF2 hashes use `$` separators.

Restart:

```bash
docker compose up -d --build
```

Then open `http://<vps-ip>:8000/`. `app/config.py` refuses to start with
`APP_HOST_BIND=0.0.0.0` unless dashboard auth is enabled. Keep Postgres and
Redis loopback-only; never expose ports `5433` or `6380`. If the VPS firewall
supports it, allow `8000/tcp` only from your trusted IPs.

Reverse proxy option: keep `APP_HOST_BIND=127.0.0.1`, put the proxy on the
public interface, and still enable dashboard auth before exposing the route.
Use TLS at the proxy for any non-local access. `/health` stays public and
read-only for compose healthchecks and watchdogs.

What "healthy" means: the compose healthcheck hits `GET /health` (interval
30s, start period 60s). `/health` also exposes per-job poll liveness
(`polls`), so an external watchdog can distinguish "process up" from "engine
dead". Watchdog suggestion: cron `curl /health` + Telegram on failure.

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
cd /opt/betting-ai
git pull
docker compose up -d --build
```

Migrations run automatically on boot (idempotent — no-op at head). Dependency
bumps go through `scripts/upgrade_deps.sh` (the gated path) on a dev machine,
get committed, then ship through the same `git pull` + rebuild.

After bumping `oddsharvester` past 0.3.0: re-verify the Dockerfile's sandbox
note — 0.3.0 detects Docker via `/.dockerenv` and launches Chromium with
`--no-sandbox --disable-dev-shm-usage`; the container relies on that.

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
       <vps>:/opt/betting-ai/data/ml/
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
cannot be re-fetched — ADR-0010). One-time:

```bash
sudo mkdir -p /opt/backups
sudo chown $USER /opt/backups
```

Cron (note `-T`: cron has no TTY and `compose exec` fails without it; `%` is
escaped because cron expands it; `-Fc` = compressed custom format):

```cron
15 03 * * * docker compose -f /opt/betting-ai/docker-compose.yml exec -T postgres pg_dump -U betting_ai -Fc betting_ai > /opt/backups/betting_ai_$(date -u +\%Y\%m\%dT\%H\%M\%SZ).dump
45 03 * * * find /opt/backups -name 'betting_ai_*.dump' -mtime +14 -delete
```

Restore (test this once — an untested backup is not a backup):

```bash
docker compose exec -T postgres pg_restore -U betting_ai -d betting_ai --clean < /opt/backups/betting_ai_<stamp>.dump
```

Push dumps off-box (rsync/rclone to anywhere) — the VPS disk is a single
point of failure.

## 6. Troubleshooting

| Symptom                                            | Meaning / fix                                                                                                                                                                 |
| -------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `docker compose ps` shows no `app` service         | `COMPOSE_PROFILES=prod` missing from the VPS `.env` (see §1)                                                                                                                  |
| App container restarts repeatedly at boot          | entrypoint retries `alembic upgrade head` 10×; check `compose logs app` — usually postgres still initializing on first boot, self-heals                                       |
| Container OOM-killed / Chromium crashes mid-scrape | the app is capped at `mem_limit: 2g` (3 concurrent Chromium pages + Python). Raise to 3g in docker-compose.yml if the VPS has headroom; lower `ODDSPORTAL_CONCURRENCY` if not |
| Scrape gaps / partial cycles                       | **expected** — OddsPortal scraping is ToS-sensitive and DOM-fragile; gaps are tolerated by design, never bypass anti-bot protections                                          |
| Dashboard shows ENGINE OFFLINE right after deploy  | `/health` `polls` is empty until the FIRST full cycle completes — a cycle takes 20-40 min. Don't page on an empty polls dict in the first hour after boot                     |
| Dashboard shows ENGINE OFFLINE in steady state     | the scheduler stopped polling (check `compose logs app` for per-cycle errors) or the container is down (`compose ps`)                                                         |
| Duplicate Telegram alerts after a redis crash      | redis runs AOF (`--appendonly yes`) to minimize this; residual duplicates are an annoyance, not a safety issue — nothing places bets                                          |
| "Executable doesn't exist" for Chromium            | the image bakes Chromium at `/ms-playwright` at build time; this error means a stale image — `docker compose up -d --build`                                                   |

What a restart costs (safe by design): the daily exposure ledger re-seeds
from today's persisted picks; one duplicate odds-snapshot row per live key;
poll liveness empty until the first cycle completes; alert dedupe survives in
redis. If ledger seeding fails it starts empty for the day (logged,
over-recommends at worst).

## 7. OpenClaw coexistence

- Host port bindings: app 8000 defaults to `127.0.0.1` and can be changed with
  `APP_HOST_BIND` after dashboard auth is enabled. Postgres 5433 and Redis
  6380 stay `127.0.0.1` only (5432/6379 left free for other stacks). If
  OpenClaw claims any host port, change the HOST side of the mapping in
  `docker-compose.yml`; container ports stay standard.
- Resources are capped in compose (app: 2 GB RAM, 2 CPUs, `init: true` to
  reap zombie Chromium helpers) — committed defaults, tune in place.

## 8. Safety in production

- `.env` is 0600, never in the image (`.dockerignore`) and never committed
  (gitleaks gates every commit and runs in CI).
- `scripts/safety_audit.sh` runs in CI on every push: the image cannot ship
  bet-placement code paths.
- Betfair credentials, if ever added, are read-only market-data keys only.
