#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${ROOT_DIR}/backups"
DB_CONTAINER="${DB_CONTAINER:-lightning-db}"
DB_NAME="${DB_NAME:-lightning}"
DB_USER="${DB_USER:-lightning}"
DB_PASSWORD="${DB_PASSWORD:-lightningpass}"
LABEL="${1:-manual}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
OUT_FILE="${BACKUP_DIR}/lightning_backup_${TIMESTAMP}_${LABEL}.sql"

mkdir -p "${BACKUP_DIR}"

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker is required." >&2
  exit 1
fi

if ! docker ps --format '{{.Names}}' | grep -Fxq "${DB_CONTAINER}"; then
  echo "ERROR: container '${DB_CONTAINER}' is not running." >&2
  exit 1
fi

echo "Creating SQL backup at ${OUT_FILE} ..."
docker exec -e PGPASSWORD="${DB_PASSWORD}" "${DB_CONTAINER}" \
  pg_dump -U "${DB_USER}" -d "${DB_NAME}" --clean --if-exists --no-owner --no-privileges > "${OUT_FILE}"

echo "Backup complete: ${OUT_FILE}"
