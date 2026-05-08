#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCHEMA_FILE="${ROOT_DIR}/schema.sql"

DB_CONTAINER="${DB_CONTAINER:-lightning-db}"
DB_VOLUME="${DB_VOLUME:-lightning_pgdata}"
POSTGRES_IMAGE="${POSTGRES_IMAGE:-postgres:16}"
DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5432}"
DB_NAME="${DB_NAME:-lightning}"
DB_USER="${DB_USER:-lightning}"
DB_PASSWORD="${DB_PASSWORD:-lightningpass}"
DB_WAIT_SECONDS="${DB_WAIT_SECONDS:-60}"

PG_SHARED_BUFFERS="${PG_SHARED_BUFFERS:-512MB}"
PG_WORK_MEM="${PG_WORK_MEM:-16MB}"
PG_MAINTENANCE_WORK_MEM="${PG_MAINTENANCE_WORK_MEM:-256MB}"
PG_EFFECTIVE_CACHE_SIZE="${PG_EFFECTIVE_CACHE_SIZE:-1536MB}"
PG_MAX_WAL_SIZE="${PG_MAX_WAL_SIZE:-2GB}"
PG_MIN_WAL_SIZE="${PG_MIN_WAL_SIZE:-512MB}"
PG_CHECKPOINT_COMPLETION_TARGET="${PG_CHECKPOINT_COMPLETION_TARGET:-0.9}"

RESTORE_SQL_FILE=""
if [[ "${1:-}" == "--restore-sql" ]]; then
  RESTORE_SQL_FILE="${2:-}"
  if [[ -z "${RESTORE_SQL_FILE}" ]]; then
    echo "ERROR: --restore-sql requires a path to a .sql backup file." >&2
    exit 1
  fi
  if [[ ! -f "${RESTORE_SQL_FILE}" ]]; then
    echo "ERROR: restore file not found: ${RESTORE_SQL_FILE}" >&2
    exit 1
  fi
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker is required." >&2
  exit 1
fi

if [[ ! -f "${SCHEMA_FILE}" ]]; then
  echo "ERROR: schema.sql not found at ${SCHEMA_FILE}" >&2
  exit 1
fi

echo "Ensuring PostgreSQL image is available (${POSTGRES_IMAGE})..."
docker pull "${POSTGRES_IMAGE}" >/dev/null

echo "Ensuring volume exists (${DB_VOLUME})..."
if ! docker volume ls --format '{{.Name}}' | grep -Fxq "${DB_VOLUME}"; then
  docker volume create "${DB_VOLUME}" >/dev/null
  echo "Created volume ${DB_VOLUME}."
fi

if docker ps -a --format '{{.Names}}' | grep -Fxq "${DB_CONTAINER}"; then
  echo "Container ${DB_CONTAINER} already exists."
  if [[ "$(docker inspect -f '{{.State.Running}}' "${DB_CONTAINER}")" != "true" ]]; then
    docker start "${DB_CONTAINER}" >/dev/null
    echo "Started existing container ${DB_CONTAINER}."
  fi
else
  echo "Creating container ${DB_CONTAINER}..."
  docker run -d \
    --name "${DB_CONTAINER}" \
    --restart unless-stopped \
    -e POSTGRES_USER="${DB_USER}" \
    -e POSTGRES_PASSWORD="${DB_PASSWORD}" \
    -e POSTGRES_DB="${DB_NAME}" \
    -p "${DB_PORT}:5432" \
    -v "${DB_VOLUME}:/var/lib/postgresql/data" \
    "${POSTGRES_IMAGE}" >/dev/null
fi

echo "Waiting for PostgreSQL to accept connections..."
for _ in $(seq 1 "${DB_WAIT_SECONDS}"); do
  if docker exec "${DB_CONTAINER}" pg_isready -U "${DB_USER}" -d "${DB_NAME}" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if ! docker exec "${DB_CONTAINER}" pg_isready -U "${DB_USER}" -d "${DB_NAME}" >/dev/null 2>&1; then
  echo "ERROR: PostgreSQL is not ready after ${DB_WAIT_SECONDS}s." >&2
  exit 1
fi

if [[ -n "${RESTORE_SQL_FILE}" ]]; then
  echo "Resetting database ${DB_NAME} before restore..."
  docker exec -e PGPASSWORD="${DB_PASSWORD}" "${DB_CONTAINER}" psql -U "${DB_USER}" -d postgres -v ON_ERROR_STOP=1 \
    -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '${DB_NAME}' AND pid <> pg_backend_pid();" \
    -c "DROP DATABASE IF EXISTS \"${DB_NAME}\";" \
    -c "CREATE DATABASE \"${DB_NAME}\";"

  echo "Restoring SQL backup ${RESTORE_SQL_FILE}..."
  docker exec -i -e PGPASSWORD="${DB_PASSWORD}" "${DB_CONTAINER}" psql -U "${DB_USER}" -d "${DB_NAME}" -v ON_ERROR_STOP=1 < "${RESTORE_SQL_FILE}"
else
  TABLE_EXISTS="$(docker exec -e PGPASSWORD="${DB_PASSWORD}" "${DB_CONTAINER}" psql -U "${DB_USER}" -d "${DB_NAME}" -tA -c "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='garage')")"

  if [[ "${TABLE_EXISTS}" != "t" ]]; then
    echo "Applying schema from ${SCHEMA_FILE}..."
    docker exec -i -e PGPASSWORD="${DB_PASSWORD}" "${DB_CONTAINER}" psql -U "${DB_USER}" -d "${DB_NAME}" -v ON_ERROR_STOP=1 < "${SCHEMA_FILE}"
  else
    echo "Schema already exists. Skipping schema apply."
  fi
fi

echo "Applying PostgreSQL memory tuning via ALTER SYSTEM..."
docker exec -e PGPASSWORD="${DB_PASSWORD}" "${DB_CONTAINER}" psql -U "${DB_USER}" -d "${DB_NAME}" -v ON_ERROR_STOP=1 <<SQL
ALTER SYSTEM SET shared_buffers = '${PG_SHARED_BUFFERS}';
ALTER SYSTEM SET work_mem = '${PG_WORK_MEM}';
ALTER SYSTEM SET maintenance_work_mem = '${PG_MAINTENANCE_WORK_MEM}';
ALTER SYSTEM SET effective_cache_size = '${PG_EFFECTIVE_CACHE_SIZE}';
ALTER SYSTEM SET max_wal_size = '${PG_MAX_WAL_SIZE}';
ALTER SYSTEM SET min_wal_size = '${PG_MIN_WAL_SIZE}';
ALTER SYSTEM SET checkpoint_completion_target = '${PG_CHECKPOINT_COMPLETION_TARGET}';
SELECT pg_reload_conf();
SQL

echo "Restarting database container to ensure all settings are active..."
docker restart "${DB_CONTAINER}" >/dev/null
for _ in $(seq 1 "${DB_WAIT_SECONDS}"); do
  if docker exec "${DB_CONTAINER}" pg_isready -U "${DB_USER}" -d "${DB_NAME}" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

docker exec -e PGPASSWORD="${DB_PASSWORD}" "${DB_CONTAINER}" psql -U "${DB_USER}" -d "${DB_NAME}" -c "SHOW shared_buffers; SHOW work_mem; SHOW maintenance_work_mem; SHOW effective_cache_size; SHOW max_wal_size; SHOW min_wal_size;"

echo "Install complete. Container '${DB_CONTAINER}' is persistent and configured with volume '${DB_VOLUME}'."
