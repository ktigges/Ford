#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ENTRY="${ROOT_DIR}/app.py"
PYTHON_BIN="${PYTHON_BIN:-${ROOT_DIR}/venv/bin/python}"
LOG_FILE="${ROOT_DIR}/logs/stdout.log"

ENSURE_DB=true

for arg in "$@"; do
    case "$arg" in
        --no-db)
            ENSURE_DB=false
            ;;
        *)
            echo "Unknown option: ${arg}" >&2
            echo "Usage: ./start.sh [--no-db]" >&2
            exit 1
            ;;
    esac
done

mkdir -p "${ROOT_DIR}/logs"

if [[ "${ENSURE_DB}" == "true" ]]; then
    "${ROOT_DIR}/scripts/start_db_container.sh"
fi

EXISTING_PIDS="$(pgrep -f "${APP_ENTRY}" || true)"
if [[ -n "${EXISTING_PIDS}" ]]; then
    echo "Stopping existing app.py process(es): ${EXISTING_PIDS}"
    kill ${EXISTING_PIDS}
    sleep 2
fi

echo "Starting app.py in the background..."
nohup "${PYTHON_BIN}" "${APP_ENTRY}" > "${LOG_FILE}" 2>&1 &

APP_PID="$(pgrep -f "${APP_ENTRY}" | tr '\n' ' ' | sed 's/[[:space:]]*$//')"
echo "App started with PID(s): ${APP_PID}"