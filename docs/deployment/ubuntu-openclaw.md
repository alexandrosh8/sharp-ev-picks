# Ubuntu / OpenClaw Production Deployment (Stage 2)

Target: Ubuntu LTS VPS already running OpenClaw. Everything ships via Docker
Compose; nothing macOS-specific exists in this repo.

## One-time setup

```bash
sudo apt update
sudo apt install -y docker.io docker-compose-plugin git
sudo usermod -aG docker $USER
```

```bash
git clone <repo> /opt/betting-ai
cd /opt/betting-ai
cp .env.example .env
chmod 600 .env
# fill in: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ODDS_API_KEY_1..3 (read-only data keys)
```

## OpenClaw coexistence

- All ports bind to 127.0.0.1 only (compose already does this): app 8000,
  postgres 5433, redis 6380. If OpenClaw claims any of these, change the HOST
  side of the mapping in `docker-compose.yml` — container ports stay standard.
- Resource isolation: add `mem_limit`/`cpus` to the app service if the VPS is
  shared.

## Deploy / upgrade

```bash
cd /opt/betting-ai
git pull
docker compose --profile prod build
docker compose --profile prod up -d
docker compose exec app uv run alembic upgrade head
```

`restart: unless-stopped` on every service is the resurrection policy
(Docker daemon is itself systemd-managed). No launchd anywhere.

## Logs & monitoring

```bash
docker compose logs -f app
docker compose ps
curl -s http://127.0.0.1:8000/health
```

- All services log UTC to stdout (12-factor); ship with `docker compose logs`
  or a log driver — no log files in containers.
- Watchdog suggestion: a cron `curl /health` + Telegram on failure (read-only,
  no betting surface).

## Backups

```bash
docker compose exec postgres pg_dump -U betting_ai betting_ai > /opt/backups/betting_ai_$(date -u +%Y%m%dT%H%M%SZ).sql
```

Schedule daily via cron (UTC); keep 14 days. The odds snapshot archive is the
irreplaceable asset (NBA closing lines cannot be re-fetched — ADR-0010).

## Safety in production

- `.env` is 0600, owned by the deploy user; never in the image (verified by
  .dockerignore + gitleaks in CI).
- `scripts/safety_audit.sh` runs in CI on every push: the image cannot ship
  bet-placement code paths.
- Betfair credentials, if ever added, are read-only market-data keys only.
