#!/usr/bin/env bash
set -euo pipefail

DB_CONTAINER="${DB_CONTAINER:-lightning-db}"
DB_NAME="${DB_NAME:-lightning}"
DB_USER="${DB_USER:-lightning}"
DB_PASSWORD="${DB_PASSWORD:-lightningpass}"

fail() {
  echo "[FAIL] $1" >&2
  exit 1
}

pass() {
  echo "[PASS] $1"
}

if ! command -v docker >/dev/null 2>&1; then
  fail "docker is required"
fi

if ! docker ps --format '{{.Names}}' | grep -Fxq "${DB_CONTAINER}"; then
  fail "container ${DB_CONTAINER} is not running"
fi
pass "container ${DB_CONTAINER} is running"

if ! docker exec "${DB_CONTAINER}" pg_isready -U "${DB_USER}" -d "${DB_NAME}" >/dev/null 2>&1; then
  fail "PostgreSQL is not ready in ${DB_CONTAINER}"
fi
pass "PostgreSQL is ready"

run_sql() {
  local sql="$1"
  docker exec -e PGPASSWORD="${DB_PASSWORD}" "${DB_CONTAINER}" \
    psql -U "${DB_USER}" -d "${DB_NAME}" -tA -c "${sql}"
}

garage_count="$(run_sql "SELECT COUNT(*) FROM garage;")"
telemetry_count="$(run_sql "SELECT COUNT(*) FROM telemetry;")"
drives_count="$(run_sql "SELECT COUNT(*) FROM drives;")"
drive_points_count="$(run_sql "SELECT COUNT(*) FROM drive_points;")"
oauth_count="$(run_sql "SELECT COUNT(*) FROM oauth_credentials WHERE enabled = TRUE;")"
last_poll="$(run_sql "SELECT COALESCE(MAX(polled_at)::text, 'none') FROM telemetry;")"
json_rows="$(run_sql "SELECT COUNT(*) FROM telemetry WHERE raw_metrics IS NOT NULL;")"

printf "\n=== Live DB Summary ===\n"
printf "garage:            %s\n" "${garage_count}"
printf "telemetry:         %s\n" "${telemetry_count}"
printf "drives:            %s\n" "${drives_count}"
printf "drive_points:      %s\n" "${drive_points_count}"
printf "oauth enabled:     %s\n" "${oauth_count}"
printf "telemetry json rows: %s\n" "${json_rows}"
printf "last telemetry:    %s\n\n" "${last_poll}"

[[ "${garage_count}" -gt 0 ]] || fail "garage is empty"
[[ "${telemetry_count}" -gt 0 ]] || fail "telemetry is empty"
[[ "${json_rows}" -gt 0 ]] || fail "telemetry.raw_metrics appears empty"
[[ "${oauth_count}" -gt 0 ]] || fail "no enabled oauth credentials found"

pass "core migration checks look good"

echo "Optional deep check (manual): compare table counts against source server backup summary."
