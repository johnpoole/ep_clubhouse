"""
Configuration and logging for Yarbo Bridge.
"""

import os
import logging
from pathlib import Path

# Load .env file if present (before reading os.environ in CONFIG)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass  # python-dotenv not installed; rely on system env vars

# ─── Configuration ───────────────────────────────────────────────────────────

CONFIG = {
    "email": os.environ.get("YARBO_EMAIL", ""),
    "password": os.environ.get("YARBO_PASSWORD", ""),
    "bridge_port": int(os.environ.get("YARBO_BRIDGE_PORT", "8099")),
    "bridge_host": os.environ.get("YARBO_BRIDGE_HOST", "0.0.0.0"),

    # Auth0
    "auth0_domain": "dev-6ubfuqym1d3m0mq1.us.auth0.com",
    "auth0_client_id": "SL1GSNy3VmCLTML01qPkwqjgY4xm66i0",
    "auth0_audience": "https://auth0-jwt-authorizer",

    # Pre-loaded tokens from app backup (used as fallback / initial token)
    # These tokens were extracted from FlutterSharedPreferences.xml
    "initial_access_token": os.environ.get("YARBO_ACCESS_TOKEN", ""),
    "initial_refresh_token": os.environ.get("YARBO_REFRESH_TOKEN", ""),

    # Yarbo API
    "api_base": "https://4zx17x5q7l.execute-api.us-east-1.amazonaws.com/Stage",
    "mqtt_api_base": "https://26akbclmo9.execute-api.us-east-1.amazonaws.com/Stage",

    # MQTT — Cloud broker (for reference; auth not available)
    "mqtt_broker": "t9db1d91.us-east-1.emqx.cloud",
    "mqtt_port": 15525,

    # MQTT — Data Center local broker (no auth required!)
    # The Yarbo Data Center (docking station) runs EMQX on port 8883 (TLS).
    # It connects via ethernet. The robot connects to it over WiFi.
    # YARBO_ROBOT_IP should point to the data center, NOT the robot itself.
    "robot_ip": os.environ.get("YARBO_ROBOT_IP", "192.168.68.102"),
    "robot_mqtt_port": int(os.environ.get("YARBO_ROBOT_MQTT_PORT", "8883")),
    "robot_mqtt_tls": os.environ.get("YARBO_ROBOT_MQTT_TLS", "true").lower() == "true",
    "robot_serial": os.environ.get("YARBO_ROBOT_SERIAL", ""),

    # Cache TTL (seconds) - how often to re-fetch from cloud
    "cache_ttl_device": 300,      # 5 min
    "cache_ttl_map": 3600,        # 1 hour
    "cache_ttl_messages": 120,    # 2 min
    "cache_ttl_firmware": 3600,   # 1 hour
    "cache_ttl_status": 30,       # 30 sec (MQTT-fed)

    # Home Assistant integration (for calendar-based schedule blocking)
    "ha_url": os.environ.get("HA_URL", "http://homeassistant.local:8123"),
    "ha_token": os.environ.get("HA_TOKEN", ""),
    "ha_calendar_entity": os.environ.get("HA_CALENDAR_ENTITY", "calendar.epclubhouseyyc_gmail_com"),
    "calendar_block_enabled": os.environ.get("YARBO_CALENDAR_BLOCK", "true").lower() == "true",

    # SSL (HTTPS) — required for Agora WebCrypto in browser
    "ssl_certfile": os.environ.get("YARBO_SSL_CERT", "/opt/yarbo-bridge/bridge-cert.pem"),
    "ssl_keyfile": os.environ.get("YARBO_SSL_KEY", "/opt/yarbo-bridge/bridge-key.pem"),

    # MQTT Traffic Logging
    "mqtt_log_enabled": os.environ.get("YARBO_MQTT_LOG", "false").lower() == "true",
    "mqtt_log_file": os.environ.get("YARBO_MQTT_LOG_FILE", "/opt/yarbo-bridge/mqtt_traffic.log"),
}

# ─── Logging ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("yarbo-bridge")
