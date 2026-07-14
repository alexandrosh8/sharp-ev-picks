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

- **Optional off-host copy**: when `OFFHOST_BACKUP_TARGET` is set, the
  just-created dump is shipped off-host at the end of the run —
  `user@host:/path` targets via `rsync -e ssh`, `remote:path` targets via
  `rclone copyto`. Unset = silent no-op; set + anything failing (missing
  binary, unreachable host, bad remote) = **loud non-zero exit**. No
  credentials ever appear in the script or its argv — ssh auth is
  key/agent-based and rclone reads its own config.

Defaults (override via environment):

| Variable                | Default                         |
| ----------------------- | ------------------------------- |
| `BACKUP_DIR`            | `<repository>/backups`            |
| `RETENTION_DAYS`        | `14`                            |
| `COMPOSE_FILE`          | `<repository>/docker-compose.yml` |
| `OFFHOST_BACKUP_TARGET` | unset (off-host copy disabled)  |

`backups/` is gitignored — dumps must never enter the repo.

## Manual run

```bash
bash /opt/sharp-ev-picks/scripts/backup_db.sh
```

```bash
bash /opt/sharp-ev-picks/scripts/backup_db.sh --verify
```

A dump is read-only for the database; it is safe while the app is live.

## Nightly cron (host crontab)

`crontab -e` on the host that runs the compose stack:

```cron
17 3 * * * /usr/bin/env bash /opt/sharp-ev-picks/scripts/backup_db.sh >> /opt/sharp-ev-picks/backups/backup.log 2>&1
```

Notes:

- 03:17 **UTC** nightly (cron follows the host clock — production hosts run
  UTC; the dump filename timestamp is always UTC regardless).
- The script uses `compose exec -T` internally (cron has no TTY).
- `backup.log` lives inside the backup dir but does not match the
  `betting_*.dump` rotation pattern, so it is never pruned.
- The defaults are resolved from the script location. To keep backups outside
  the repository, set the overrides on the cron line:

```cron
17 3 * * * BACKUP_DIR=/opt/backups COMPOSE_FILE=/opt/sharp-ev-picks/docker-compose.yml /usr/bin/env bash /opt/sharp-ev-picks/scripts/backup_db.sh >> /opt/backups/backup.log 2>&1
```

Optionally add a weekly verification pass:

```cron
47 3 * * 0 /usr/bin/env bash /opt/sharp-ev-picks/scripts/backup_db.sh --verify >> /opt/sharp-ev-picks/backups/backup.log 2>&1
```

## Off-host copy (`OFFHOST_BACKUP_TARGET`)

Two accepted target forms:

- `user@host:/path` — shipped with `rsync -e ssh` into that directory.
- `remote:path` — shipped with `rclone copyto` (any rclone remote: S3, B2,
  Drive, SFTP, …), keeping the dump's own filename.

Enable it on the cron line (cron does **not** read your shell profile, so the
variable must be set inline or in a wrapper):

```cron
17 3 * * * OFFHOST_BACKUP_TARGET=backup@offhost.example:/srv/betting-backups /usr/bin/env bash /opt/sharp-ev-picks/scripts/backup_db.sh >> /opt/sharp-ev-picks/backups/backup.log 2>&1
```

Crontab implications:

- **Non-interactive auth only.** For rsync: an ssh key without passphrase (or
  an agent available to cron) for a dedicated low-privilege user on the
  receiving host; first connect once interactively so the host key is in
  `known_hosts` — cron cannot answer the prompt. For rclone: the remote must
  already be configured for the crontab user (`rclone config`), since the
  config file is per-user.
- **PATH**: cron's PATH is minimal (`/usr/bin:/bin`). If `rsync`/`rclone`
  live elsewhere (e.g. `/usr/local/bin`), set `PATH=` on the cron line.
- **Failure is loud by design**: a set target with a failed copy exits
  non-zero and logs `off-host copy FAILED …` to `backup.log`. Retry without
  re-dumping via the manual entry:

```bash
OFFHOST_BACKUP_TARGET=backup@offhost.example:/srv/betting-backups bash /opt/sharp-ev-picks/scripts/backup_db.sh --offhost-copy
```

- The local dump always lands and rotates BEFORE the copy step — a broken
  off-host leg never costs you the local backup.
- Rotation is local-only: prune the off-host directory on the receiving side
  (its own cron / lifecycle rule); this script never deletes remote files.
- No credentials belong in the crontab line, this script, or the repo — the
  target string is host/path only.

### Restore from the off-host copy

Fetch the dump back to the host, then follow the normal restore procedure
below from step 1:

```bash
# rsync form
rsync -e ssh backup@offhost.example:/srv/betting-backups/betting_<stamp>.dump /opt/sharp-ev-picks/backups/

# rclone form
rclone copyto offsite:betting-backups/betting_<stamp>.dump /opt/sharp-ev-picks/backups/betting_<stamp>.dump
```

Verify the fetched file before restoring:
`bash /opt/sharp-ev-picks/scripts/backup_db.sh --verify` (it picks the newest dump in
`BACKUP_DIR`, which is the one you just fetched).

## Restore procedure

**Always restore into a scratch database first.** Never point `pg_restore`
at the live `betting_ai` database as the first step.

1. Create a scratch database (inside the running container):

   ```bash
   docker compose -f /opt/sharp-ev-picks/docker-compose.yml exec -T postgres sh -c 'createdb -U "$POSTGRES_USER" betting_ai_restore_check'
   ```

2. Restore the dump into it:

   ```bash
   docker compose -f /opt/sharp-ev-picks/docker-compose.yml exec -T postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d betting_ai_restore_check' < /opt/sharp-ev-picks/backups/betting_<stamp>.dump
   ```

3. Sanity-check the restored data (row counts on the critical tables):

   ```bash
   docker compose -f /opt/sharp-ev-picks/docker-compose.yml exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d betting_ai_restore_check -c "SELECT count(*) FROM odds_snapshots;"'
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
   docker compose -f /opt/sharp-ev-picks/docker-compose.yml exec -T postgres sh -c 'dropdb -U "$POSTGRES_USER" betting_ai_restore_check'
   ```

## Retention

14 days of nightly dumps by default (`RETENTION_DAYS`). Rotation runs at the
end of every successful backup; a failed dump never triggers pruning of
older good dumps beyond the age policy.

## ⚠️ Same-host warning

**By default these backups live on the same host (and same disk) as the
database.** They protect against bad migrations, application bugs, and
accidental deletes — **not** against disk failure or loss of the VPS. Set
`OFFHOST_BACKUP_TARGET` (see the off-host copy section above) so every
nightly dump also lands on another machine or object storage; until that is
configured, same-host is all you have.
