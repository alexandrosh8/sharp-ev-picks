# Database backups — `scripts/backup_db.sh`

Nightly `pg_dump` of the compose `postgres` service to a rotated local
archive. The odds-snapshot archive is the irreplaceable asset (NBA closing
lines cannot be re-fetched — ADR-0010), so an untested backup is treated as
no backup.

**No credentials appear anywhere in this flow**: `pg_dump` runs *inside* the
postgres container and reads `POSTGRES_USER` / `POSTGRES_DB` from the
container environment. The host never sees a password.

## What the script does

- Dumps in **custom format** (`-Fc`, compressed, selective-restore capable)
  to `BACKUP_DIR/betting_YYYY-MM-DDTHHMMSSZ.dump` (UTC timestamp).
- Writes via a `.part` temp file and renames only on success — a partial
  dump never masquerades as a complete one.
- **Rotates**: prunes `betting_*.dump` (and stray `.part` files) older than
  `RETENTION_DAYS`. Uses `trash` when installed; otherwise a guarded
  `find -maxdepth 1 -delete` that refuses empty/root/relative directories
  and only matches the script's own file naming. Never bare `rm`.
- `--verify` runs `pg_restore --list` (TOC read only — touches no database)
  on the newest dump and fails loudly on zero entries.
- Exits non-zero on any failure (`set -euo pipefail`).

Defaults (override via environment):

| Variable         | Default                        |
| ---------------- | ------------------------------ |
| `BACKUP_DIR`     | `/workspace/backups`           |
| `RETENTION_DAYS` | `14`                           |
| `COMPOSE_FILE`   | `/workspace/docker-compose.yml`|

`backups/` is gitignored — dumps must never enter the repo.

## Manual run

```bash
bash /workspace/scripts/backup_db.sh
```

```bash
bash /workspace/scripts/backup_db.sh --verify
```

A dump is read-only for the database; it is safe while the app is live.

## Nightly cron (host crontab)

`crontab -e` on the host that runs the compose stack:

```cron
17 3 * * * /usr/bin/env bash /workspace/scripts/backup_db.sh >> /workspace/backups/backup.log 2>&1
```

Notes:

- 03:17 **UTC** nightly (cron follows the host clock — production hosts run
  UTC; the dump filename timestamp is always UTC regardless).
- The script uses `compose exec -T` internally (cron has no TTY).
- `backup.log` lives inside the backup dir but does not match the
  `betting_*.dump` rotation pattern, so it is never pruned.
- If the repo lives elsewhere on your host (e.g. `/opt/betting-ai`), set the
  overrides on the cron line:

```cron
17 3 * * * BACKUP_DIR=/opt/backups COMPOSE_FILE=/opt/betting-ai/docker-compose.yml /usr/bin/env bash /opt/betting-ai/scripts/backup_db.sh >> /opt/backups/backup.log 2>&1
```

Optionally add a weekly verification pass:

```cron
47 3 * * 0 /usr/bin/env bash /workspace/scripts/backup_db.sh --verify >> /workspace/backups/backup.log 2>&1
```

## Restore procedure

**Always restore into a scratch database first.** Never point `pg_restore`
at the live `betting_ai` database as the first step.

1. Create a scratch database (inside the running container):

   ```bash
   docker compose -f /workspace/docker-compose.yml exec -T postgres sh -c 'createdb -U "$POSTGRES_USER" betting_ai_restore_check'
   ```

2. Restore the dump into it:

   ```bash
   docker compose -f /workspace/docker-compose.yml exec -T postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d betting_ai_restore_check' < /workspace/backups/betting_<stamp>.dump
   ```

3. Sanity-check the restored data (row counts on the critical tables):

   ```bash
   docker compose -f /workspace/docker-compose.yml exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d betting_ai_restore_check -c "SELECT count(*) FROM odds_snapshots;"'
   ```

4. **Swap guidance** (only after step 3 looks right): stop the app
   container first so nothing writes mid-swap, then either

   - point the app at the restored DB (change the database name in
     `DATABASE_URL` via `.env` / compose), **or**
   - rename databases inside postgres (`ALTER DATABASE ... RENAME TO ...`)
     so the restored one takes the `betting_ai` name — keeping the damaged
     original under a `_broken` suffix until you are sure.

   Restart the app afterwards; the entrypoint runs
   `alembic upgrade head`, which brings an older dump forward to the
   current schema.

5. Drop the scratch DB when done:

   ```bash
   docker compose -f /workspace/docker-compose.yml exec -T postgres sh -c 'dropdb -U "$POSTGRES_USER" betting_ai_restore_check'
   ```

## Retention

14 days of nightly dumps by default (`RETENTION_DAYS`). Rotation runs at the
end of every successful backup; a failed dump never triggers pruning of
older good dumps beyond the age policy.

## ⚠️ Same-host warning

**These backups live on the same host (and same disk) as the database.**
They protect against bad migrations, application bugs, and accidental
deletes — **not** against disk failure or loss of the VPS. An off-host copy
(e.g. nightly `rsync`/`rclone` of `BACKUP_DIR` to another machine or object
storage) is the required next step and is deliberately **not** implemented
here — pick a destination and add it as a separate cron line.
