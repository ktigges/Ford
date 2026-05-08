#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <path-to-sql-backup>" >&2
  exit 1
fi

BACKUP_FILE="$1"
DB_CONTAINER="${DB_CONTAINER:-lightning-db}"
DB_NAME="${DB_NAME:-lightning}"
DB_USER="${DB_USER:-lightning}"
DB_PASSWORD="${DB_PASSWORD:-lightningpass}"

if [[ ! -f "${BACKUP_FILE}" ]]; then
  echo "ERROR: backup file not found: ${BACKUP_FILE}" >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker is required." >&2
  exit 1
fi

if ! docker ps --format '{{.Names}}' | grep -Fxq "${DB_CONTAINER}"; then
  echo "ERROR: container '${DB_CONTAINER}' is not running." >&2
  exit 1
fi

echo "Restoring SQL backup ${BACKUP_FILE} into ${DB_NAME} ..."
docker exec -i -e PGPASSWORD="${DB_PASSWORD}" "${DB_CONTAINER}" \
  psql -U "${DB_USER}" -d "${DB_NAME}" -v ON_ERROR_STOP=1 < "${BACKUP_FILE}"

echo "Restore complete."
