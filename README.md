# EP Clubhouse

Deployment files for the Yarbo Bridge — a REST API bridge between Home Assistant and the Yarbo robot mower/snow blower.

Public deployment repo. Contains only the files needed to run the bridge on a Raspberry Pi.

## Quick Install (Raspberry Pi)

```bash
# Clone and deploy
sudo git clone https://github.com/johnpoole/yarbo-bridge-deploy.git /opt/yarbo-bridge
cd /opt/yarbo-bridge
sudo bash pi/deploy.sh
```

## Manual Install

```bash
sudo git clone https://github.com/johnpoole/yarbo-bridge-deploy.git /opt/yarbo-bridge
cd /opt/yarbo-bridge
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt

# Configure credentials
sudo nano .env
# YARBO_EMAIL=your_email
# YARBO_PASSWORD=your_password
# YARBO_ROBOT_IP=192.168.68.102
# YARBO_BRIDGE_PORT=8099
# YARBO_BRIDGE_HOST=0.0.0.0

# Install systemd service
sudo cp pi/yarbo-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable yarbo-bridge
sudo systemctl start yarbo-bridge
```

## Auto-Update

The bridge checks for updates from this repo every 15 minutes:

```bash
sudo cp pi/yarbo-bridge-update.service /etc/systemd/system/
sudo cp pi/yarbo-bridge-update.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable yarbo-bridge-update.timer
sudo systemctl start yarbo-bridge-update.timer
```

Manual update: `sudo systemctl start yarbo-bridge-update`

## Files

| File | Purpose |
|------|---------|
| `yarbo_bridge.py` | Main bridge server (FastAPI + MQTT) |
| `requirements.txt` | Python dependencies |
| `pi/deploy.sh` | First-time deployment script |
| `pi/update.sh` | Auto-update script (pulls from GitHub) |
| `pi/yarbo-bridge.service` | systemd service for the bridge |
| `pi/yarbo-bridge-update.service` | systemd oneshot for updates |
| `pi/yarbo-bridge-update.timer` | 15-minute update timer |
