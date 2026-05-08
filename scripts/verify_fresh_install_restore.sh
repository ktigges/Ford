#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <path-to-sql-backup>" >&2
  exit 1
fi

BACKUP_FILE="$1"
if [[ ! -f "${BACKUP_FILE}" ]]; then
  echo "ERROR: backup file not found: ${BACKUP_FILE}" >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker is required." >&2
  exit 1
fi

POSTGRES_IMAGE="${POSTGRES_IMAGE:-postgres:16}"
VERIFY_CONTAINER="${VERIFY_CONTAINER:-lightning-verify-db}"
VERIFY_VOLUME="${VERIFY_VOLUME:-lightning_verify_pgdata}"
VERIFY_PORT="${VERIFY_PORT:-55432}"
DB_NAME="${DB_NAME:-lightning}"
DB_USER="${DB_USER:-lightning}"
DB_PASSWORD="${DB_PASSWORD:-lightningpass}"
KEEP_VERIFY_DB="${KEEP_VERIFY_DB:-false}"

cleanup() {
  if [[ "${KEEP_VERIFY_DB}" == "true" ]]; then
    echo "KEEP_VERIFY_DB=true, leaving ${VERIFY_CONTAINER} and ${VERIFY_VOLUME} in place."
    return
  fi
  docker rm -f "${VERIFY_CONTAINER}" >/dev/null 2>&1 || true
  docker volume rm "${VERIFY_VOLUME}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker rm -f "${VERIFY_CONTAINER}" >/dev/null 2>&1 || true
docker volume rm "${VERIFY_VOLUME}" >/dev/null 2>&1 || true
docker volume create "${VERIFY_VOLUME}" >/dev/null

echo "Starting fresh verification PostgreSQL container..."
docker run -d \
  --name "${VERIFY_CONTAINER}" \
  -e POSTGRES_USER="${DB_USER}" \
  -e POSTGRES_PASSWORD="${DB_PASSWORD}" \
  -e POSTGRES_DB="${DB_NAME}" \
  -p "${VERIFY_PORT}:5432" \
  -v "${VERIFY_VOLUME}:/var/lib/postgresql/data" \
  "${POSTGRES_IMAGE}" >/dev/null

echo "Waiting for verification database readiness..."
for _ in $(seq 1 60); do
  if docker exec "${VERIFY_CONTAINER}" pg_isready -U "${DB_USER}" -d "${DB_NAME}" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if ! docker exec "${VERIFY_CONTAINER}" pg_isready -U "${DB_USER}" -d "${DB_NAME}" >/dev/null 2>&1; then
  echo "ERROR: verification database did not become ready." >&2
  exit 1
fi

echo "Restoring backup into fresh database..."
docker exec -i -e PGPASSWORD="${DB_PASSWORD}" "${VERIFY_CONTAINER}" \
  psql -U "${DB_USER}" -d "${DB_NAME}" -v ON_ERROR_STOP=1 < "${BACKUP_FILE}"

echo "Running post-restore validation checks..."
docker exec -e PGPASSWORD="${DB_PASSWORD}" "${VERIFY_CONTAINER}" psql -U "${DB_USER}" -d "${DB_NAME}" -v ON_ERROR_STOP=1 <<'SQL'
SELECT current_database() AS database_name;
SELECT COUNT(*) AS table_count
FROM information_schema.tables
WHERE table_schema='public';
SELECT
  (SELECT COUNT(*) FROM garage) AS garage_rows,
  (SELECT COUNT(*) FROM telemetry) AS telemetry_rows,
  (SELECT COUNT(*) FROM charging_sessions) AS charging_sessions_rows,
  (SELECT COUNT(*) FROM charging_history) AS charging_history_rows,
  (SELECT COUNT(*) FROM drives) AS drives_rows,
  (SELECT COUNT(*) FROM drive_points) AS drive_points_rows;
SQL

echo "Fresh install + restore verification completed successfully."
