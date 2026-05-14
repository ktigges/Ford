#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${LOG_DIR:-${ROOT_DIR}/logs}"
ARCHIVE_DIR="${ARCHIVE_DIR:-${LOG_DIR}/archive}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"
TIMESTAMP="$(date -u +%Y%m%d_%H%M%S)"

mkdir -p "${ARCHIVE_DIR}"

shopt -s nullglob
for log_file in "${LOG_DIR}"/*.log; do
    # Skip non-regular files if any exist.
    [[ -f "${log_file}" ]] || continue

    base_name="$(basename "${log_file}")"
    archive_file="${ARCHIVE_DIR}/${base_name}.${TIMESTAMP}.gz"

    # Copy + truncate keeps the same inode so active FileHandler writers continue logging.
    gzip -c "${log_file}" > "${archive_file}"
    : > "${log_file}"

done
shopt -u nullglob

# Keep only recent archives to limit disk usage.
find "${ARCHIVE_DIR}" -type f -name "*.gz" -mtime +"${RETENTION_DAYS}" -delete

echo "Log rotation complete at ${TIMESTAMP} (retention=${RETENTION_DAYS} days)."
