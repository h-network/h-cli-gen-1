#!/bin/bash
set -e

if [ "$1" = "setup-token" ]; then
    echo "[entrypoint] Starting Claude Code authentication..."
    exec claude setup-token
fi

echo "[entrypoint] Starting h-cli-claude dispatcher as $(whoami)..."
exec "$@"
