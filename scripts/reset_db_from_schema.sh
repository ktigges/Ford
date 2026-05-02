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

echo "Done. Database ${DB_NAME} has been reset and schema.sql was applied."
