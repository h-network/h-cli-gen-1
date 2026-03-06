#!/bin/bash
set -e

LOG_DIR="${LOG_DIR:-/var/log/hcli}"

echo "[entrypoint] Creating log directories..."
mkdir -p "$LOG_DIR/discord"

echo "[entrypoint] Starting h-cli-discord..."
exec "$@"
