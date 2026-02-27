#!/bin/bash
set -e

LOG_DIR="${LOG_DIR:-/var/log/hcli}"

echo "[entrypoint] Creating log directories..."
mkdir -p "$LOG_DIR/core" "$LOG_DIR/memory"

SSH_STAGING="/tmp/ssh-keys-staging"
SSH_DIR="/home/hcli/.ssh"

# Set up SSH keys if staging directory has files
if [ -d "$SSH_STAGING" ] && [ "$(ls -A "$SSH_STAGING" 2>/dev/null)" ]; then
    echo "[entrypoint] Setting up SSH keys..."

    mkdir -p "$SSH_DIR"
    chmod 700 "$SSH_DIR"

    # Copy all files from staging (read-only mount) to writable location
    cp -a "$SSH_STAGING"/. "$SSH_DIR"/

    # Re-apply directory permission (cp -a may override it)
    chmod 700 "$SSH_DIR"

    # Remove leftover git artifacts first
    rm -f "$SSH_DIR/.gitkeep" "$SSH_DIR/README.md"

    # Fix permissions on private keys (no .pub, no config, no known_hosts)
    for f in "$SSH_DIR"/*; do
        [ -e "$f" ] || continue
        filename=$(basename "$f")
        case "$filename" in
            *.pub)
                chmod 644 "$f"
                ;;
            config|known_hosts|authorized_keys)
                chmod 644 "$f"
                ;;
            *)
                chmod 600 "$f"
                ;;
        esac
    done

    # Create default SSH config if not provided
    if [ ! -f "$SSH_DIR/config" ]; then
        cat > "$SSH_DIR/config" <<'EOF'
Host *
    StrictHostKeyChecking accept-new
    UserKnownHostsFile /home/hcli/.ssh/known_hosts
    ServerAliveInterval 60
    ServerAliveCountMax 3
EOF
        chmod 644 "$SSH_DIR/config"
    fi

    chown -R hcli:hcli "$SSH_DIR"

    echo "[entrypoint] SSH keys configured:"
    ls -la "$SSH_DIR"/ | grep -v "^total"
else
    echo "[entrypoint] No SSH keys found in $SSH_STAGING, skipping SSH setup."
fi

chown -R hcli:hcli "$LOG_DIR/core" "$LOG_DIR/memory"

# ── Sudo whitelist ──────────────────────────────────────────────────
# Dangerous argument patterns (ip netns exec, nmap --script, etc.) are
# blocked by the Asimov firewall's pattern denylist (BLOCKED_PATTERNS_FILE),
# not here. This keeps sudoers simple and the block list maintainable.
if [ -n "${SUDO_COMMANDS:-}" ]; then
    echo "[entrypoint] Configuring sudo whitelist..."
    SUDOERS_LINE="hcli ALL=(ALL) NOPASSWD:"
    FIRST=true
    IFS=',' read -ra CMDS <<< "$SUDO_COMMANDS"
    for cmd in "${CMDS[@]}"; do
        cmd=$(echo "$cmd" | xargs)  # trim whitespace
        FULL=$(command -v "$cmd" 2>/dev/null || true)
        if [ -n "$FULL" ]; then
            $FIRST && SUDOERS_LINE="$SUDOERS_LINE $FULL" || SUDOERS_LINE="$SUDOERS_LINE, $FULL"
            FIRST=false
        else
            echo "[entrypoint] WARNING: command '$cmd' not found, skipping"
        fi
    done
    if ! $FIRST; then
        echo "$SUDOERS_LINE" > /etc/sudoers.d/hcli
        chmod 440 /etc/sudoers.d/hcli
        echo "[entrypoint] Sudo whitelist: $SUDOERS_LINE"
    else
        echo "[entrypoint] WARNING: No valid commands found, sudo disabled"
        rm -f /etc/sudoers.d/hcli
    fi
else
    echo "[entrypoint] No SUDO_COMMANDS set, sudo disabled for hcli"
    rm -f /etc/sudoers.d/hcli
fi

# ── Memory server (supervised background) ─────────────────────────
# Always start memory_server.py — it handles Qdrant unavailability
# gracefully (memory_search returns "not initialized" if _init fails).
MEMORY_PID=""

cleanup() {
    if [ -n "$MEMORY_PID" ] && kill -0 "$MEMORY_PID" 2>/dev/null; then
        echo "[entrypoint] Forwarding SIGTERM to memory server (PID $MEMORY_PID)..."
        kill "$MEMORY_PID" 2>/dev/null
        wait "$MEMORY_PID" 2>/dev/null
    fi
}
trap cleanup TERM INT

echo "[entrypoint] Starting memory server on port 8084..."
gosu hcli python3 -u /app/memory_server.py &
MEMORY_PID=$!
echo "[entrypoint] Memory server started (PID $MEMORY_PID)"

# Monitor memory server in background — log if it exits unexpectedly
(
    wait "$MEMORY_PID" 2>/dev/null
    EXIT_CODE=$?
    if [ $EXIT_CODE -ne 0 ]; then
        echo "[entrypoint] WARNING: memory server exited with code $EXIT_CODE"
    fi
) &

echo "[entrypoint] Starting h-cli-core as hcli..."
exec gosu hcli "$@"
