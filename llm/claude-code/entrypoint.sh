#!/bin/bash
set -e

# ── Claude auth (read-only staging → writable home) ──────────────
CLAUDE_STAGING="/tmp/claude-staging"
CLAUDE_DIR="/home/hcli/.claude"

if [ -d "$CLAUDE_STAGING" ] && [ "$(ls -A "$CLAUDE_STAGING" 2>/dev/null)" ]; then
    echo "[entrypoint] Setting up Claude credentials..."
    mkdir -p "$CLAUDE_DIR"
    cp -a "$CLAUDE_STAGING"/. "$CLAUDE_DIR"/
    echo "[entrypoint] Claude credentials configured."
else
    echo "[entrypoint] No Claude credentials found in $CLAUDE_STAGING."
    echo "[entrypoint] Run: docker exec -it <container> claude   (then Ctrl+C when done)"
fi

echo "[entrypoint] Starting h-cli-claude dispatcher as $(whoami)..."
exec "$@"
