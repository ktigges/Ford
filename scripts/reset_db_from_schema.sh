#!/usr/bin/env bash
set -euo pipefail

# Reset the target PostgreSQL database and recreate all tables from schema.sql.
# WARNING: This destroys all existing data in the selected database.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCHEMA_FILE="${ROOT_DIR}/schema.sql"

DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5432}"
DB_NAME="${DB_NAME:-lightning}"
DB_USER="${DB_USER:-lightning}"
DB_PASSWORD="${DB_PASSWORD:-lightningpass}"
DB_CONTAINER="${DB_CONTAINER:-lightning-db}"

AUTO_YES="false"
if [[ "${1:-}" == "--yes" || "${1:-}" == "-y" ]]; then
  AUTO_YES="true"
fi

if [[ ! -f "${SCHEMA_FILE}" ]]; then
  echo "schema.sql not found at ${SCHEMA_FILE}" >&2
  exit 1
fi

if [[ "${AUTO_YES}" != "true" ]]; then
  echo "This will DROP and RECREATE database '${DB_NAME}' on ${DB_HOST}:${DB_PORT}."
  read -r -p "Type RESET to continue: " CONFIRM
  if [[ "${CONFIRM}" != "RESET" ]]; then
    echo "Cancelled."
    exit 1
  fi
fi

export PGPASSWORD="${DB_PASSWORD}"

if command -v dropdb >/dev/null 2>&1 && \
   command -v createdb >/dev/null 2>&1 && \
   command -v psql >/dev/null 2>&1; then
  echo "Using local PostgreSQL client tools."

  echo "Dropping database ${DB_NAME} (if it exists)..."
  dropdb \
    --if-exists \
    --host="${DB_HOST}" \
    --port="${DB_PORT}" \
    --username="${DB_USER}" \
    "${DB_NAME}"

  echo "Creating database ${DB_NAME}..."
  createdb \
    --host="${DB_HOST}" \
    --port="${DB_PORT}" \
    --username="${DB_USER}" \
    "${DB_NAME}"

  echo "Applying schema from ${SCHEMA_FILE}..."
  psql \
    --host="${DB_HOST}" \
    --port="${DB_PORT}" \
    --username="${DB_USER}" \
    --dbname="${DB_NAME}" \
    --file="${SCHEMA_FILE}" \
    --set=ON_ERROR_STOP=1
else
  if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: PostgreSQL client tools (dropdb/createdb/psql) are not installed, and docker is not available for fallback." >&2
    echo "Install PostgreSQL client tools (for Ubuntu: sudo apt-get install -y postgresql-client) or install docker." >&2
    exit 1
  fi

  if ! docker ps -a --format '{{.Names}}' | grep -Fxq "${DB_CONTAINER}"; then
    echo "ERROR: PostgreSQL client tools are missing and Docker fallback container '${DB_CONTAINER}' was not found." >&2
    echo "Set DB_CONTAINER to your PostgreSQL container name, or install postgresql-client." >&2
    exit 1
  fi

  if [[ "$(docker inspect -f '{{.State.Running}}' "${DB_CONTAINER}")" != "true" ]]; then
    echo "Starting Docker container ${DB_CONTAINER}..."
    docker start "${DB_CONTAINER}" >/dev/null
  fi

  echo "Using Docker fallback via container ${DB_CONTAINER}."

  echo "Dropping and recreating database ${DB_NAME} in container..."
  docker exec \
    -e PGPASSWORD="${DB_PASSWORD}" \
    "${DB_CONTAINER}" \
    psql -U "${DB_USER}" -d postgres -v ON_ERROR_STOP=1 \
      -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '${DB_NAME}' AND pid <> pg_backend_pid();" \
      -c "DROP DATABASE IF EXISTS \"${DB_NAME}\";" \
      -c "CREATE DATABASE \"${DB_NAME}\";"

  echo "Applying schema from ${SCHEMA_FILE} in container..."
  docker exec \
    -i \
    -e PGPASSWORD="${DB_PASSWORD}" \
    "${DB_CONTAINER}" \
    psql -U "${DB_USER}" -d "${DB_NAME}" -v ON_ERROR_STOP=1 < "${SCHEMA_FILE}"
fi

echo "Done. Database ${DB_NAME} has been reset and schema.sql was applied."
