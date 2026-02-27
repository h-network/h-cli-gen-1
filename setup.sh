#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== h-cli Setup ==="
echo ""

# Sanity checks
if [ ! -d ".git" ]; then
    echo "[!] Error: not a git repository."
    echo "    Clone the repo first, then run setup.sh from inside it."
    echo "    Example: git clone <repo-url> h-cli && cd h-cli && bash setup.sh"
    exit 1
fi

if [ ! -f "docker-compose.yml" ]; then
    echo "[!] Error: docker-compose.yml not found. Are you in the h-cli root?"
    exit 1
fi

echo "[*] Working directory: $SCRIPT_DIR"

# --- .env ---

if [ -f .env ]; then
    echo "[*] .env already exists — keeping existing configuration."
else
    cp .env.template .env
    echo "[*] Created .env from template."
fi

# --- ENV_TAG ---

echo ""
echo "Environment tag — namespaces container and network names."
echo "Examples: dev, staging, test, alice"
echo "Leave empty for production (no prefix)."
echo ""

CURRENT_TAG=$(grep -E '^ENV_TAG=' .env | cut -d= -f2- || true)
if [ -n "$CURRENT_TAG" ]; then
    read -rp "ENV_TAG [$CURRENT_TAG]: " ENV_TAG
    ENV_TAG="${ENV_TAG:-$CURRENT_TAG}"
else
    read -rp "ENV_TAG []: " ENV_TAG
fi

# Strip trailing dashes — the compose pattern adds its own separator
ENV_TAG="${ENV_TAG%-}"
ENV_TAG="${ENV_TAG%-}"

sed -i "s/^ENV_TAG=.*/ENV_TAG=$ENV_TAG/" .env
if [ -n "$ENV_TAG" ]; then
    echo "[*] Set ENV_TAG=$ENV_TAG (containers: h-cli-${ENV_TAG}-*)"
else
    echo "[*] ENV_TAG is empty (production mode, containers: h-cli-*)"
fi

# --- Telegram credentials ---

echo ""
CURRENT_TOKEN=$(grep -E '^TELEGRAM_BOT_TOKEN=' .env | cut -d= -f2- || true)
if [ "$CURRENT_TOKEN" = "your-bot-token-here" ] || [ -z "$CURRENT_TOKEN" ]; then
    read -rp "Telegram bot token: " BOT_TOKEN
    sed -i "s/^TELEGRAM_BOT_TOKEN=.*/TELEGRAM_BOT_TOKEN=$BOT_TOKEN/" .env
    echo "[*] Set Telegram bot token."
else
    echo "[*] Telegram bot token already configured — skipping."
fi

CURRENT_CHATS=$(grep -E '^ALLOWED_CHATS=' .env | cut -d= -f2- || true)
if [ -z "$CURRENT_CHATS" ]; then
    read -rp "Allowed Telegram chat IDs (comma-separated): " CHAT_IDS
    sed -i "s/^ALLOWED_CHATS=.*/ALLOWED_CHATS=$CHAT_IDS/" .env
    echo "[*] Set allowed chat IDs."

    # Ask for friendly names per chat ID (used for session chunk directories)
    echo ""
    echo "Give each chat a friendly name (used for session history directories)."
    echo "Leave blank to use the numeric chat ID instead."
    CHAT_NAMES=""
    IFS=',' read -ra IDS <<< "$CHAT_IDS"
    for id in "${IDS[@]}"; do
        id=$(echo "$id" | xargs)  # trim whitespace
        read -rp "  Name for chat $id: " CHAT_NAME
        if [ -n "$CHAT_NAME" ]; then
            CHAT_NAMES="${CHAT_NAMES:+$CHAT_NAMES,}$id:$CHAT_NAME"
        fi
    done
    if [ -n "$CHAT_NAMES" ]; then
        if grep -q '^#\?CHAT_NAMES=' .env 2>/dev/null; then
            sed -i "s/^#\?CHAT_NAMES=.*/CHAT_NAMES=$CHAT_NAMES/" .env
        else
            echo "CHAT_NAMES=$CHAT_NAMES" >> .env
        fi
        echo "[*] Set CHAT_NAMES=$CHAT_NAMES"
    fi
else
    echo "[*] Allowed chat IDs already configured — skipping."
fi

# --- GATE_CHECK ---

echo ""
echo "Asimov Firewall (GATE_CHECK) — independent LLM checks every command."
echo "Adds ~2-3s latency per command. Recommended for production."
read -rp "Enable GATE_CHECK? [Y/n]: " GATE_ANSWER
GATE_ANSWER="${GATE_ANSWER:-Y}"
if [[ "$GATE_ANSWER" =~ ^[Nn] ]]; then
    sed -i 's/^GATE_CHECK=.*/GATE_CHECK=false/' .env
    echo "[*] GATE_CHECK disabled. Re-enable in .env any time."
else
    sed -i 's/^GATE_CHECK=.*/GATE_CHECK=true/' .env
    echo "[*] GATE_CHECK enabled."
fi

# --- Compose profiles (optional services) ---

echo ""
echo "Optional service profiles:"
echo "  monitor  — TimescaleDB + Grafana (metrics dashboards)"
echo "  vectordb — Qdrant (semantic memory search)"
echo ""

PROFILES=""

read -rp "Enable monitoring (TimescaleDB + Grafana)? [y/N]: " MONITOR_ANSWER
MONITOR_ANSWER="${MONITOR_ANSWER:-N}"
if [[ "$MONITOR_ANSWER" =~ ^[Yy] ]]; then
    PROFILES="monitor"
    echo "[*] Monitor profile enabled."
fi

read -rp "Enable vector memory (Qdrant)? [y/N]: " VECTOR_ANSWER
VECTOR_ANSWER="${VECTOR_ANSWER:-N}"
if [[ "$VECTOR_ANSWER" =~ ^[Yy] ]]; then
    PROFILES="${PROFILES:+$PROFILES,}vectordb"
    echo "[*] Vector memory profile enabled."
fi

if [ -n "$PROFILES" ]; then
    if grep -q '^#\?COMPOSE_PROFILES=' .env 2>/dev/null; then
        sed -i "s/^#\?COMPOSE_PROFILES=.*/COMPOSE_PROFILES=$PROFILES/" .env
    else
        echo "COMPOSE_PROFILES=$PROFILES" >> .env
    fi
    echo "[*] Set COMPOSE_PROFILES=$PROFILES"
else
    echo "[*] No optional profiles enabled. Enable later in .env (COMPOSE_PROFILES=monitor,vectordb)."
fi

# --- Remote backups ---

echo ""
CURRENT_BACKUP=$(grep -E '^BACKUP_TARGET=' .env | cut -d= -f2- || true)
if [ -n "$CURRENT_BACKUP" ]; then
    echo "[*] Remote backup already configured: $CURRENT_BACKUP — skipping."
else
    read -rp "Enable remote backups? [y/N]: " BACKUP_ANSWER
    BACKUP_ANSWER="${BACKUP_ANSWER:-N}"
    if [[ "$BACKUP_ANSWER" =~ ^[Yy] ]]; then
        echo "  Format: user@host:/path/to/backup"
        read -rp "BACKUP_TARGET: " BACKUP_TARGET
        if [ -n "$BACKUP_TARGET" ]; then
            sed -i "s|^#\?BACKUP_TARGET=.*|BACKUP_TARGET=$BACKUP_TARGET|" .env
            echo "[*] Set BACKUP_TARGET=$BACKUP_TARGET (daily 3 AM cron via install.sh)."
        else
            echo "[*] No target provided — skipping."
        fi
    else
        echo "[*] Remote backups disabled. Set BACKUP_TARGET in .env later to enable."
    fi
fi

# --- install.sh ---

echo ""
echo "[*] Running install.sh..."
bash install.sh

# --- OAuth / Claude Code authentication ---

echo ""
echo "=== Claude Code Authentication ==="
echo ""
echo "h-cli needs a Claude Max/Pro subscription. You need to authenticate once."
echo ""
read -rp "Set up Claude Code authentication now? [Y/n]: " OAUTH_ANSWER
OAUTH_ANSWER="${OAUTH_ANSWER:-Y}"
if [[ "$OAUTH_ANSWER" =~ ^[Yy]|^$ ]]; then
    echo ""
    echo "[*] Starting Claude Code login..."
    echo "    A browser URL will appear — authenticate and paste the code back."
    echo ""
    docker compose run -it --rm claude-code setup-token
    echo ""
    echo "[*] Authentication complete."
else
    echo "[*] Skipping OAuth setup. Run later:"
    echo "    docker compose run -it --rm claude-code setup-token"
fi

# --- Done ---

echo ""
echo "=== Setup Complete ==="
echo ""
if [ -n "$ENV_TAG" ]; then
    TAG_FILTER="h-cli-${ENV_TAG}"
else
    TAG_FILTER="h-cli"
fi
echo "Next steps:"
echo "  1. Review .env:        nano $SCRIPT_DIR/.env"
echo "  2. Edit persona:       nano $SCRIPT_DIR/context.md"
echo "  3. Add SSH key:        ssh-copy-id -i $SCRIPT_DIR/ssh-keys/id_ed25519.pub user@host"
echo "  4. Start services:     docker compose up -d"
echo "  5. Verify:             docker ps | grep $TAG_FILTER"
echo "  6. Tear down:          docker compose down"
echo ""
