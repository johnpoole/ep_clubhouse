#!/bin/bash
# =============================================================================
# Yarbo Bridge - Remote Update Script
# =============================================================================
# Pulls latest code from GitHub (public repo), reinstalls deps if changed,
# restarts service.
# Called by systemd timer (every 15 min) or manually:
#   sudo systemctl start yarbo-bridge-update
# =============================================================================

set -euo pipefail

INSTALL_DIR="/opt/yarbo-bridge"
SERVICE_NAME="yarbo-bridge"
LOG_TAG="yarbo-update"

log() { logger -t "$LOG_TAG" "$1"; echo "[$(date '+%H:%M:%S')] $1"; }

cd "$INSTALL_DIR"

# --- Check for updates ---
git fetch origin main --quiet 2>/dev/null || { log "WARN: git fetch failed (no network?)"; exit 0; }

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
    log "Up to date ($LOCAL)"
    exit 0
fi

log "Update available: $LOCAL -> $REMOTE"

# --- Preserve .env ---
if [ -f .env ]; then
    cp .env /tmp/yarbo-bridge-env-backup
fi

# --- Pull changes ---
git reset --hard origin/main
log "Code updated to $(git rev-parse --short HEAD)"

# --- Restore .env ---
if [ -f /tmp/yarbo-bridge-env-backup ]; then
    cp /tmp/yarbo-bridge-env-backup .env
    rm /tmp/yarbo-bridge-env-backup
fi

# --- Reinstall deps if requirements.txt changed ---
if git diff --name-only "$LOCAL" "$REMOTE" | grep -q "requirements.txt"; then
    log "requirements.txt changed — reinstalling dependencies..."
    .venv/bin/pip install -r requirements.txt --quiet
fi

# --- Restart service ---
log "Restarting $SERVICE_NAME..."
systemctl restart "$SERVICE_NAME"
log "Update complete. Service restarted."
