#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_SCRIPT="${ROOT_DIR}/scripts/install.sh"
START_SCRIPT="${ROOT_DIR}/start.sh"

RESTORE_SQL_FILE=""
NO_PULL=false

usage() {
  cat <<'USAGE'
Usage:
  ./scripts/deploy.sh [--restore-sql <backup.sql>] [--no-pull]

Behavior:
  1. git pull --ff-only (unless --no-pull)
  2. install.sh (or install.sh --restore-sql <backup.sql>)
  3. start.sh (app restart)
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --restore-sql)
      RESTORE_SQL_FILE="${2:-}"
      if [[ -z "${RESTORE_SQL_FILE}" ]]; then
        echo "ERROR: --restore-sql requires a file path." >&2
        usage
        exit 1
      fi
      if [[ ! -f "${RESTORE_SQL_FILE}" ]]; then
        echo "ERROR: restore file not found: ${RESTORE_SQL_FILE}" >&2
        exit 1
      fi
      shift 2
      ;;
    --no-pull)
      NO_PULL=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown option: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ ! -x "${INSTALL_SCRIPT}" ]]; then
  echo "ERROR: install script not found or not executable: ${INSTALL_SCRIPT}" >&2
  exit 1
fi

if [[ ! -x "${START_SCRIPT}" ]]; then
  echo "ERROR: start script not found or not executable: ${START_SCRIPT}" >&2
  exit 1
fi

cd "${ROOT_DIR}"

if [[ "${NO_PULL}" == "false" ]]; then
  echo "Pulling latest code from git..."
  if ! git pull --ff-only; then
    echo "ERROR: git pull failed. Aborting deploy." >&2
    exit 1
  fi
  echo "git pull completed successfully."
else
  echo "Skipping git pull (--no-pull)."
fi

if [[ -n "${RESTORE_SQL_FILE}" ]]; then
  echo "Running install with restore: ${RESTORE_SQL_FILE}"
  "${INSTALL_SCRIPT}" --restore-sql "${RESTORE_SQL_FILE}"
else
  echo "Running install without restore..."
  "${INSTALL_SCRIPT}"
fi

echo "Restarting app..."
"${START_SCRIPT}"

echo "Deploy complete."
