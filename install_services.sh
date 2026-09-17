#!/usr/bin/env bash
# ==============================================================================
# install_services.sh — Dynamic Zero-Hardcode Systemd Service Builder & Installer
# Standardizes WTB to match the CSB architecture with dynamic host auto-detection.
# ==============================================================================
set -e

# ── 1. Auto-Detect Environment & Host Paths (Zero Hardcoding) ─────────────────
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_USER="${SUDO_USER:-$(id -un)}"
TARGET_GROUP="$(id -gn "$TARGET_USER" 2>/dev/null || echo "$TARGET_USER")"

# Locate Python Virtual Environment
if [ -x "$APP_DIR/venv/bin/python3" ]; then
    PYTHON_BIN="$APP_DIR/venv/bin/python3"
elif [ -x "$APP_DIR/.venv/bin/python3" ]; then
    PYTHON_BIN="$APP_DIR/.venv/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
else
    echo "❌ Error: Could not locate python3 executable." >&2
    exit 1
fi

# Locate Ngrok Binary
if command -v ngrok >/dev/null 2>&1; then
    NGROK_BIN="$(command -v ngrok)"
elif [ -x "/snap/bin/ngrok" ]; then
    NGROK_BIN="/snap/bin/ngrok"
elif [ -x "/usr/local/bin/ngrok" ]; then
    NGROK_BIN="/usr/local/bin/ngrok"
elif [ -x "/usr/bin/ngrok" ]; then
    NGROK_BIN="/usr/bin/ngrok"
else
    NGROK_BIN="/snap/bin/ngrok"
fi

# Read WEB_PORT dynamically from .env or default to 8101
WEB_PORT=8101
if [ -f "$APP_DIR/.env" ]; then
    PARSED_PORT=$(grep -E '^WEB_PORT=' "$APP_DIR/.env" | head -n1 | cut -d'=' -f2 | tr -d ' "\r\n'\''')
    if [ -n "$PARSED_PORT" ]; then
        WEB_PORT="$PARSED_PORT"
    fi
fi

echo "=================================================================="
echo " 🐋 WTB Systemd Service Builder & Installer"
echo "=================================================================="
echo "  • Working Directory : $APP_DIR"
echo "  • Service User      : $TARGET_USER"
echo "  • Service Group     : $TARGET_GROUP"
echo "  • Python Binary     : $PYTHON_BIN"
echo "  • Ngrok Binary      : $NGROK_BIN"
echo "  • Web / Ngrok Port  : $WEB_PORT"
echo "=================================================================="

# ── 2. Verify Port Mapping in ngrok.yml ─────────────────────────────────────────
if [ -f "$APP_DIR/ngrok.yml" ]; then
    echo "🔍 Checking ngrok.yml port mapping..."
    if grep -q "addr:" "$APP_DIR/ngrok.yml"; then
        # Check if addr matches WEB_PORT
        if ! grep -q "addr:[[:space:]]*${WEB_PORT}" "$APP_DIR/ngrok.yml"; then
            echo "⚠️  Updating ngrok.yml addr to match WEB_PORT ($WEB_PORT)..."
            sed -i -E "s/(addr:[[:space:]]*)[0-9]+/\1${WEB_PORT}/" "$APP_DIR/ngrok.yml"
        fi
    fi
elif [ -f "$APP_DIR/ngrok.yml.example" ]; then
    echo "📋 Initializing ngrok.yml from template with port $WEB_PORT..."
    sed "s/8101/${WEB_PORT}/g" "$APP_DIR/ngrok.yml.example" > "$APP_DIR/ngrok.yml"
fi

# ── 3. Stop Running Services & Clean Up Legacy Units ──────────────────────────
echo "🛑 Stopping any existing WTB services to prevent duplicate processes..."
sudo systemctl stop wtb wtb-bot wtb-web wtb-ngrok wtb-tg wtb-ui 2>/dev/null || true

# Disable legacy names so systemd wants-links don't conflict
sudo systemctl disable wtb-tg wtb-ui 2>/dev/null || true
sudo rm -f /etc/systemd/system/wtb-tg.service /etc/systemd/system/wtb-ui.service

# ── 4. Build & Write Unit Files Dynamically ────────────────────────────────────
echo "🔨 Generating dynamic systemd service unit files..."

# 4a. wtb.service (Trading Engine)
cat <<EOF | sudo tee /etc/systemd/system/wtb.service >/dev/null
[Unit]
Description=wtb — Solana Whale Tracker LIVE Bot
After=network-online.target
Wants=network-online.target

[Service]
User=${TARGET_USER}
Group=${TARGET_GROUP}
WorkingDirectory=${APP_DIR}
ExecStart=${PYTHON_BIN} main.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=wtb
Environment=PYTHONUNBUFFERED=1
Environment=WEB_PORT=${WEB_PORT}

[Install]
WantedBy=multi-user.target
EOF

# 4b. wtb-bot.service (Telegram Control Bot)
cat <<EOF | sudo tee /etc/systemd/system/wtb-bot.service >/dev/null
[Unit]
Description=wtb — Solana Whale Tracker LIVE Telegram Control Bot
After=network-online.target
Wants=network-online.target

[Service]
User=${TARGET_USER}
Group=${TARGET_GROUP}
WorkingDirectory=${APP_DIR}
ExecStart=${PYTHON_BIN} telegram_bot.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=wtb-bot
Environment=PYTHONUNBUFFERED=1
Environment=WEB_PORT=${WEB_PORT}

[Install]
WantedBy=multi-user.target
EOF

# 4c. wtb-web.service (Web Dashboard)
cat <<EOF | sudo tee /etc/systemd/system/wtb-web.service >/dev/null
[Unit]
Description=wtb — Solana Whale Tracker Web Dashboard
After=network-online.target
Wants=network-online.target

[Service]
User=${TARGET_USER}
Group=${TARGET_GROUP}
WorkingDirectory=${APP_DIR}
ExecStart=${PYTHON_BIN} web_server.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=wtb-web
Environment=PYTHONUNBUFFERED=1
Environment=WEB_PORT=${WEB_PORT}

[Install]
WantedBy=multi-user.target
EOF

# 4d. wtb-ngrok.service (Ngrok Tunnel)
cat <<EOF | sudo tee /etc/systemd/system/wtb-ngrok.service >/dev/null
[Unit]
Description=wtb — Ngrok Tunnel (Zero-Conflict)
After=network.target wtb-web.service

[Service]
Type=simple
User=${TARGET_USER}
Group=${TARGET_GROUP}
WorkingDirectory=${APP_DIR}
ExecStart=${NGROK_BIN} start wtb_dashboard --config ${APP_DIR}/ngrok.yml --log=stdout
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=wtb-ngrok

[Install]
WantedBy=multi-user.target
EOF

# ── 5. Backward Compatibility Aliases ──────────────────────────────────────────
echo "🔗 Setting up compatibility symlinks for legacy service names..."
sudo ln -sf /etc/systemd/system/wtb-bot.service /etc/systemd/system/wtb-tg.service
sudo ln -sf /etc/systemd/system/wtb-web.service /etc/systemd/system/wtb-ui.service

# ── 6. Systemd Reload & Enable ────────────────────────────────────────────────
echo "🔄 Reloading systemd daemon..."
sudo systemctl daemon-reload

echo "⚡ Enabling services for auto-start on boot..."
sudo systemctl enable wtb wtb-bot wtb-web wtb-ngrok

echo ""
echo "=================================================================="
echo " ✅ SUCCESS! All services generated and installed dynamically."
echo "=================================================================="
echo "To start all services:"
echo "  sudo systemctl restart wtb wtb-bot wtb-web wtb-ngrok"
echo ""
echo "To check live status:"
echo "  sudo systemctl status wtb wtb-bot wtb-web wtb-ngrok"
echo "=================================================================="
