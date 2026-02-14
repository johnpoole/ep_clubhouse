#!/bin/bash
# =============================================================================
# Yarbo Bridge - Raspberry Pi First-Time Deploy Script
# =============================================================================
# Run this on the Pi after first SSH login:
#   curl -sSL https://raw.githubusercontent.com/johnpoole/Yarbo_emulator/main/pi/deploy.sh | bash
#
# Or copy this script to the Pi and run:
#   chmod +x deploy.sh && ./deploy.sh
# =============================================================================

set -euo pipefail

REPO_URL="https://github.com/johnpoole/ep_clubhouse.git"
INSTALL_DIR="/opt/yarbo-bridge"
SERVICE_NAME="yarbo-bridge"
PYTHON_MIN="3.9"

echo "============================================"
echo "  Yarbo Bridge - Raspberry Pi Deployment"
echo "============================================"

# --- Check Python version ---
if ! command -v python3 &>/dev/null; then
    echo "[!] Python3 not found. Installing..."
    sudo apt-get update && sudo apt-get install -y python3 python3-pip python3-venv git
fi

PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "[+] Python version: $PY_VER"

# --- Install system dependencies ---
echo "[+] Installing system packages..."
sudo apt-get update
sudo apt-get install -y git python3-venv python3-pip

# --- Clone or update repo ---
if [ -d "$INSTALL_DIR" ]; then
    echo "[+] Updating existing installation..."
    cd "$INSTALL_DIR"
    sudo git fetch --all
    sudo git reset --hard origin/main
else
    echo "[+] Cloning repository..."
    sudo git clone "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

# --- Create virtual environment ---
echo "[+] Setting up Python virtual environment..."
if [ ! -d "$INSTALL_DIR/.venv" ]; then
    sudo python3 -m venv "$INSTALL_DIR/.venv"
fi
sudo "$INSTALL_DIR/.venv/bin/pip" install --upgrade pip
sudo "$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

# --- Create .env if it doesn't exist ---
if [ ! -f "$INSTALL_DIR/.env" ]; then
    echo "[+] Creating .env file (you'll need to edit this)..."
    cat <<'ENV' | sudo tee "$INSTALL_DIR/.env" > /dev/null
# Yarbo Bridge Configuration
# Edit these values for your setup
YARBO_EMAIL=your_email@example.com
YARBO_PASSWORD=your_password
YARBO_ROBOT_IP=192.168.68.102
YARBO_BRIDGE_PORT=8099
YARBO_BRIDGE_HOST=0.0.0.0
ENV
    echo ""
    echo "  *** IMPORTANT: Edit /opt/yarbo-bridge/.env with your credentials ***"
    echo "  sudo nano /opt/yarbo-bridge/.env"
    echo ""
fi

# --- Install systemd service ---
echo "[+] Installing systemd service..."
sudo cp "$INSTALL_DIR/pi/yarbo-bridge.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"

# --- Install update script + timer ---
echo "[+] Installing auto-update timer..."
sudo cp "$INSTALL_DIR/pi/yarbo-bridge-update.service" /etc/systemd/system/
sudo cp "$INSTALL_DIR/pi/yarbo-bridge-update.timer" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable yarbo-bridge-update.timer
sudo systemctl start yarbo-bridge-update.timer

echo ""
echo "============================================"
echo "  Deployment complete!"
echo "============================================"
echo ""
echo "  Next steps:"
echo "  1. Edit credentials:  sudo nano /opt/yarbo-bridge/.env"
echo "  2. Start the bridge:  sudo systemctl start yarbo-bridge"
echo "  3. Check status:      sudo systemctl status yarbo-bridge"
echo "  4. View logs:         sudo journalctl -u yarbo-bridge -f"
echo ""
echo "  Auto-update: checks GitHub every 15 minutes."
echo "  Manual update: sudo systemctl start yarbo-bridge-update"
echo ""
echo "  Bridge URL: http://$(hostname -I | awk '{print $1}'):8099"
echo "============================================"
