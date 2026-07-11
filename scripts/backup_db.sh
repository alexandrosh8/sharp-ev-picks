#!/usr/bin/env bash
# backup_db.sh — nightly pg_dump of the compose postgres service to a
# rotated local archive.
#
# Usage:
#   bash /workspace/scripts/backup_db.sh            # dump + rotate
#   bash /workspace/scripts/backup_db.sh --verify   # pg_restore --list on newest dump
#
# Environment overrides (all optional):
#   BACKUP_DIR      target directory   (default /workspace/backups)
#   RETENTION_DAYS  days to keep dumps (default 14)
#   COMPOSE_FILE    compose file path  (default /workspace/docker-compose.yml)
#
# Design notes:
# - Credentials are NEVER passed here: pg_dump runs inside the postgres
#   container and reads POSTGRES_USER/POSTGRES_DB from the container env.
# - Dumps are custom-format (-Fc), streamed to
#   betting_YYYY-MM-DDTHHMMSSZ.dump (UTC), written via a .part temp file
#   and renamed only on success — a partial dump never looks complete.
# - Rotation never uses bare rm: it prefers `trash` when installed and
#   otherwise uses a guarded `find -delete` that refuses to run outside
#   an absolute, existing backup directory and only matches our own
#   betting_*.dump naming at -maxdepth 1.
# - Runbook: /workspace/docs/deployment/db-backup.md

set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/workspace/backups}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
COMPOSE_FILE="${COMPOSE_FILE:-/workspace/docker-compose.yml}"
DUMP_PREFIX="betting_"

log() { printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }

usage() {
  grep '^#' "${BASH_SOURCE[0]}" | head -n 14 | sed 's/^# \{0,1\}//'
  exit 2
}

# ---------- guards -----------------------------------------------------------

check_backup_dir_safe() {
  # Every destructive/creating operation goes through these guards.
  case "${BACKUP_DIR}" in
    "" | "/") die "refusing to operate: BACKUP_DIR is empty or filesystem root" ;;
    /*) : ;;
    *) die "refusing to operate: BACKUP_DIR is not an absolute path: ${BACKUP_DIR}" ;;
  esac
}

check_retention_sane() {
  case "${RETENTION_DAYS}" in
    '' | *[!0-9]*) die "RETENTION_DAYS must be a non-negative integer, got: ${RETENTION_DAYS}" ;;
  esac
}

compose() {
  docker compose -f "${COMPOSE_FILE}" "$@"
}

# ---------- backup -----------------------------------------------------------

run_backup() {
  check_backup_dir_safe
  check_retention_sane # fail fast BEFORE dumping, not after
  [ -f "${COMPOSE_FILE}" ] || die "compose file not found: ${COMPOSE_FILE}"

  mkdir -p "${BACKUP_DIR}"

  local stamp dump_file tmp_file size
  stamp="$(date -u '+%Y-%m-%dT%H%M%SZ')"
  dump_file="${BACKUP_DIR}/${DUMP_PREFIX}${stamp}.dump"
  tmp_file="${dump_file}.part"

  log "dumping postgres (custom format) -> ${dump_file}"
  # -T: no TTY (cron-safe). pg_dump is read-only for the database.
  # User/db come from the CONTAINER env — no credentials on this host line.
  # shellcheck disable=SC2016  # single quotes are deliberate: expand in-container
  if ! compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "${tmp_file}"; then
    die "pg_dump failed; partial file left at ${tmp_file} (rotation will prune it)"
  fi
  [ -s "${tmp_file}" ] || die "pg_dump produced an empty file: ${tmp_file}"

  mv "${tmp_file}" "${dump_file}"
  size="$(du -h "${dump_file}" | cut -f1)"
  log "backup complete: ${dump_file} (${size})"
}

# ---------- rotation ---------------------------------------------------------

prune_old_backups() {
  check_backup_dir_safe
  check_retention_sane
  [ -d "${BACKUP_DIR}" ] || die "refusing to prune: not a directory: ${BACKUP_DIR}"

  log "pruning ${DUMP_PREFIX}*.dump / *.dump.part older than ${RETENTION_DAYS} days in ${BACKUP_DIR}"
  if command -v trash > /dev/null 2>&1; then
    # Preferred: recoverable delete via trash.
    find "${BACKUP_DIR}" -maxdepth 1 -type f \
      \( -name "${DUMP_PREFIX}*.dump" -o -name "${DUMP_PREFIX}*.dump.part" \) \
      -mtime +"${RETENTION_DAYS}" -print0 \
      | xargs -0 -r -n 1 trash --
  else
    # Guarded find-delete: absolute existing dir (checked above), files only,
    # top level only, and ONLY our own naming pattern. Never bare rm.
    find "${BACKUP_DIR}" -maxdepth 1 -type f \
      \( -name "${DUMP_PREFIX}*.dump" -o -name "${DUMP_PREFIX}*.dump.part" \) \
      -mtime +"${RETENTION_DAYS}" -print -delete
  fi
  log "rotation done"
}

# ---------- verify -----------------------------------------------------------

run_verify() {
  check_backup_dir_safe
  [ -d "${BACKUP_DIR}" ] || die "no backup directory: ${BACKUP_DIR}"

  local newest entries
  # Filenames embed a UTC timestamp, so lexical sort == chronological sort.
  newest="$(find "${BACKUP_DIR}" -maxdepth 1 -type f -name "${DUMP_PREFIX}*.dump" | sort | tail -n 1)"
  [ -n "${newest}" ] || die "no ${DUMP_PREFIX}*.dump files in ${BACKUP_DIR}"

  log "verifying newest dump: ${newest}"
  # pg_restore --list reads the archive TOC without touching any database.
  # Runs inside the container (pg_restore is not installed on the host).
  entries="$(compose exec -T postgres pg_restore --list < "${newest}" | grep -c '^[0-9]')" \
    || die "pg_restore --list FAILED for ${newest} — dump is unreadable"
  [ "${entries}" -gt 0 ] || die "pg_restore --list returned zero TOC entries for ${newest}"
  log "verify OK: ${newest} (${entries} TOC entries)"
}

# ---------- main -------------------------------------------------------------

main() {
  case "${1:-}" in
    --verify)
      run_verify
      ;;
    "")
      run_backup
      prune_old_backups
      ;;
    -h | --help)
      usage
      ;;
    *)
      log "unknown argument: $1" >&2
      usage
      ;;
  esac
}

main "$@"
