#!/usr/bin/env bash
set -euo pipefail

DB_CONTAINER="${DB_CONTAINER:-lightning-db}"
DB_USER="${DB_USER:-lightning}"
DB_NAME="${DB_NAME:-lightning}"
WAIT_SECONDS="${DB_WAIT_SECONDS:-45}"

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker is required to manage the PostgreSQL container." >&2
  exit 1
fi

if ! docker ps -a --format '{{.Names}}' | grep -Fxq "${DB_CONTAINER}"; then
  echo "ERROR: container '${DB_CONTAINER}' does not exist." >&2
  echo "Run scripts/install.sh first to create and initialize it." >&2
  exit 1
fi

if [[ "$(docker inspect -f '{{.State.Running}}' "${DB_CONTAINER}")" != "true" ]]; then
  echo "Starting database container '${DB_CONTAINER}'..."
  docker start "${DB_CONTAINER}" >/dev/null
else
  echo "Database container '${DB_CONTAINER}' is already running."
fi

echo "Waiting for PostgreSQL readiness..."
for _ in $(seq 1 "${WAIT_SECONDS}"); do
  if docker exec "${DB_CONTAINER}" pg_isready -U "${DB_USER}" -d "${DB_NAME}" >/dev/null 2>&1; then
    echo "PostgreSQL is ready."
    exit 0
  fi
  sleep 1
done

echo "ERROR: PostgreSQL did not become ready in ${WAIT_SECONDS}s." >&2
exit 1
