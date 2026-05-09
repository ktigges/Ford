#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Simply restart the app (no git pull)
"${ROOT_DIR}/start.sh" "$@"
