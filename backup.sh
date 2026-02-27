#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKUP_DIR="${SCRIPT_DIR}/backups"
TIMESTAMP=$(date +%Y%m%dT%H%M%S)

# Load BACKUP_TARGET from .env if available
if [ -f "$SCRIPT_DIR/.env" ]; then
    BACKUP_TARGET=$(grep -E '^BACKUP_TARGET=' "$SCRIPT_DIR/.env" | cut -d= -f2-)
fi

# State files — everything git doesn't track but you'd want back
BACKUP_PATHS=(
    .env
    context.md
    ssh-keys/
    logs/
    data/
    skills/private/
)

# ── Local backup (tar) ────────────────────────────────────────────────
mkdir -p "$BACKUP_DIR"

# Build tar args — only include paths that exist
TAR_ARGS=()
for p in "${BACKUP_PATHS[@]}"; do
    [ -e "$SCRIPT_DIR/$p" ] && TAR_ARGS+=("$p")
done

if [ ${#TAR_ARGS[@]} -eq 0 ]; then
    echo "Nothing to back up."
    exit 0
fi

TARBALL="$BACKUP_DIR/h-cli-backup-${TIMESTAMP}.tar.gz"
tar czf "$TARBALL" -C "$SCRIPT_DIR" "${TAR_ARGS[@]}"
echo "Local:  $TARBALL"

# Keep only last 5 local backups
ls -t "$BACKUP_DIR"/h-cli-backup-*.tar.gz 2>/dev/null | tail -n +6 | xargs -r rm --

# ── Remote backup (rsync) ─────────────────────────────────────────────
if [ -z "$BACKUP_TARGET" ]; then
    echo "Remote: skipped (BACKUP_TARGET not set in .env)"
    exit 0
fi

# Use h-cli SSH key if available
SSH_KEY="$SCRIPT_DIR/ssh-keys/id_ed25519"
SSH_OPTS=""
[ -f "$SSH_KEY" ] && SSH_OPTS="-e \"ssh -i $SSH_KEY\""

# Build rsync include list
RSYNC_ARGS=()
for p in "${BACKUP_PATHS[@]}"; do
    [ -e "$SCRIPT_DIR/$p" ] && RSYNC_ARGS+=("$p")
done

eval rsync -avz --relative $SSH_OPTS "${RSYNC_ARGS[@]/#/$SCRIPT_DIR/.\/}" "$BACKUP_TARGET"
echo "Remote: pushed state to $BACKUP_TARGET"

# ── Pull processed training data back ─────────────────────────────────
# Pipeline drops processed Q/A into a known path on the remote.
# Pull it into data/ for local ingest into Qdrant.
IMPORT_PATH="data/import/"
mkdir -p "$SCRIPT_DIR/$IMPORT_PATH"
eval rsync -avz $SSH_OPTS "$BACKUP_TARGET/$IMPORT_PATH" "$SCRIPT_DIR/$IMPORT_PATH" 2>/dev/null && \
    echo "Remote: pulled training data from $BACKUP_TARGET/$IMPORT_PATH" || \
    echo "Remote: no training data to pull (ok)"
