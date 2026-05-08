#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Update code from git before restarting
echo "Pulling latest code from git..."
if ! git pull --ff-only; then
    echo "git pull failed. Aborting restart so the running app is left unchanged."
    exit 1
fi
echo "git pull completed successfully."

"${ROOT_DIR}/start.sh" "$@"