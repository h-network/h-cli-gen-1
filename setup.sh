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

# --- Interface selection ---

echo ""
echo "Which interfaces do you want to enable?"
echo "  1. Telegram  (requires bot token from @BotFather)"
echo "  2. Slack     (requires Slack app with Socket Mode)"
echo "  3. Discord   (requires Discord app with MESSAGE_CONTENT intent)"
echo "  4. Web UI    (requires username + password)"
echo ""

CURRENT_PROFILES=$(grep -E '^COMPOSE_PROFILES=' .env | cut -d= -f2- || true)

# Detect already-configured interfaces
SELECTED_INTERFACES=""
if [[ "$CURRENT_PROFILES" == *"telegram"* ]] || grep -qE '^TELEGRAM_BOT_TOKEN=.+' .env 2>/dev/null; then
    DEFAULT_IFACES="${DEFAULT_IFACES:+$DEFAULT_IFACES,}1"
fi
if [[ "$CURRENT_PROFILES" == *"slack"* ]] || grep -qE '^SLACK_BOT_TOKEN=.+' .env 2>/dev/null; then
    DEFAULT_IFACES="${DEFAULT_IFACES:+$DEFAULT_IFACES,}2"
fi
if [[ "$CURRENT_PROFILES" == *"discord"* ]] || grep -qE '^DISCORD_BOT_TOKEN=.+' .env 2>/dev/null; then
    DEFAULT_IFACES="${DEFAULT_IFACES:+$DEFAULT_IFACES,}3"
fi
if [[ "$CURRENT_PROFILES" == *"web"* ]] || grep -qE '^WEB_USERNAME=.+' .env 2>/dev/null; then
    DEFAULT_IFACES="${DEFAULT_IFACES:+$DEFAULT_IFACES,}4"
fi

if [ -n "$DEFAULT_IFACES" ]; then
    read -rp "Select (comma-separated, e.g. 1,4) [$DEFAULT_IFACES]: " IFACE_CHOICE
    IFACE_CHOICE="${IFACE_CHOICE:-$DEFAULT_IFACES}"
else
    read -rp "Select (comma-separated, e.g. 1,4): " IFACE_CHOICE
fi

ENABLE_TELEGRAM=false
ENABLE_SLACK=false
ENABLE_DISCORD=false
ENABLE_WEB=false
IFACE_PROFILES=""

IFS=',' read -ra IFACE_NUMS <<< "$IFACE_CHOICE"
for num in "${IFACE_NUMS[@]}"; do
    num=$(echo "$num" | xargs)
    case "$num" in
        1) ENABLE_TELEGRAM=true; IFACE_PROFILES="${IFACE_PROFILES:+$IFACE_PROFILES,}telegram" ;;
        2) ENABLE_SLACK=true; IFACE_PROFILES="${IFACE_PROFILES:+$IFACE_PROFILES,}slack" ;;
        3) ENABLE_DISCORD=true; IFACE_PROFILES="${IFACE_PROFILES:+$IFACE_PROFILES,}discord" ;;
        4) ENABLE_WEB=true; IFACE_PROFILES="${IFACE_PROFILES:+$IFACE_PROFILES,}web" ;;
        *) echo "[!] Unknown interface: $num — skipping." ;;
    esac
done

if [ -z "$IFACE_PROFILES" ]; then
    echo "[!] Warning: no interfaces selected. h-cli needs at least one interface to be useful."
    echo "    Re-run setup.sh or edit COMPOSE_PROFILES in .env manually."
fi

echo "[*] Enabled interfaces: ${IFACE_PROFILES:-none}"

# --- Telegram credentials ---

if [ "$ENABLE_TELEGRAM" = true ]; then
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
fi

# --- Slack credentials ---

if [ "$ENABLE_SLACK" = true ]; then
    echo ""
    CURRENT_SLACK_BOT=$(grep -E '^SLACK_BOT_TOKEN=' .env | cut -d= -f2- || true)
    if [ -z "$CURRENT_SLACK_BOT" ] || [ "$CURRENT_SLACK_BOT" = "xoxb-your-bot-token" ]; then
        read -rp "Slack Bot Token (xoxb-...): " SLACK_BOT_TOKEN
        if grep -q '^#\?SLACK_BOT_TOKEN=' .env 2>/dev/null; then
            sed -i "s|^#\?SLACK_BOT_TOKEN=.*|SLACK_BOT_TOKEN=$SLACK_BOT_TOKEN|" .env
        else
            echo "SLACK_BOT_TOKEN=$SLACK_BOT_TOKEN" >> .env
        fi
        echo "[*] Set Slack bot token."
    else
        echo "[*] Slack bot token already configured — skipping."
    fi

    CURRENT_SLACK_APP=$(grep -E '^SLACK_APP_TOKEN=' .env | cut -d= -f2- || true)
    if [ -z "$CURRENT_SLACK_APP" ] || [ "$CURRENT_SLACK_APP" = "xapp-your-app-token" ]; then
        read -rp "Slack App Token (xapp-...): " SLACK_APP_TOKEN
        if grep -q '^#\?SLACK_APP_TOKEN=' .env 2>/dev/null; then
            sed -i "s|^#\?SLACK_APP_TOKEN=.*|SLACK_APP_TOKEN=$SLACK_APP_TOKEN|" .env
        else
            echo "SLACK_APP_TOKEN=$SLACK_APP_TOKEN" >> .env
        fi
        echo "[*] Set Slack app token."
    else
        echo "[*] Slack app token already configured — skipping."
    fi

    CURRENT_SLACK_USERS=$(grep -E '^SLACK_ALLOWED_USERS=' .env | cut -d= -f2- || true)
    if [ -z "$CURRENT_SLACK_USERS" ]; then
        read -rp "Allowed Slack user IDs (comma-separated, e.g. U12345,U67890): " SLACK_USERS
        if grep -q '^#\?SLACK_ALLOWED_USERS=' .env 2>/dev/null; then
            sed -i "s|^#\?SLACK_ALLOWED_USERS=.*|SLACK_ALLOWED_USERS=$SLACK_USERS|" .env
        else
            echo "SLACK_ALLOWED_USERS=$SLACK_USERS" >> .env
        fi
        echo "[*] Set Slack allowed users."
    else
        echo "[*] Slack allowed users already configured — skipping."
    fi
fi

# --- Discord credentials ---

if [ "$ENABLE_DISCORD" = true ]; then
    echo ""
    CURRENT_DISCORD_TOKEN=$(grep -E '^DISCORD_BOT_TOKEN=' .env | cut -d= -f2- || true)
    if [ -z "$CURRENT_DISCORD_TOKEN" ] || [ "$CURRENT_DISCORD_TOKEN" = "your-bot-token" ]; then
        read -rp "Discord Bot Token: " DISCORD_BOT_TOKEN
        if grep -q '^#\?DISCORD_BOT_TOKEN=' .env 2>/dev/null; then
            sed -i "s|^#\?DISCORD_BOT_TOKEN=.*|DISCORD_BOT_TOKEN=$DISCORD_BOT_TOKEN|" .env
        else
            echo "DISCORD_BOT_TOKEN=$DISCORD_BOT_TOKEN" >> .env
        fi
        echo "[*] Set Discord bot token."
    else
        echo "[*] Discord bot token already configured — skipping."
    fi

    CURRENT_DISCORD_USERS=$(grep -E '^DISCORD_ALLOWED_USERS=' .env | cut -d= -f2- || true)
    if [ -z "$CURRENT_DISCORD_USERS" ]; then
        read -rp "Allowed Discord user IDs (comma-separated): " DISCORD_USERS
        if grep -q '^#\?DISCORD_ALLOWED_USERS=' .env 2>/dev/null; then
            sed -i "s|^#\?DISCORD_ALLOWED_USERS=.*|DISCORD_ALLOWED_USERS=$DISCORD_USERS|" .env
        else
            echo "DISCORD_ALLOWED_USERS=$DISCORD_USERS" >> .env
        fi
        echo "[*] Set Discord allowed users."
    else
        echo "[*] Discord allowed users already configured — skipping."
    fi

    CURRENT_DISCORD_GUILDS=$(grep -E '^DISCORD_GUILD_IDS=' .env | cut -d= -f2- || true)
    if [ -z "$CURRENT_DISCORD_GUILDS" ]; then
        read -rp "Discord Guild/Server IDs for instant command sync (comma-separated, recommended): " DISCORD_GUILDS
        if [ -n "$DISCORD_GUILDS" ]; then
            if grep -q '^#\?DISCORD_GUILD_IDS=' .env 2>/dev/null; then
                sed -i "s|^#\?DISCORD_GUILD_IDS=.*|DISCORD_GUILD_IDS=$DISCORD_GUILDS|" .env
            else
                echo "DISCORD_GUILD_IDS=$DISCORD_GUILDS" >> .env
            fi
            echo "[*] Set Discord guild IDs."
        else
            echo "[*] No guild IDs — slash commands will sync globally (up to 1h delay)."
        fi
    else
        echo "[*] Discord guild IDs already configured — skipping."
    fi
fi

# --- Web UI credentials ---

if [ "$ENABLE_WEB" = true ]; then
    echo ""
    CURRENT_WEB_USER=$(grep -E '^WEB_USERNAME=' .env | cut -d= -f2- || true)
    if [ -z "$CURRENT_WEB_USER" ]; then
        read -rp "Web UI username [admin]: " WEB_USER
        WEB_USER="${WEB_USER:-admin}"
        if grep -q '^#\?WEB_USERNAME=' .env 2>/dev/null; then
            sed -i "s|^#\?WEB_USERNAME=.*|WEB_USERNAME=$WEB_USER|" .env
        else
            echo "WEB_USERNAME=$WEB_USER" >> .env
        fi
        echo "[*] Set Web UI username: $WEB_USER"
    else
        echo "[*] Web UI username already configured ($CURRENT_WEB_USER) — skipping."
    fi

    CURRENT_WEB_PASS=$(grep -E '^WEB_PASSWORD=' .env | cut -d= -f2- || true)
    if [ -z "$CURRENT_WEB_PASS" ] || [ "$CURRENT_WEB_PASS" = "changeme-generate-a-real-password" ]; then
        read -rp "Web UI password (leave empty to auto-generate): " WEB_PASS
        if [ -z "$WEB_PASS" ]; then
            WEB_PASS=$(openssl rand -base64 18)
            echo "[*] Auto-generated Web UI password: $WEB_PASS"
        fi
        if grep -q '^#\?WEB_PASSWORD=' .env 2>/dev/null; then
            sed -i "s|^#\?WEB_PASSWORD=.*|WEB_PASSWORD=$WEB_PASS|" .env
        else
            echo "WEB_PASSWORD=$WEB_PASS" >> .env
        fi
        echo "[*] Set Web UI password."
    else
        echo "[*] Web UI password already configured — skipping."
    fi
fi

# --- Web SSL ---

if [ "$ENABLE_WEB" = true ]; then
    echo ""
    echo "SSL for the web UI — serves HTTPS with a self-signed certificate."
    echo "To use your own certs, place cert.pem and key.pem in ssl/"
    echo "To disable (behind a load balancer), set WEB_SSL=false"
    echo ""
    read -rp "Enable HTTPS for web UI? [Y/n]: " SSL_ANSWER
    SSL_ANSWER="${SSL_ANSWER:-Y}"
    if [[ "$SSL_ANSWER" =~ ^[Nn] ]]; then
        sed -i 's/^WEB_SSL=.*/WEB_SSL=false/' .env
        # Default to port 8080 for HTTP
        if grep -q '^#\?WEB_PORT=' .env 2>/dev/null; then
            sed -i 's/^#\?WEB_PORT=.*/WEB_PORT=8080/' .env
        fi
        echo "[*] Web UI will serve HTTP on port 8080."
    else
        sed -i 's/^WEB_SSL=.*/WEB_SSL=true/' .env
        if grep -q '^#\?WEB_PORT=' .env 2>/dev/null; then
            sed -i 's/^#\?WEB_PORT=.*/WEB_PORT=8443/' .env
        fi
        echo "[*] Web UI will serve HTTPS on port 8443 (self-signed cert auto-generated by install.sh)."
    fi
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

# Start with interface profiles
PROFILES="$IFACE_PROFILES"

read -rp "Enable monitoring (TimescaleDB + Grafana)? [y/N]: " MONITOR_ANSWER
MONITOR_ANSWER="${MONITOR_ANSWER:-N}"
if [[ "$MONITOR_ANSWER" =~ ^[Yy] ]]; then
    PROFILES="${PROFILES:+$PROFILES,}monitor"
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
    echo "[*] No profiles enabled. Enable later in .env (COMPOSE_PROFILES=telegram,web,monitor)."
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

# --- Knowledge indexing ---

echo ""
echo "h-cli can auto-index completed conversations into vector memory."
echo "Conversations become searchable knowledge — the bot learns from itself."
echo "Runs daily at a time you choose. Requires the vectordb profile (Qdrant)."
echo ""
read -rp "Enable conversation auto-indexing? [Y/n]: " INDEX_ANSWER
INDEX_ANSWER="${INDEX_ANSWER:-Y}"
if [[ "$INDEX_ANSWER" =~ ^[Yy] ]]; then
    read -rp "Index time (24h format) [3:00]: " INDEX_TIME
    INDEX_TIME="${INDEX_TIME:-3:00}"
    INDEX_HOUR=$(echo "$INDEX_TIME" | cut -d: -f1)
    INDEX_MIN=$(echo "$INDEX_TIME" | cut -d: -f2)
    # Validate
    if ! [[ "$INDEX_HOUR" =~ ^[0-9]{1,2}$ ]] || ! [[ "$INDEX_MIN" =~ ^[0-9]{1,2}$ ]]; then
        INDEX_HOUR=3
        INDEX_MIN=0
        echo "[!] Invalid time, defaulting to 3:00."
    fi
    # Ensure vectordb profile is enabled
    if [[ "$PROFILES" != *"vectordb"* ]]; then
        PROFILES="${PROFILES:+$PROFILES,}vectordb"
        echo "[*] Auto-enabled vectordb profile (required for indexing)."
    fi
    # Add conversations to QDRANT_COLLECTIONS
    CURRENT_COLLECTIONS=$(grep -E '^QDRANT_COLLECTIONS=' .env | cut -d= -f2- || true)
    if [[ "$CURRENT_COLLECTIONS" != *"conversations"* ]]; then
        NEW_COLLECTIONS="${CURRENT_COLLECTIONS:+$CURRENT_COLLECTIONS,}conversations"
        if grep -q '^#\?QDRANT_COLLECTIONS=' .env 2>/dev/null; then
            sed -i "s|^#\?QDRANT_COLLECTIONS=.*|QDRANT_COLLECTIONS=$NEW_COLLECTIONS|" .env
        else
            echo "QDRANT_COLLECTIONS=$NEW_COLLECTIONS" >> .env
        fi
        echo "[*] Added 'conversations' to QDRANT_COLLECTIONS."
    fi
    # Set up cron
    MAINT_CRON="$INDEX_MIN $INDEX_HOUR * * * $SCRIPT_DIR/maintenance.sh >> $SCRIPT_DIR/logs/maintenance.log 2>&1"
    if ! crontab -l 2>/dev/null | grep -qF "maintenance.sh"; then
        (crontab -l 2>/dev/null; echo "$MAINT_CRON") | crontab -
        echo "[*] Conversation indexing scheduled at ${INDEX_HOUR}:$(printf '%02d' $INDEX_MIN)."
    else
        echo "[*] Maintenance cron already exists — skipping."
    fi
else
    echo "[*] Auto-indexing disabled. Run maintenance.sh manually any time."
fi

# --- install.sh ---

echo ""
echo "[*] Running install.sh..."
bash install.sh

# --- Done ---

echo ""
echo "=== Setup Complete ==="
echo ""
if [ -n "$ENV_TAG" ]; then
    TAG_FILTER="h-cli-${ENV_TAG}"
    CLAUDE_CONTAINER="h-cli-${ENV_TAG}-claude"
else
    TAG_FILTER="h-cli"
    CLAUDE_CONTAINER="h-cli-claude"
fi
echo "Next steps:"
echo "  1. Review .env:        vi $SCRIPT_DIR/.env"
echo "  2. Edit persona:       vi $SCRIPT_DIR/context.md"
echo "  3. Add SSH key:        ssh-copy-id -i $SCRIPT_DIR/ssh-keys/id_ed25519.pub user@host"
echo "  4. Start services:     docker compose up -d"
echo "  5. Authenticate:       docker exec -it $CLAUDE_CONTAINER claude"
echo "                         (interactive login, then Ctrl+C when done)"
echo "  6. Verify:             docker ps | grep $TAG_FILTER"
echo "  7. Tear down:          docker compose down"
echo ""
