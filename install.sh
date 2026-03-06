#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== h-cli Install ==="

# Create .env from template if it doesn't exist
if [ ! -f .env ]; then
    cp .env.template .env
    echo "[*] Created .env from template — edit it with your tokens before starting."
    echo "    vi $SCRIPT_DIR/.env"
    echo ""
fi

# Generate dedicated SSH keypair if none exists
if ! ls ssh-keys/id_* &>/dev/null; then
    echo ""
    echo "SSH keypair — used by h-cli to connect to remote hosts."
    read -rp "Generate SSH keypair? [Y/n]: " SSH_ANSWER
    SSH_ANSWER="${SSH_ANSWER:-Y}"
    if [[ "$SSH_ANSWER" =~ ^[Yy] ]]; then
        mkdir -p -m 700 ssh-keys
        echo "[*] Generating hcli SSH keypair..."
        ssh-keygen -t ed25519 -f ssh-keys/id_ed25519 -N "" -C "hcli@$(hostname)"
        echo ""
        echo "[*] Public key (add this to your servers' authorized_keys):"
        echo ""
        cat ssh-keys/id_ed25519.pub
        echo ""
    else
        echo "[*] Skipping SSH keypair. Generate later: ssh-keygen -t ed25519 -f ssh-keys/id_ed25519"
    fi
else
    echo "[*] SSH keypair already exists — skipping."
fi

# Generate Redis password if not set (or still placeholder)
CURRENT_REDIS_PW=$(grep -E '^REDIS_PASSWORD=' .env | cut -d= -f2- || true)
if [ -z "$CURRENT_REDIS_PW" ] || [ "$CURRENT_REDIS_PW" = "changeme-generate-a-real-password" ]; then
    REDIS_PASS=$(openssl rand -hex 24)
    sed -i "s/^REDIS_PASSWORD=.*/REDIS_PASSWORD=$REDIS_PASS/" .env
    echo "[*] Generated REDIS_PASSWORD."
    echo ""
fi

# Generate HMAC key for result signing if not set
if ! grep -q '^RESULT_HMAC_KEY=.\+' .env 2>/dev/null; then
    HMAC_KEY=$(openssl rand -hex 32)
    if grep -q '^RESULT_HMAC_KEY=' .env 2>/dev/null; then
        sed -i "s/^RESULT_HMAC_KEY=.*/RESULT_HMAC_KEY=$HMAC_KEY/" .env
    else
        echo "RESULT_HMAC_KEY=$HMAC_KEY" >> .env
    fi
    echo "[*] Generated HMAC key for result signing."
    echo ""
fi

# Generate Qdrant API key if not set
if ! grep -q '^QDRANT_API_KEY=.\+' .env 2>/dev/null; then
    QDRANT_KEY=$(openssl rand -hex 32)
    if grep -q '^QDRANT_API_KEY=' .env 2>/dev/null; then
        sed -i "s/^QDRANT_API_KEY=.*/QDRANT_API_KEY=$QDRANT_KEY/" .env
    else
        echo "QDRANT_API_KEY=$QDRANT_KEY" >> .env
    fi
    echo "[*] Generated Qdrant API key."
    echo ""
fi

# Generate TimescaleDB password and connection URL if not set
if ! grep -q '^TIMESCALE_PASSWORD=.\+' .env 2>/dev/null; then
    TS_PASS=$(openssl rand -hex 32)
    if grep -q '^TIMESCALE_PASSWORD=' .env 2>/dev/null; then
        sed -i "s/^TIMESCALE_PASSWORD=.*/TIMESCALE_PASSWORD=$TS_PASS/" .env
    elif grep -q '^#TIMESCALE_PASSWORD=' .env 2>/dev/null; then
        sed -i "s/^#TIMESCALE_PASSWORD=.*/TIMESCALE_PASSWORD=$TS_PASS/" .env
    else
        echo "TIMESCALE_PASSWORD=$TS_PASS" >> .env
    fi
    # Also set TIMESCALE_URL with the generated password
    TS_URL="postgresql://hcli:${TS_PASS}@h-cli-timescaledb:5432/hcli_metrics"
    if grep -q '^#\?TIMESCALE_URL=' .env 2>/dev/null; then
        sed -i "s|^#\?TIMESCALE_URL=.*|TIMESCALE_URL=$TS_URL|" .env
    else
        echo "TIMESCALE_URL=$TS_URL" >> .env
    fi
    echo "[*] Generated TimescaleDB password and connection URL."
    echo ""
fi

# Generate Grafana admin password if not set
if ! grep -q '^GRAFANA_ADMIN_PASSWORD=.\+' .env 2>/dev/null; then
    GF_PASS=$(openssl rand -hex 32)
    if grep -q '^GRAFANA_ADMIN_PASSWORD=' .env 2>/dev/null; then
        sed -i "s/^GRAFANA_ADMIN_PASSWORD=.*/GRAFANA_ADMIN_PASSWORD=$GF_PASS/" .env
    elif grep -q '^#GRAFANA_ADMIN_PASSWORD=' .env 2>/dev/null; then
        sed -i "s/^#GRAFANA_ADMIN_PASSWORD=.*/GRAFANA_ADMIN_PASSWORD=$GF_PASS/" .env
    else
        echo "GRAFANA_ADMIN_PASSWORD=$GF_PASS" >> .env
    fi
    echo "[*] Generated Grafana admin password."
    echo ""
fi

# Generate self-signed SSL certificate if none exists
mkdir -p ssl
if [ ! -f ssl/cert.pem ] || [ ! -f ssl/key.pem ]; then
    echo "[*] Generating self-signed SSL certificate..."
    openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
        -keyout ssl/key.pem -out ssl/cert.pem \
        -subj "/CN=h-cli" -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" \
        2>/dev/null
    echo "[*] SSL certificate generated (10-year, self-signed)."
    echo "    To use your own: replace ssl/cert.pem and ssl/key.pem"
    echo ""
else
    echo "[*] SSL certificate already exists — skipping."
fi

# Create context.md from template if it doesn't exist
if [ ! -f context.md ]; then
    cp llm/context.md.template context.md
    echo "[*] Created context.md from template — customize it to describe your deployment."
    echo "    vi $SCRIPT_DIR/context.md"
    echo ""
fi

# Helper: chown with sudo fallback
safe_chown() {
    local owner="$1"; shift
    if chown -R "$owner" "$@" 2>/dev/null; then return 0
    elif command -v sudo >/dev/null 2>&1 && sudo chown -R "$owner" "$@" 2>/dev/null; then return 0
    else
        echo "[!] Warning: could not chown $* to $owner."
        echo "    Fix manually: sudo chown -R $owner $*"
    fi
}

# Ensure log directories exist (uid 1000 = hcli user in claude-code container)
mkdir -p logs/core logs/telegram logs/slack logs/discord logs/web logs/sessions logs/claude logs/firewall logs/memory logs/memory-proxy logs/media
safe_chown 1000:1000 logs/claude logs/firewall logs/sessions logs/telegram logs/slack logs/discord logs/web logs/memory logs/memory-proxy logs/media

# Persistent data directories (uid 1000 = hcli user in containers)
mkdir -p -m 700 data/redis data/claude-credentials data/qdrant data/timescaledb data/grafana
safe_chown 1000:1000 data/redis data/claude-credentials data/qdrant
# TimescaleDB runs as postgres (uid 70 in alpine, 999 in debian — let container own it)
# Grafana runs as grafana (uid 472)
safe_chown 472:472 data/grafana

# Shortcut to Claude Code conversation JSONL files
mkdir -p data/claude-credentials/projects/-app
ln -sfn claude-credentials/projects/-app data/conversations

# Set up backup cron (daily 3 AM) if not already present
CRON_CMD="0 3 * * * $SCRIPT_DIR/backup.sh >> $SCRIPT_DIR/logs/backup.log 2>&1"
if ! crontab -l 2>/dev/null | grep -qF "$SCRIPT_DIR/backup.sh"; then
    (crontab -l 2>/dev/null; echo "$CRON_CMD") | crontab -
    echo "[*] Added daily backup cron (3 AM). Set BACKUP_TARGET in .env for remote sync."
    echo ""
fi

# Build containers
read -rp "Fresh build (no cache)? [y/N]: " NOCACHE_ANSWER
NOCACHE_ANSWER="${NOCACHE_ANSWER:-N}"
if [[ "$NOCACHE_ANSWER" =~ ^[Yy] ]]; then
    echo "[*] Building containers (no cache)..."
    docker compose build --no-cache
else
    echo "[*] Building containers..."
    docker compose build
fi

echo ""
echo "=== Done ==="
echo ""
echo "Next steps:"
echo "  1. Edit .env with your tokens:  vi $SCRIPT_DIR/.env"
echo "  2. Add the public key to your servers:  ssh-copy-id -i $SCRIPT_DIR/ssh-keys/id_ed25519.pub user@host"
echo "  3. Optional: set BACKUP_TARGET in .env for remote backups"
echo "  4. Start services:              docker compose up -d"
echo ""
