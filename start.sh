#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ENTRY="${ROOT_DIR}/app.py"
PYTHON_BIN="${PYTHON_BIN:-${ROOT_DIR}/venv/bin/python}"
LOG_FILE="${ROOT_DIR}/logs/stdout.log"
ENV_FILE="${ROOT_DIR}/.env"

ENSURE_DB=true
SKIP_ENV_CHECK=false

for arg in "$@"; do
    case "$arg" in
        --no-db)
            ENSURE_DB=false
            ;;
        --skip-env-check)
            SKIP_ENV_CHECK=true
            ;;
        *)
            echo "Unknown option: ${arg}" >&2
            echo "Usage: ./start.sh [--no-db] [--skip-env-check]" >&2
            exit 1
            ;;
    esac
done

# ============================================================================
# Load environment from .env file if it exists
# ============================================================================
if [[ -f "${ENV_FILE}" ]]; then
    echo "Loading environment from .env..."
    set -a  # Mark all new variables for export
    # shellcheck disable=SC1090
    source "${ENV_FILE}"
    set +a
else
    echo "⚠️  .env file not found at ${ENV_FILE}"
    echo "   Copy .env.example to .env and configure it:"
    echo "   cp .env.example .env"
fi

# ============================================================================
# Validate required environment variables
# ============================================================================
if [[ "${SKIP_ENV_CHECK}" != "true" ]]; then
    if [[ -z "${LIGHTNING_SECRET_KEY:-}" ]]; then
        echo ""
        echo "❌ ERROR: LIGHTNING_SECRET_KEY environment variable is not set"
        echo ""
        echo "To fix this:"
        echo "  1. If you don't have a .env file:"
        echo "     cp .env.example .env"
        echo ""
        echo "  2. Generate a secure secret key:"
        echo "     python -c 'import secrets; print(secrets.token_urlsafe(32))'"
        echo ""
        echo "  3. Add to .env:"
        echo "     LIGHTNING_SECRET_KEY=<generated-key>"
        echo ""
        echo "  4. Run start.sh again"
        echo ""
        exit 1
    fi

    if [[ -z "${LIGHTNING_DB_USER:-}" ]]; then
        echo ""
        echo "❌ ERROR: LIGHTNING_DB_USER environment variable is not set"
        echo ""
        echo "Run ./setup-env.sh to create and populate .env, then try again."
        echo ""
        exit 1
    fi

    if [[ -z "${LIGHTNING_DB_PASSWORD:-}" ]]; then
        echo ""
        echo "❌ ERROR: LIGHTNING_DB_PASSWORD environment variable is not set"
        echo ""
        echo "Run ./setup-env.sh to create and populate .env, then try again."
        echo ""
        exit 1
    fi
fi

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
echo "Log file: ${LOG_FILE}"