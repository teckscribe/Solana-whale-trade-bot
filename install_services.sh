#!/usr/bin/env bash
# ==============================================================================
# install_services.sh — WTB Systemd Services Installer
# Standardizes WTB to match the multi-process architecture used in CSB.
# ==============================================================================
set -e

echo "=== Installing WTB Systemd Services ==="

# 1. Copy unit files to /etc/systemd/system/
sudo cp wtb.service /etc/systemd/system/wtb.service
sudo cp wtb-bot.service /etc/systemd/system/wtb-bot.service
sudo cp wtb-web.service /etc/systemd/system/wtb-web.service
sudo cp wtb-ngrok.service /etc/systemd/system/wtb-ngrok.service

# 2. Backward compatibility symlinks for legacy service names
sudo ln -sf /etc/systemd/system/wtb-bot.service /etc/systemd/system/wtb-tg.service
sudo ln -sf /etc/systemd/system/wtb-web.service /etc/systemd/system/wtb-ui.service

# 3. Reload systemd daemon
sudo systemctl daemon-reload

# 4. Enable all services for automatic boot
sudo systemctl enable wtb wtb-bot wtb-web wtb-ngrok

echo "✅ All WTB services installed and enabled successfully!"
echo "To start/restart all services:"
echo "  sudo systemctl restart wtb wtb-bot wtb-web wtb-ngrok"
echo "To check status:"
echo "  sudo systemctl status wtb wtb-bot wtb-web wtb-ngrok"
