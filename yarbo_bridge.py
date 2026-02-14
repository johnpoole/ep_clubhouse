#!/usr/bin/env python3
"""
Yarbo REST API Bridge for Home Assistant
========================================
A local REST API that wraps the Yarbo cloud API, making it easy
to integrate with Home Assistant's REST sensor/binary_sensor platforms.

Usage:
    pip install fastapi uvicorn requests paho-mqtt
    python yarbo_bridge.py

Then configure HA sensors to poll http://localhost:8099/...

Environment variables (or edit config below):
    YARBO_EMAIL        - Your Yarbo account email
    YARBO_PASSWORD     - Your Yarbo account password
    YARBO_BRIDGE_PORT  - Port to run on (default 8099)
"""

import os
import sys
import json
import time
import logging
import threading
import ssl
import zlib
import gzip
import io
import uuid
from datetime import datetime, timezone
from typing import Optional
from pathlib import Path

# Load .env file if present (before reading os.environ in CONFIG)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass  # python-dotenv not installed; rely on system env vars

import requests as http_requests
import math
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse, Response
import uvicorn

try:
    import paho.mqtt.client as paho_mqtt
except ImportError:
    paho_mqtt = None

# ─── Configuration ───────────────────────────────────────────────────────────

CONFIG = {
    "email": os.environ.get("YARBO_EMAIL", "jdpoole@gmail.com"),
    "password": os.environ.get("YARBO_PASSWORD", "finn1234"),
    "bridge_port": int(os.environ.get("YARBO_BRIDGE_PORT", "8099")),
    "bridge_host": os.environ.get("YARBO_BRIDGE_HOST", "0.0.0.0"),

    # Auth0
    "auth0_domain": "dev-6ubfuqym1d3m0mq1.us.auth0.com",
    "auth0_client_id": "SL1GSNy3VmCLTML01qPkwqjgY4xm66i0",
    "auth0_audience": "https://auth0-jwt-authorizer",

    # Pre-loaded tokens from app backup (used as fallback / initial token)
    # These tokens were extracted from FlutterSharedPreferences.xml
    "initial_access_token": os.environ.get("YARBO_ACCESS_TOKEN", ""),
    "initial_refresh_token": os.environ.get("YARBO_REFRESH_TOKEN",
        "v1.McqVkIx5sr-UbZ6zM5I2aSbsmIrHZSYQhf52PEtG9BVSTpOjUXsbd1egDmGgiQZWG5c2FY2YcwFBUQaXIDiNtZ0"),

    # Yarbo API
    "api_base": "https://4zx17x5q7l.execute-api.us-east-1.amazonaws.com/Stage",
    "mqtt_api_base": "https://26akbclmo9.execute-api.us-east-1.amazonaws.com/Stage",

    # MQTT — Cloud broker (for reference; auth not available)
    "mqtt_broker": "t9db1d91.us-east-1.emqx.cloud",
    "mqtt_port": 15525,

    # MQTT — Robot local broker (no auth required!)
    # The robot runs its own EMQX broker on port 8883 (TLS) or 1883 (plain).
    # Connect robot to WiFi first, then set YARBO_ROBOT_IP to its local IP.
    "robot_ip": os.environ.get("YARBO_ROBOT_IP", "192.168.68.102"),
    "robot_mqtt_port": int(os.environ.get("YARBO_ROBOT_MQTT_PORT", "8883")),
    "robot_mqtt_tls": os.environ.get("YARBO_ROBOT_MQTT_TLS", "true").lower() == "true",
    "robot_serial": os.environ.get("YARBO_ROBOT_SERIAL", "25210102S63YI872"),

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
}

# ─── Logging ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("yarbo-bridge")

# ─── Token Manager ───────────────────────────────────────────────────────────

class TokenManager:
    """Handles Auth0 token lifecycle — login, refresh, and pre-loaded tokens."""

    def __init__(self):
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.expires_at: float = 0
        self._lock = threading.Lock()

        # Load initial tokens if available
        if CONFIG.get("initial_access_token"):
            self.access_token = CONFIG["initial_access_token"]
            self.expires_at = time.time() + 86400  # assume valid for 1 day
            log.info("Loaded initial access token from config")
        if CONFIG.get("initial_refresh_token"):
            self.refresh_token = CONFIG["initial_refresh_token"]
            log.info("Loaded refresh token from config")

    def _login(self):
        """Authenticate via Auth0 Resource Owner Password Grant."""
        log.info("Authenticating with Auth0 (password grant)...")
        url = f"https://{CONFIG['auth0_domain']}/oauth/token"
        payload = {
            "grant_type": "password",
            "client_id": CONFIG["auth0_client_id"],
            "audience": CONFIG["auth0_audience"],
            "scope": "openid profile offline_access",
            "username": CONFIG["email"],
            "password": CONFIG["password"],
        }
        r = http_requests.post(url, json=payload, timeout=15)
        if r.status_code != 200:
            log.error(f"Auth0 password login failed: {r.status_code} {r.text[:200]}")
            raise RuntimeError(f"Auth0 login failed: {r.status_code}")

        data = r.json()
        self.access_token = data["access_token"]
        self.refresh_token = data.get("refresh_token", self.refresh_token)
        self.expires_at = time.time() + data.get("expires_in", 86400) - 300
        log.info(f"Auth0 login successful, token expires in {data.get('expires_in', '?')}s")

    def _refresh(self):
        """Refresh the access token using the refresh token."""
        if not self.refresh_token:
            return self._login()

        log.info("Refreshing Auth0 token...")
        url = f"https://{CONFIG['auth0_domain']}/oauth/token"
        payload = {
            "grant_type": "refresh_token",
            "client_id": CONFIG["auth0_client_id"],
            "refresh_token": self.refresh_token,
        }
        r = http_requests.post(url, json=payload, timeout=15)
        if r.status_code != 200:
            log.warning(f"Token refresh failed ({r.status_code}), trying password login")
            try:
                return self._login()
            except RuntimeError:
                log.error("Both refresh and password login failed!")
                raise

        data = r.json()
        self.access_token = data["access_token"]
        if "refresh_token" in data:
            self.refresh_token = data["refresh_token"]
        self.expires_at = time.time() + data.get("expires_in", 86400) - 300
        log.info("Token refreshed successfully")

    def get_token(self) -> str:
        """Get a valid access token, refreshing if needed."""
        with self._lock:
            if not self.access_token or time.time() >= self.expires_at:
                if self.refresh_token:
                    self._refresh()
                else:
                    self._login()
            return self.access_token

    def get_headers(self) -> dict:
        """Get HTTP headers with valid auth token."""
        return {
            "Authorization": f"Bearer {self.get_token()}",
            "Content-Type": "application/json",
        }


tokens = TokenManager()

# ─── Cache ───────────────────────────────────────────────────────────────────

class Cache:
    """Simple TTL cache for API responses."""

    def __init__(self):
        self._store: dict = {}
        self._lock = threading.Lock()

    def get(self, key: str, ttl: int) -> Optional[dict]:
        with self._lock:
            entry = self._store.get(key)
            if entry and (time.time() - entry["ts"]) < ttl:
                return entry["data"]
            return None

    def set(self, key: str, data: dict):
        with self._lock:
            self._store[key] = {"data": data, "ts": time.time()}

    def invalidate(self, key: str = None):
        with self._lock:
            if key:
                self._store.pop(key, None)
            else:
                self._store.clear()


cache = Cache()

# ─── Yarbo API Client ────────────────────────────────────────────────────────

class YarboAPI:
    """Wrapper around Yarbo's cloud REST API."""

    BASE = CONFIG["api_base"]
    MQTT_BASE = CONFIG["mqtt_api_base"]

    def _get(self, path: str, base: str = None) -> dict:
        url = (base or self.BASE) + path
        r = http_requests.get(url, headers=tokens.get_headers(), timeout=15)
        if r.status_code == 401:
            # Token expired mid-flight, force refresh and retry
            tokens.access_token = None
            r = http_requests.get(url, headers=tokens.get_headers(), timeout=15)
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail=r.text[:300])
        data = r.json()
        if data.get("code") != "00000":
            raise HTTPException(status_code=502, detail=data.get("message", "API error"))
        return data["data"]

    def _post(self, path: str, body: dict, base: str = None) -> dict:
        url = (base or self.BASE) + path
        r = http_requests.post(url, headers=tokens.get_headers(), json=body, timeout=15)
        if r.status_code == 401:
            tokens.access_token = None
            r = http_requests.post(url, headers=tokens.get_headers(), json=body, timeout=15)
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail=r.text[:300])
        data = r.json()
        if data.get("code") != "00000":
            raise HTTPException(status_code=502, detail=data.get("message", "API error"))
        return data["data"]

    # ── Device ──

    def get_devices(self) -> list:
        cached = cache.get("devices", CONFIG["cache_ttl_device"])
        if cached is not None:
            return cached
        data = self._get("/yarbo/robot-service/commonUser/userRobotBind/getUserRobotBindVos")
        result = data.get("deviceList", [])
        cache.set("devices", result)
        return result

    def get_user_info(self) -> dict:
        cached = cache.get("user_info", CONFIG["cache_ttl_device"])
        if cached is not None:
            return cached
        data = self._get("/yarbo/robot-service/robot/commonUser/getUesrInfo")
        cache.set("user_info", data)
        return data

    # ── Map ──

    def get_map(self, sn: str) -> dict:
        cached = cache.get(f"map_{sn}", CONFIG["cache_ttl_map"])
        if cached is not None:
            return cached
        data = self._get(f"/yarbo/commonUser/getUploadMap?sn={sn}")
        maps = data.get("mapList", [])
        if not maps:
            return {}
        map_json = json.loads(maps[0].get("mapJson", "{}"))
        cache.set(f"map_{sn}", map_json)
        return map_json

    def get_raster_background(self, sn: str) -> dict:
        cached = cache.get(f"raster_{sn}", CONFIG["cache_ttl_map"])
        if cached is not None:
            return cached
        data = self._get(f"/yarbo/robot/rasterBackground/get?sn={sn}")
        cache.set(f"raster_{sn}", data)
        return data

    # ── Messages ──

    def get_messages(self, sn: str) -> list:
        cached = cache.get(f"messages_{sn}", CONFIG["cache_ttl_messages"])
        if cached is not None:
            return cached
        data = self._get(f"/yarbo/msg/userDeviceMsg?sn={sn}")
        msgs = []
        for dev_msg in data.get("deviceMsg", []):
            for msg in dev_msg.get("msgs", []):
                msgs.append(msg)
        cache.set(f"messages_{sn}", msgs)
        return msgs

    # ── Firmware ──

    def get_firmware(self) -> dict:
        cached = cache.get("firmware", CONFIG["cache_ttl_firmware"])
        if cached is not None:
            return cached
        data = self._get("/yarbo/commonUser/getLatestPubVersion")
        cache.set("firmware", data)
        return data

    def get_dc_version(self, sn: str) -> dict:
        cached = cache.get(f"dc_version_{sn}", CONFIG["cache_ttl_firmware"])
        if cached is not None:
            return cached
        data = self._post("/yarbo/robot/getDcVersion", {"sn": sn})
        cache.set(f"dc_version_{sn}", data)
        return data

    # ── Notifications ──

    def get_notification_settings(self) -> dict:
        return self._get("/yarbo/msg/getNotificationSetting")

    # ── Shared users ──

    def get_shared_users(self, sn: str) -> list:
        data = self._get(f"/yarbo/robot-service/commonUser/userWhiteList/getUserWhiteList?sn={sn}")
        return data.get("userWhiteLists", [])

    # ── MQTT migration ──

    def get_mqtt_status(self, sn: str) -> dict:
        return self._get(f"/yarbo/mqtt-migration/query?sn={sn}", base=self.MQTT_BASE)


api = YarboAPI()

# ─── MQTT Real-time Client ───────────────────────────────────────────────────

class YarboMQTTClient:
    """
    Connects to the Yarbo robot's LOCAL MQTT broker for real-time
    telemetry and command control.

    The robot runs its own EMQX broker:
      - Port 8883 (TLS with self-signed cert) or 1883 (no TLS)
      - No authentication required (anonymous access)
      - Robot must be on same LAN (connect it to WiFi first)

    Topic structure:
      Send commands  → snowbot/{SN}/app/{command}
      Receive data   ← snowbot/{SN}/device/data_feedback
      Heartbeat      ← snowbot/{SN}/device/heart_beat
      Replies        ← snowbot/{SN}/app/+/reply
      Acks           ← snowbot/{SN}/app/+/ack
      Messages       ← snowbot/{SN}/msg/#

    Payload can be zlib-compressed (\\x78\\x01) or gzip (\\x1f\\x8b).
    Commands are sent as plain JSON; robot accepts both.
    """

    def __init__(self, robot_ip: str, serial: str,
                 port: int = 8883, use_tls: bool = True):
        self.robot_ip = robot_ip
        self.serial = serial
        self.port = port
        self.use_tls = use_tls
        self._client = None
        self._connected = False
        self._lock = threading.Lock()
        self._command_responses: dict = {}   # req_id → response
        self._response_events: dict = {}     # req_id → threading.Event

        # ── telemetry store ──
        self._status: dict = {
            "connected": False,
            "last_heartbeat": None,
            "battery": None,
            "battery_level": None,
            "state": "unknown",
            "state_code": None,
            "running_status": {},
            "electric_info": {},
            "rtk_status": {},
            "motor_info": {},
            "position": {},
            "body_info": {},
            "hub_info": {},
            "ultrasonic": {},
            "velocity": {},
            "net_status": {},
            "vision_info": {},
            "mower_head_info": {},
            "led_info": {},
            "odom_info": {},
            "system_info": {},
            "last_data_feedback": None,
        }

        # ── command-response stores ──
        self._live_map: Optional[dict] = None
        self._live_plans = None
        self._live_gps_ref: Optional[dict] = None
        self._live_schedules = None
        self._live_global_params: Optional[dict] = None
        self._device_msg: Optional[dict] = None

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self):
        """Connect to robot's local MQTT broker in a background thread."""
        if paho_mqtt is None:
            log.error("paho-mqtt not installed; MQTT features disabled")
            return
        if not self.robot_ip:
            log.warning(
                "No robot IP configured — MQTT client not started. "
                "Set YARBO_ROBOT_IP to the robot's WiFi IP address."
            )
            return

        cid = "yarbo-bridge-" + uuid.uuid4().hex[:8]
        self._client = paho_mqtt.Client(
            client_id=cid,
            protocol=paho_mqtt.MQTTv311,
            callback_api_version=paho_mqtt.CallbackAPIVersion.VERSION2,
        )
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

        if self.use_tls:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            self._client.tls_set_context(ctx)

        tls_label = "TLS" if self.use_tls else "plain"
        log.info("Connecting to robot MQTT at %s:%d (%s)", self.robot_ip, self.port, tls_label)
        try:
            self._client.connect_async(self.robot_ip, self.port, keepalive=60)
            self._client.loop_start()
        except Exception as e:
            log.error("MQTT connect failed: %s", e)

    def stop(self):
        """Disconnect from robot."""
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()
            self._connected = False
            with self._lock:
                self._status["connected"] = False

    # ── callbacks ────────────────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            log.info("Connected to robot MQTT broker at %s", self.robot_ip)
            self._connected = True
            with self._lock:
                self._status["connected"] = True

            sn = self.serial
            topics = [
                (f"snowbot/{sn}/device/data_feedback", 0),
                (f"snowbot/{sn}/device/heart_beat", 0),
                (f"snowbot/{sn}/app/+/reply", 0),
                (f"snowbot/{sn}/app/+/ack", 0),
                (f"snowbot/{sn}/msg/#", 0),
            ]
            client.subscribe(topics)
            log.info("Subscribed to %d topic patterns for SN %s", len(topics), sn)

            # Request full state on connect
            self._request_initial_state()
        else:
            log.error("MQTT connection refused: rc=%s", reason_code)
            self._connected = False

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        log.warning("Disconnected from robot MQTT (rc=%s), will auto-reconnect", reason_code)
        self._connected = False
        with self._lock:
            self._status["connected"] = False

    # ── message handling ─────────────────────────────────────────────────

    @staticmethod
    def _decompress(payload: bytes) -> bytes:
        """Decompress zlib or gzip payload; return as-is if uncompressed."""
        try:
            if payload[:2] == b"\x1f\x8b":
                with gzip.GzipFile(fileobj=io.BytesIO(payload)) as f:
                    return f.read()
            else:
                return zlib.decompress(payload)
        except Exception:
            return payload

    def _on_message(self, client, userdata, msg):
        try:
            raw = self._decompress(msg.payload)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                log.debug("Non-JSON on %s: %s", msg.topic, raw[:100])
                return

            tp = msg.topic

            if "heart_beat" in tp:
                with self._lock:
                    self._status["connected"] = True
                    self._status["last_heartbeat"] = datetime.now(timezone.utc).isoformat()
                return

            if "data_feedback" in tp:
                self._handle_data_feedback(data)
                return

            if "/reply" in tp or "/ack" in tp:
                self._handle_command_reply(data)
                return

            log.debug("MQTT other: %s → %s", tp, json.dumps(data)[:200])

        except Exception as e:
            log.error("Error processing MQTT message: %s", e)

    def _handle_data_feedback(self, data: dict):
        """Route data_feedback messages by their internal 'topic' field."""
        topic = data.get("topic", "")
        payload = data.get("data", data)
        req_id = data.get("req_id")

        with self._lock:
            self._status["last_data_feedback"] = datetime.now(timezone.utc).isoformat()

            # ── telemetry ──
            if topic == "batteryInfo":
                self._status["battery"] = payload
                if isinstance(payload, dict):
                    self._status["battery_level"] = payload.get(
                        "level", payload.get("battery_level"))
            elif topic == "runningStatus":
                self._status["running_status"] = payload
                sc = payload.get("state", payload.get("robot_state"))
                if sc is not None:
                    self._status["state_code"] = sc
                    self._status["state"] = self._decode_state(sc)
            elif topic == "electricInfo":
                self._status["electric_info"] = payload
            elif topic == "rtkMSG":
                self._status["rtk_status"] = payload
            elif topic == "motorInfo":
                self._status["motor_info"] = payload
            elif topic == "stateInfo":
                if isinstance(payload, dict):
                    self._status.update(payload)
            elif topic == "bodyInfoMSG":
                self._status["body_info"] = payload
            elif topic == "hubInfoMsg":
                self._status["hub_info"] = payload
            elif topic == "ultrasonicMsg":
                self._status["ultrasonic"] = payload
            elif topic == "velocityShow":
                self._status["velocity"] = payload
            elif topic == "netStatusInfo":
                self._status["net_status"] = payload
            elif topic == "visionInfo":
                self._status["vision_info"] = payload
            elif topic == "mowerHeadInfo":
                self._status["mower_head_info"] = payload
            elif topic == "ledInfoMsg":
                self._status["led_info"] = payload
            elif topic == "odomInfo":
                self._status["odom_info"] = payload
            elif topic == "SystemInfoFeedback":
                self._status["system_info"] = payload

            # ── command responses ──
            elif topic == "get_map":
                self._live_map = payload
            elif topic == "read_all_plan":
                self._live_plans = payload
            elif topic == "read_gps_ref":
                self._live_gps_ref = payload
            elif topic == "read_schedules":
                self._live_schedules = payload
            elif topic == "read_global_params":
                self._live_global_params = payload
            elif topic == "get_device_msg":
                self._device_msg = payload
            else:
                self._status[topic] = payload

        # Signal waiting callers
        if req_id and req_id in self._response_events:
            self._command_responses[req_id] = data
            self._response_events[req_id].set()

    def _handle_command_reply(self, data: dict):
        req_id = data.get("req_id")
        if req_id and req_id in self._response_events:
            self._command_responses[req_id] = data
            self._response_events[req_id].set()

    # ── initial state request ────────────────────────────────────────────

    def _request_initial_state(self):
        """Ask the robot for a full state dump right after connecting."""
        for cmd in ["get_device_msg", "get_map", "read_all_plan",
                     "read_gps_ref", "read_global_params", "read_schedules"]:
            try:
                self.send_command(cmd, {}, wait=False)
                time.sleep(0.15)
            except Exception as e:
                log.warning("Initial %s request failed: %s", cmd, e)

    # ── public API ───────────────────────────────────────────────────────

    def send_command(self, command: str, payload: dict = None,
                     wait: bool = True, timeout: float = 5.0) -> Optional[dict]:
        """
        Send a command to the robot.

        Args:
            command:  e.g. 'get_map', 'stop', 'start_plan', 'cmd_vel'
            payload:  JSON payload dict (default {})
            wait:     block until a response arrives?
            timeout:  seconds to wait for response

        Returns:
            Response data dict if wait=True and response arrives, else None.
        """
        if not self._client or not self._connected:
            raise ConnectionError("Not connected to robot MQTT")

        if payload is None:
            payload = {}

        topic = f"snowbot/{self.serial}/app/{command}"

        req_id = None
        if wait:
            req_id = uuid.uuid4().hex[:12]
            payload["req_id"] = req_id
            self._response_events[req_id] = threading.Event()

        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        result = self._client.publish(topic, raw, qos=0)
        if result.rc != 0:
            if req_id:
                self._response_events.pop(req_id, None)
            raise RuntimeError("MQTT publish failed: rc=%d" % result.rc)

        log.info("Sent command: %s → %s", command, topic)

        if wait and req_id:
            self._response_events[req_id].wait(timeout=timeout)
            self._response_events.pop(req_id, None)
            return self._command_responses.pop(req_id, None)
        return None

    # ── backward-compatible helpers (used by endpoints) ──────────────────

    def update(self, topic: str, payload: dict):
        """Manual update (backward compat with old /api/status/update)."""
        with self._lock:
            if topic == "heart_beat":
                self._status["connected"] = True
                self._status["last_heartbeat"] = datetime.now(timezone.utc).isoformat()
            elif topic == "batteryInfo":
                self._status["battery"] = payload
            elif topic == "runningStatus":
                self._status["running_status"] = payload
                sc = payload.get("state", payload.get("robot_state"))
                if sc is not None:
                    self._status["state"] = self._decode_state(sc)
            elif topic == "electricInfo":
                self._status["electric_info"] = payload
            elif topic == "rtkMSG":
                self._status["rtk_status"] = payload
            elif topic == "motorInfo":
                self._status["motor_info"] = payload
            else:
                self._status[topic] = payload

    def get(self) -> dict:
        """Return current status dict (backward compat)."""
        with self._lock:
            return dict(self._status)

    @property
    def is_connected(self) -> bool:
        return self._connected

    @staticmethod
    def _decode_state(code) -> str:
        states = {
            0: "idle", 1: "working", 2: "paused", 3: "charging",
            4: "error", 5: "docking", 6: "returning",
        }
        return states.get(code, "unknown_%s" % code)


def _init_mqtt_client() -> YarboMQTTClient:
    """Create the MQTT client, auto-detecting serial from cloud API if needed."""
    robot_ip = CONFIG["robot_ip"]
    serial = CONFIG["robot_serial"]
    port = CONFIG["robot_mqtt_port"]
    use_tls = CONFIG["robot_mqtt_tls"]

    # Auto-detect serial from cloud API if not configured
    if not serial:
        try:
            devices = api.get_devices()
            if devices:
                serial = devices[0]["serialNum"]
                log.info("Auto-detected robot serial: %s", serial)
        except Exception:
            serial = ""

    return YarboMQTTClient(robot_ip, serial, port, use_tls)


mqtt_client = None  # Initialized in __main__

# Placeholder until main() runs; endpoints check for None
def _get_mqtt() -> YarboMQTTClient:
    if mqtt_client is None:
        raise HTTPException(status_code=503, detail="MQTT client not initialized")
    return mqtt_client

# ─── FastAPI App ─────────────────────────────────────────────────────────────

app = FastAPI(
    title="Yarbo Bridge for Home Assistant",
    description="Local REST API bridge to Yarbo cloud services",
    version="1.0.0",
)


def _get_default_sn() -> str:
    """Get the first device serial number."""
    devices = api.get_devices()
    if not devices:
        raise HTTPException(status_code=404, detail="No devices found")
    return devices[0]["serialNum"]


# ── Health ──

@app.get("/")
def root():
    """Health check endpoint."""
    return {"status": "ok", "service": "yarbo-bridge", "version": "1.0.0"}


# ── Devices ──

@app.get("/api/devices")
def get_devices():
    """List all bound Yarbo devices."""
    return api.get_devices()


@app.get("/api/device")
def get_device():
    """Get primary device info (HA-friendly flat structure)."""
    devices = api.get_devices()
    if not devices:
        raise HTTPException(status_code=404, detail="No devices found")
    d = devices[0]
    head_types = {0: "mower", 1: "snow_blower", 2: "blower", 3: "trimmer"}
    return {
        "serial_number": d["serialNum"],
        "name": d.get("deviceNickname", "Yarbo"),
        "head_type": head_types.get(d.get("headType", -1), "unknown"),
        "head_type_id": d.get("headType"),
        "master": d.get("masterUsername"),
        "created": d.get("gmtCreate"),
        "is_master": d.get("master") == 1,
    }


# ── User ──

@app.get("/api/user")
def get_user():
    """Get user profile info."""
    return api.get_user_info()


# ── Map & Areas ──

@app.get("/api/map")
def get_map(sn: str = None):
    """Get full map data including areas, pathways, charging points, zones."""
    sn = sn or _get_default_sn()
    return api.get_map(sn)


@app.get("/api/areas")
def get_areas(sn: str = None):
    """Get just the areas (job zones) with boundary coordinates."""
    sn = sn or _get_default_sn()
    map_data = api.get_map(sn)
    areas = map_data.get("area", [])
    result = []
    for a in areas:
        result.append({
            "id": a["id"],
            "name": a["name"],
            "area_sq_meters": round(a.get("area", 0), 1),
            "boundary_points": len(a.get("range", [])),
            "boundary": a.get("range", []),
            "gps_ref": a.get("ref", {}),
            "snow_piles": len(a.get("snowPiles", [])),
            "trimming_edges": len(a.get("trimming_edges", [])),
        })
    return result


@app.get("/api/map/summary")
def get_map_summary(sn: str = None):
    """HA-friendly summary of map stats."""
    sn = sn or _get_default_sn()
    map_data = api.get_map(sn)
    areas = map_data.get("area", [])
    ref = map_data.get("ref", {}).get("ref", {})
    cp = map_data.get("chargingPoint", {})
    cp_pos = cp.get("chargingPoint", {})
    dc = map_data.get("dc", {})

    return {
        "map_id": map_data.get("id"),
        "map_name": map_data.get("name"),
        "total_areas": len(areas),
        "total_area_sq_meters": round(sum(a.get("area", 0) for a in areas), 1),
        "total_pathways": len(map_data.get("pathway", [])),
        "total_nogo_zones": len(map_data.get("nogozone", [])),
        "total_novision_zones": len(map_data.get("novisionzone", [])),
        "gps_latitude": ref.get("latitude"),
        "gps_longitude": ref.get("longitude"),
        "rtk_height_m": map_data.get("ref", {}).get("hgt"),
        "charging_station_x": cp_pos.get("x"),
        "charging_station_y": cp_pos.get("y"),
        "docking_station_name": dc.get("dc_name"),
        "docking_station_mac": dc.get("dc_mac"),
    }


def _local_to_gps(x: float, y: float, ref_lat: float, ref_lon: float) -> tuple:
    """Convert local x/y (meters) to GPS lat/lon.

    The Yarbo coordinate system uses:
      x = west (+) / east (-)   (mirrored from standard)
      y = north (+) / south (-)
    relative to the GPS reference point.
    """
    lat = ref_lat + y / 111320.0
    lon = ref_lon - x / (111320.0 * math.cos(math.radians(ref_lat)))
    return lat, lon


def _get_map_geometry(sn: str):
    """Extract areas, pathways, charging points as GPS polygons."""
    map_data = api.get_map(sn)
    ref = map_data.get("ref", {}).get("ref", {})
    ref_lat = ref.get("latitude", 0)
    ref_lon = ref.get("longitude", 0)

    areas_geo = []
    for area in map_data.get("area", []):
        pts = area.get("range", [])
        gps_pts = [_local_to_gps(p["x"], p["y"], ref_lat, ref_lon) for p in pts]
        areas_geo.append({
            "name": area.get("name", "Area"),
            "area_sqm": area.get("area", 0),
            "points": gps_pts,
            "local_points": [(p["x"], p["y"]) for p in pts],
        })

    pathways_geo = []
    for pw in map_data.get("pathway", []):
        pts = pw.get("range", [])
        gps_pts = [_local_to_gps(p["x"], p["y"], ref_lat, ref_lon) for p in pts]
        pathways_geo.append({
            "name": pw.get("name", "Pathway"),
            "points": gps_pts,
            "local_points": [(p["x"], p["y"]) for p in pts],
        })

    nogo_geo = []
    for nz in map_data.get("nogozone", []):
        pts = nz.get("range", [])
        gps_pts = [_local_to_gps(p["x"], p["y"], ref_lat, ref_lon) for p in pts]
        nogo_geo.append({"name": "No-Go Zone", "points": gps_pts})

    chargers = []
    for cp in map_data.get("chargingPoints", []):
        pt = cp.get("chargingPoint", {})
        lat, lon = _local_to_gps(pt.get("x", 0), pt.get("y", 0), ref_lat, ref_lon)
        chargers.append({"lat": lat, "lon": lon, "enabled": cp.get("enable", False)})

    # Snow pile zones (within each area)
    snow_piles_geo = []
    for area in map_data.get("area", []):
        for sp in area.get("snowPiles", []):
            pts = sp.get("range", [])
            gps_pts = [_local_to_gps(p["x"], p["y"], ref_lat, ref_lon) for p in pts]
            snow_piles_geo.append({
                "name": "Snow Pile Zone",
                "points": gps_pts,
                "local_points": [(p["x"], p["y"]) for p in pts],
            })

    # Raster background image
    raster = None
    try:
        bg = api.get_raster_background(sn)
        obj = json.loads(bg.get("object_data", "{}"))
        tl = obj.get("top_left_real", {})
        br = obj.get("bottom_right_real", {})
        if tl and br:
            tl_lat, tl_lon = _local_to_gps(tl.get("x", 0), tl.get("y", 0), ref_lat, ref_lon)
            br_lat, br_lon = _local_to_gps(br.get("x", 0), br.get("y", 0), ref_lat, ref_lon)
            raster = {
                "image_url": bg.get("accessUrl", ""),
                "bounds": [[tl_lat, tl_lon], [br_lat, br_lon]],
                "rotation_rad": obj.get("rad", 0),
            }
    except Exception:
        pass

    return {
        "ref_lat": ref_lat, "ref_lon": ref_lon,
        "areas": areas_geo, "pathways": pathways_geo,
        "nogo": nogo_geo, "chargers": chargers,
        "snow_piles": snow_piles_geo,
        "raster": raster,
        "raw": map_data,
    }


def _get_mqtt_map_geometry(mqtt_map_data: dict) -> dict:
    """Parse MQTT get_map response into GPS geometry for Leaflet rendering.

    The MQTT map uses slightly different keys than the cloud map:
      - 'areas' not 'area'
      - 'allchargingData' not 'chargingPoints'
      - 'nogozones' not 'nogozone'
      - 'pathways' not 'pathway'
      - Each area/pathway has its own 'ref' with lat/lon
    """
    # Use first area's ref as the global reference
    areas = mqtt_map_data.get("areas", [])
    ref_lat, ref_lon = 0, 0
    if areas and "ref" in areas[0]:
        ref_lat = areas[0]["ref"].get("latitude", 0)
        ref_lon = areas[0]["ref"].get("longitude", 0)

    areas_geo = []
    snow_piles_geo = []
    for area in areas:
        pts = area.get("range", [])
        a_ref = area.get("ref", {})
        a_ref_lat = a_ref.get("latitude", ref_lat)
        a_ref_lon = a_ref.get("longitude", ref_lon)
        gps_pts = [_local_to_gps(p["x"], p["y"], a_ref_lat, a_ref_lon) for p in pts]
        areas_geo.append({
            "id": area.get("id"),
            "name": area.get("name", "Area"),
            "area_sqm": area.get("area", 0),
            "points": gps_pts,
            "local_points": [(p["x"], p["y"]) for p in pts],
        })
        for sp in area.get("snowPiles", []):
            sp_pts = sp.get("range", [])
            sp_ref = sp.get("ref", a_ref)
            sp_ref_lat = sp_ref.get("latitude", a_ref_lat)
            sp_ref_lon = sp_ref.get("longitude", a_ref_lon)
            sp_gps = [_local_to_gps(p["x"], p["y"], sp_ref_lat, sp_ref_lon) for p in sp_pts]
            snow_piles_geo.append({
                "name": "Snow Pile Zone",
                "points": sp_gps,
                "local_points": [(p["x"], p["y"]) for p in sp_pts],
            })

    pathways_geo = []
    for pw in mqtt_map_data.get("pathways", []):
        pts = pw.get("range", [])
        pw_ref = pw.get("ref", {})
        pw_ref_lat = pw_ref.get("latitude", ref_lat)
        pw_ref_lon = pw_ref.get("longitude", ref_lon)
        gps_pts = [_local_to_gps(p["x"], p["y"], pw_ref_lat, pw_ref_lon) for p in pts]
        pathways_geo.append({
            "name": pw.get("name", "Pathway"),
            "points": gps_pts,
            "local_points": [(p["x"], p["y"]) for p in pts],
        })

    nogo_geo = []
    for nz in mqtt_map_data.get("nogozones", []):
        pts = nz.get("range", [])
        nz_ref = nz.get("ref", {})
        nz_ref_lat = nz_ref.get("latitude", ref_lat)
        nz_ref_lon = nz_ref.get("longitude", ref_lon)
        gps_pts = [_local_to_gps(p["x"], p["y"], nz_ref_lat, nz_ref_lon) for p in pts]
        nogo_geo.append({
            "name": nz.get("name", "No-Go Zone"),
            "points": gps_pts,
            "enabled": nz.get("enable", True),
        })

    chargers = []
    for cp in mqtt_map_data.get("allchargingData", []):
        pt = cp.get("chargingPoint", {})
        lat, lon = _local_to_gps(pt.get("x", 0), pt.get("y", 0), ref_lat, ref_lon)
        chargers.append({
            "lat": lat, "lon": lon,
            "enabled": cp.get("enable", False),
            "name": cp.get("name", ""),
        })

    sidewalks_geo = []
    for sw in mqtt_map_data.get("sidewalks", []):
        pts = sw.get("range", [])
        sw_ref = sw.get("ref", {})
        sw_ref_lat = sw_ref.get("latitude", ref_lat)
        sw_ref_lon = sw_ref.get("longitude", ref_lon)
        gps_pts = [_local_to_gps(p["x"], p["y"], sw_ref_lat, sw_ref_lon) for p in pts]
        sidewalks_geo.append({
            "name": sw.get("name", "Sidewalk"),
            "points": gps_pts,
        })

    elec_fence = []
    for ef in mqtt_map_data.get("elec_fence", []):
        pts = ef.get("range", [])
        ef_ref = ef.get("ref", {})
        ef_ref_lat = ef_ref.get("latitude", ref_lat)
        ef_ref_lon = ef_ref.get("longitude", ref_lon)
        gps_pts = [_local_to_gps(p["x"], p["y"], ef_ref_lat, ef_ref_lon) for p in pts]
        elec_fence.append({
            "name": "Electric Fence",
            "points": gps_pts,
        })

    return {
        "ref_lat": ref_lat, "ref_lon": ref_lon,
        "areas": areas_geo, "pathways": pathways_geo,
        "nogo": nogo_geo, "chargers": chargers,
        "snow_piles": snow_piles_geo,
        "sidewalks": sidewalks_geo,
        "elec_fence": elec_fence,
        "raster": None,  # No raster in MQTT data
    }


def _load_mqtt_map() -> Optional[dict]:
    """Load MQTT map data from the live bridge cache or the saved response file."""
    # Try live cached map from the MQTT client
    if mqtt_client and mqtt_client._live_map:
        return mqtt_client._live_map

    # Fall back to saved response file
    responses_file = Path(__file__).parent / "mqtt" / "responses" / "get_map.json"
    if responses_file.exists():
        with open(responses_file) as f:
            data = json.load(f)
        resp = data.get("response", {})
        return resp.get("data", resp)

    return None


@app.get("/api/map/mqtt", response_class=HTMLResponse)
def get_mqtt_map_view():
    """Interactive Leaflet map rendered from MQTT get_map data (no cloud API needed).

    Uses the robot's local MQTT map response instead of the cloud API.
    Falls back to mqtt/responses/get_map.json if MQTT is not connected.

    Embed in HA with an iframe card:
      type: iframe
      url: http://BRIDGE_IP:8099/api/map/mqtt
      aspect_ratio: "16:9"
    """
    mqtt_map = _load_mqtt_map()
    if not mqtt_map:
        raise HTTPException(404, "No MQTT map data available. Connect to robot MQTT or place get_map.json in mqtt/responses/")

    geo = _get_mqtt_map_geometry(mqtt_map)

    def _esc(s):
        """Escape a string for safe embedding in JS double-quoted strings."""
        return str(s).replace('\\', '\\\\').replace('"', '\\"').replace("'", "\\'")

    # Build Leaflet polygon JS
    area_polygons_js = []
    colors = ["#4fc3f7", "#81c784", "#ffb74d", "#ba68c8", "#ef5350", "#26c6da"]
    for i, area in enumerate(geo["areas"]):
        color = colors[i % len(colors)]
        coords = json.dumps([[lat, lon] for lat, lon in area["points"]])
        sqm = round(area["area_sqm"])
        label = _esc(f'{area["name"]} ({sqm} m\u00b2)')
        area_polygons_js.append(
            f'L.polygon({coords}, {{color:"{color}",weight:2,fillOpacity:0.25}})'
            f'.addTo(areasLayer).bindPopup("{label}");'
        )

    pathway_lines_js = []
    for pw in geo["pathways"]:
        coords = json.dumps([[lat, lon] for lat, lon in pw["points"]])
        pw_name = _esc(pw["name"])
        pathway_lines_js.append(
            f'L.polyline({coords}, {{color:"#ffd54f",weight:3,dashArray:"8,4"}})'
            f'.addTo(pathwaysLayer).bindPopup("{pw_name}");'
        )

    nogo_js = []
    for nz in geo["nogo"]:
        coords = json.dumps([[lat, lon] for lat, lon in nz["points"]])
        nz_name = _esc(nz.get("name", "No-Go Zone"))
        nz_color = "#ef5350" if nz.get("enabled", True) else "#999"
        nogo_js.append(
            f'L.polygon({coords}, {{color:"{nz_color}",weight:2,fillOpacity:0.3}})'
            f'.addTo(nogoLayer).bindPopup("{nz_name}");'
        )

    snow_js = []
    for sp in geo.get("snow_piles", []):
        coords = json.dumps([[lat, lon] for lat, lon in sp["points"]])
        snow_js.append(
            f'L.polygon({coords}, {{color:"#90caf9",weight:1.5,dashArray:"6,3",fillOpacity:0.15}})'
            f'.addTo(snowLayer).bindPopup("Snow Pile Zone");'
        )

    charger_js = []
    for cp in geo["chargers"]:
        cp_icon = "Active" if cp["enabled"] else "Inactive"
        cp_color = "green" if cp["enabled"] else "gray"
        cp_name = _esc(cp.get("name", ""))
        label = _esc(f"Charging: {cp_name} ({cp_icon})") if cp_name else _esc(f"Charging ({cp_icon})")
        charger_js.append(
            f'L.circleMarker([{cp["lat"]},{cp["lon"]}], {{radius:8,color:"{cp_color}",fillColor:"{cp_color}",fillOpacity:0.8}})'
            f'.addTo(chargersLayer).bindPopup("{label}");'
        )

    sidewalk_js = []
    for sw in geo.get("sidewalks", []):
        coords = json.dumps([[lat, lon] for lat, lon in sw["points"]])
        sw_name = _esc(sw["name"])
        sidewalk_js.append(
            f'L.polyline({coords}, {{color:"#b0bec5",weight:4,opacity:0.7}})'
            f'.addTo(sidewalksLayer).bindPopup("{sw_name}");'
        )

    fence_js = []
    for ef in geo.get("elec_fence", []):
        coords = json.dumps([[lat, lon] for lat, lon in ef["points"]])
        fence_js.append(
            f'L.polyline({coords}, {{color:"#ff9800",weight:2,dashArray:"4,4"}})'
            f'.addTo(fenceLayer).bindPopup("Electric Fence");'
        )

    ref_marker = f'L.marker([{geo["ref_lat"]},{geo["ref_lon"]}]).addTo(markersLayer).bindPopup("GPS Reference Point");'

    all_js = "\n".join(area_polygons_js + pathway_lines_js + nogo_js + snow_js + charger_js + sidewalk_js + fence_js + [ref_marker])

    # Collect all GPS points for fitBounds
    all_points = []
    for a in geo["areas"]:
        all_points.extend(a["points"])
    for sp in geo.get("snow_piles", []):
        all_points.extend(sp["points"])
    for nz in geo["nogo"]:
        all_points.extend(nz["points"])

    source_label = "Live MQTT" if (mqtt_client and mqtt_client._live_map) else "Cached (get_map.json)"

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Yarbo MQTT Map</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <style>
    * {{ margin:0; padding:0; }}
    html, body, #map {{ width:100%; height:100%; }}
    .source-badge {{
      position: fixed; top: 10px; right: 60px; z-index: 1000;
      background: rgba(0,0,0,0.7); color: #4fc3f7; padding: 6px 14px;
      border-radius: 6px; font: 13px sans-serif;
    }}
  </style>
</head>
<body>
  <div id="map"></div>
  <div class="source-badge">Source: {source_label}</div>
  <script>
    var osmLayer = L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
      maxZoom: 22, attribution: '&copy; OpenStreetMap'
    }});
    var esriSat = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}', {{
      maxZoom: 22, attribution: '&copy; Esri'
    }});

    var map = L.map('map', {{ layers: [osmLayer] }}).setView([{geo["ref_lat"]}, {geo["ref_lon"]}], 17);

    var areasLayer = L.layerGroup().addTo(map);
    var pathwaysLayer = L.layerGroup().addTo(map);
    var nogoLayer = L.layerGroup().addTo(map);
    var snowLayer = L.layerGroup().addTo(map);
    var chargersLayer = L.layerGroup().addTo(map);
    var sidewalksLayer = L.layerGroup().addTo(map);
    var fenceLayer = L.layerGroup().addTo(map);
    var markersLayer = L.layerGroup().addTo(map);

    {all_js}

    var baseLayers = {{
      "OpenStreetMap": osmLayer,
      "Esri Satellite": esriSat
    }};
    var overlays = {{
      "Areas": areasLayer,
      "Pathways": pathwaysLayer,
      "No-Go Zones": nogoLayer,
      "Snow Piles": snowLayer,
      "Chargers": chargersLayer,
      "Sidewalks": sidewalksLayer,
      "Electric Fence": fenceLayer,
      "Reference Point": markersLayer
    }};
    L.control.layers(baseLayers, overlays).addTo(map);

    var allPoints = {json.dumps([[lat, lon] for lat, lon in all_points])};
    if (allPoints.length > 0) {{
      map.fitBounds(allPoints, {{padding: [30,30]}});
    }}
  </script>
</body>
</html>"""
    return HTMLResponse(content=html)


@app.get("/api/map/svg")
def get_map_svg(sn: str = None, width: int = 800, height: int = 600):
    """Render the property map as an SVG image.

    Use in HA with a Generic Camera or Picture Entity card.
    """
    sn = sn or _get_default_sn()
    geo = _get_map_geometry(sn)

    # Collect all local points for bounding box
    all_pts = []
    for a in geo["areas"]:
        all_pts.extend(a["local_points"])
    for p in geo["pathways"]:
        all_pts.extend(p["local_points"])
    for sp in geo.get("snow_piles", []):
        all_pts.extend(sp["local_points"])

    if not all_pts:
        return Response(content="<svg xmlns='http://www.w3.org/2000/svg'/>",
                        media_type="image/svg+xml")

    xs = [p[0] for p in all_pts]
    ys = [p[1] for p in all_pts]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    pad = 3  # meters padding
    min_x -= pad; max_x += pad; min_y -= pad; max_y += pad
    range_x = max_x - min_x or 1
    range_y = max_y - min_y or 1

    # Scale to fit SVG viewport
    scale = min(width / range_x, height / range_y)

    def tx(x, y):
        sx = (max_x - x) * scale  # mirror X: Yarbo +x = west
        sy = height - (y - min_y) * scale  # flip Y
        return f"{sx:.1f},{sy:.1f}"

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}"'
             f' viewBox="0 0 {width} {height}" style="background:#1a1a2e">']

    # Grid lines
    parts.append('<g stroke="#2a2a4a" stroke-width="0.5" opacity="0.5">')
    grid_step = 10  # meters
    gx = min_x - (min_x % grid_step)
    while gx <= max_x:
        sx = (gx - min_x) * scale
        parts.append(f'<line x1="{sx:.1f}" y1="0" x2="{sx:.1f}" y2="{height}"/>')
        gx += grid_step
    gy = min_y - (min_y % grid_step)
    while gy <= max_y:
        sy = height - (gy - min_y) * scale
        parts.append(f'<line x1="0" y1="{sy:.1f}" x2="{width}" y2="{sy:.1f}"/>')
        gy += grid_step
    parts.append('</g>')

    # Areas
    colors = ["#4fc3f7", "#81c784", "#ffb74d", "#ba68c8"]
    for i, area in enumerate(geo["areas"]):
        color = colors[i % len(colors)]
        pts_str = " ".join(tx(x, y) for x, y in area["local_points"])
        parts.append(f'<polygon points="{pts_str}" fill="{color}" fill-opacity="0.25"'
                     f' stroke="{color}" stroke-width="2"/>')
        # Label
        cx = sum(p[0] for p in area["local_points"]) / len(area["local_points"])
        cy = sum(p[1] for p in area["local_points"]) / len(area["local_points"])
        lx, ly = tx(cx, cy).split(",")
        sqm = round(area["area_sqm"])
        parts.append(f'<text x="{lx}" y="{ly}" fill="white" font-family="sans-serif"'
                     f' font-size="14" text-anchor="middle">{area["name"]} ({sqm} m\u00b2)</text>')

    # Pathways
    for pw in geo["pathways"]:
        pts_str = " ".join(tx(x, y) for x, y in pw["local_points"])
        parts.append(f'<polyline points="{pts_str}" fill="none"'
                     f' stroke="#ffd54f" stroke-width="3" stroke-dasharray="8,4"/>')
        if pw["local_points"]:
            lx, ly = tx(*pw["local_points"][0]).split(",")
            parts.append(f'<text x="{lx}" y="{float(ly)-8:.1f}" fill="#ffd54f"'
                         f' font-family="sans-serif" font-size="11">{pw["name"]}</text>')

    # Snow pile zones
    for sp in geo.get("snow_piles", []):
        pts_str = " ".join(tx(x, y) for x, y in sp["local_points"])
        parts.append(f'<polygon points="{pts_str}" fill="#90caf9" fill-opacity="0.15"'
                     f' stroke="#90caf9" stroke-width="1.5" stroke-dasharray="6,3"/>')

    # Charging stations
    for cp in geo["raw"].get("chargingPoints", []):
        pt = cp.get("chargingPoint", {})
        cx, cy = tx(pt.get("x", 0), pt.get("y", 0)).split(",")
        color = "#4caf50" if cp.get("enable") else "#757575"
        parts.append(f'<circle cx="{cx}" cy="{cy}" r="6" fill="{color}" stroke="white" stroke-width="1.5"/>')
        zap_label = "\u26a1 Active" if cp.get("enable") else "\u26a1"
        parts.append(f'<text x="{cx}" y="{float(cy)-10:.1f}" fill="{color}"'
                     f' font-family="sans-serif" font-size="10" text-anchor="middle">'
                     f'{zap_label}</text>')

    # Origin marker (docking station reference)
    ox, oy = tx(0, 0).split(",")
    parts.append(f'<circle cx="{ox}" cy="{oy}" r="4" fill="#ef5350" stroke="white" stroke-width="1"/>')
    parts.append(f'<text x="{ox}" y="{float(oy)-8:.1f}" fill="#ef5350"'
                 f' font-family="sans-serif" font-size="10" text-anchor="middle">REF</text>')

    # Scale bar
    bar_m = 10
    bar_px = bar_m * scale
    parts.append(f'<line x1="15" y1="{height-15}" x2="{15+bar_px:.1f}" y2="{height-15}"'
                 f' stroke="white" stroke-width="2"/>')
    parts.append(f'<text x="{15+bar_px/2:.1f}" y="{height-20}" fill="white"'
                 f' font-family="sans-serif" font-size="11" text-anchor="middle">{bar_m}m</text>')

    parts.append('</svg>')
    return Response(content="\n".join(parts), media_type="image/svg+xml")


def _build_raster_overlay_js(geo: dict) -> str:
    """Generate JS code to overlay the raster background image on Leaflet."""
    raster = geo.get("raster")
    if not raster or not raster.get("image_url"):
        return "var rasterOverlay = null; // No raster background available"
    bounds = raster["bounds"]
    img_url = raster["image_url"]
    # Leaflet imageOverlay expects [[south,west],[north,east]]
    lats = [bounds[0][0], bounds[1][0]]
    lons = [bounds[0][1], bounds[1][1]]
    sw = [min(lats), min(lons)]
    ne = [max(lats), max(lons)]
    return (
        f"var rasterOverlay = L.imageOverlay('{img_url}', "
        f"[{json.dumps(sw)}, {json.dumps(ne)}], "
        f"{{opacity: 0.7}}).addTo(map);"
    )


@app.get("/api/map/view", response_class=HTMLResponse)
def get_map_view(sn: str = None):
    """Interactive map with area boundaries overlaid on OpenStreetMap.

    Embed in HA with an iframe card:
      type: iframe
      url: http://BRIDGE_IP:8099/api/map/view
      aspect_ratio: "16:9"
    """
    sn = sn or _get_default_sn()
    geo = _get_map_geometry(sn)

    # Build Leaflet polygon data
    area_polygons_js = []
    for i, area in enumerate(geo["areas"]):
        coords = json.dumps([[lat, lon] for lat, lon in area["points"]])
        label = f"{area['name']} ({round(area['area_sqm'])} m\u00b2)"
        area_polygons_js.append(f'L.polygon({coords}, {{color:"#4fc3f7",weight:2,fillOpacity:0.2}}).addTo(areasLayer).bindPopup("{label}");')

    pathway_lines_js = []
    for pw in geo["pathways"]:
        coords = json.dumps([[lat, lon] for lat, lon in pw["points"]])
        pw_name = pw['name']
        pathway_lines_js.append(f'L.polyline({coords}, {{color:"#ffd54f",weight:3,dashArray:"8,4"}}).addTo(pathwaysLayer).bindPopup("{pw_name}");')

    nogo_js = []
    for nz in geo["nogo"]:
        coords = json.dumps([[lat, lon] for lat, lon in nz["points"]])
        nogo_js.append(f'L.polygon({coords}, {{color:"#ef5350",weight:2,fillOpacity:0.3}}).addTo(nogoLayer).bindPopup("No-Go Zone");')

    charger_js = []
    for cp in geo["chargers"]:
        cp_icon = "Active" if cp["enabled"] else "Inactive"
        cp_color = "green" if cp["enabled"] else "gray"
        cp_lat = cp['lat']
        cp_lon = cp['lon']
        charger_js.append(
            f'L.circleMarker([{cp_lat},{cp_lon}], {{radius:8,color:"{cp_color}",fillColor:"{cp_color}",fillOpacity:0.8}})'
            f'.addTo(chargersLayer).bindPopup("Charging Station ({cp_icon})");'
        )

    # Snow pile zones
    snow_js = []
    for sp in geo.get("snow_piles", []):
        coords = json.dumps([[lat, lon] for lat, lon in sp["points"]])
        snow_js.append(f'L.polygon({coords}, {{color:"#90caf9",weight:1.5,dashArray:"6,3",fillOpacity:0.15}}).addTo(snowLayer).bindPopup("Snow Pile Zone");')

    # Reference point marker
    ref_marker = f'L.marker([{geo["ref_lat"]},{geo["ref_lon"]}]).addTo(markersLayer).bindPopup("GPS Reference Point");'

    all_js = "\n".join(area_polygons_js + pathway_lines_js + nogo_js + snow_js + charger_js + [ref_marker])

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Yarbo Property Map</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <style>
    * {{ margin:0; padding:0; }}
    html, body, #map {{ width:100%; height:100%; }}
  </style>
</head>
<body>
  <div id="map"></div>
  <script>
    // Base tile layers
    var osmLayer = L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
      maxZoom: 22,
      attribution: '&copy; OpenStreetMap'
    }});
    var esriSat = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}', {{
      maxZoom: 22,
      attribution: '&copy; Esri'
    }});

    var map = L.map('map', {{ layers: [osmLayer] }}).setView([{geo["ref_lat"]}, {geo["ref_lon"]}], 17);

    // Feature overlay layer groups
    var areasLayer = L.layerGroup().addTo(map);
    var pathwaysLayer = L.layerGroup().addTo(map);
    var nogoLayer = L.layerGroup().addTo(map);
    var snowLayer = L.layerGroup().addTo(map);
    var chargersLayer = L.layerGroup().addTo(map);
    var markersLayer = L.layerGroup().addTo(map);

    // Raster background overlay (satellite image from Yarbo)
    {_build_raster_overlay_js(geo)}

    {all_js}

    // Layer control
    var baseLayers = {{
      "OpenStreetMap": osmLayer,
      "Esri Satellite": esriSat
    }};
    var overlays = {{}};
    if (rasterOverlay) overlays["Yarbo Satellite"] = rasterOverlay;
    overlays["Areas"] = areasLayer;
    overlays["Pathways"] = pathwaysLayer;
    overlays["No-Go Zones"] = nogoLayer;
    overlays["Snow Piles"] = snowLayer;
    overlays["Chargers"] = chargersLayer;
    overlays["Reference Point"] = markersLayer;
    L.control.layers(baseLayers, overlays).addTo(map);

    // Fit bounds to all features
    var allPoints = {json.dumps([[lat,lon] for a in geo['areas'] for lat,lon in a['points']] + [[lat,lon] for sp in geo.get('snow_piles',[]) for lat,lon in sp['points']])};
    if (allPoints.length > 0) {{
      map.fitBounds(allPoints, {{padding: [30,30]}});
    }}
  </script>
</body>
</html>"""
    return HTMLResponse(content=html)


@app.get("/api/dashboard", response_class=HTMLResponse)
def get_dashboard(sn: str = None):
    """Full Yarbo dashboard with interactive map, area start buttons, and robot controls.

    Embed in HA with an iframe card:
      type: iframe
      url: http://BRIDGE_IP:8099/api/dashboard
      aspect_ratio: "16:9"
    """
    sn = sn or _get_default_sn()
    geo = _get_map_geometry(sn)
    bridge_origin = ""  # JS will use relative URLs

    # Build Leaflet polygon data with area IDs for start buttons
    area_polygons_js = []
    area_list_html = []
    colors = ["#4fc3f7", "#81c784", "#ffb74d", "#ba68c8", "#ef5350", "#26c6da"]
    for i, area in enumerate(geo["areas"]):
        color = colors[i % len(colors)]
        coords = json.dumps([[lat, lon] for lat, lon in area["points"]])
        sqm = round(area["area_sqm"])
        label = f'{area["name"]} ({sqm} m²)'
        area_id = i + 1  # Yarbo area IDs are 1-based
        area_polygons_js.append(
            f'L.polygon({coords}, {{color:"{color}",weight:2,fillOpacity:0.25}})'
            f'.addTo(areasLayer).bindPopup(`<b>{area["name"]}</b><br>{sqm} m²<br>'
            f'<button onclick="startJob({area_id})" style="margin-top:6px;padding:4px 12px;'
            f'background:#4caf50;color:#fff;border:none;border-radius:4px;cursor:pointer">'
            f'▶ Start Mow</button>`);'
        )
        area_list_html.append(
            f'<div class="area-item" style="border-left:3px solid {color}">'
            f'  <div class="area-info">'
            f'    <span class="area-name">{area["name"]}</span>'
            f'    <span class="area-size">{sqm} m²</span>'
            f'  </div>'
            f'  <button class="btn btn-start" onclick="startJob({area_id})" title="Start mowing {area["name"]}">'
            f'    ▶'
            f'  </button>'
            f'</div>'
        )

    pathway_lines_js = []
    for pw in geo["pathways"]:
        coords = json.dumps([[lat, lon] for lat, lon in pw["points"]])
        pw_name = pw["name"]
        pathway_lines_js.append(
            f'L.polyline({coords}, {{color:"#ffd54f",weight:3,dashArray:"8,4"}})'
            f'.addTo(pathwaysLayer).bindPopup("{pw_name}");'
        )

    nogo_js = []
    for nz in geo["nogo"]:
        coords = json.dumps([[lat, lon] for lat, lon in nz["points"]])
        nogo_js.append(
            f'L.polygon({coords}, {{color:"#ef5350",weight:2,fillOpacity:0.3}})'
            f'.addTo(nogoLayer).bindPopup("No-Go Zone");'
        )

    charger_js = []
    for cp in geo["chargers"]:
        cp_color = "green" if cp["enabled"] else "gray"
        cp_label = "Active" if cp["enabled"] else "Inactive"
        charger_js.append(
            f'L.circleMarker([{cp["lat"]},{cp["lon"]}], '
            f'{{radius:8,color:"{cp_color}",fillColor:"{cp_color}",fillOpacity:0.8}})'
            f'.addTo(chargersLayer).bindPopup("Charging Station ({cp_label})");'
        )

    snow_js = []
    for sp in geo.get("snow_piles", []):
        coords = json.dumps([[lat, lon] for lat, lon in sp["points"]])
        snow_js.append(
            f'L.polygon({coords}, {{color:"#90caf9",weight:1.5,dashArray:"6,3",fillOpacity:0.15}})'
            f'.addTo(snowLayer).bindPopup("Snow Pile Zone");'
        )

    ref_marker = f'L.marker([{geo["ref_lat"]},{geo["ref_lon"]}]).addTo(markersLayer).bindPopup("GPS Reference Point");'
    all_map_js = "\n".join(area_polygons_js + pathway_lines_js + nogo_js + snow_js + charger_js + [ref_marker])
    areas_html = "\n".join(area_list_html) if area_list_html else '<div class="empty">No areas defined</div>'

    all_pts_json = json.dumps(
        [[lat, lon] for a in geo["areas"] for lat, lon in a["points"]]
        + [[lat, lon] for sp in geo.get("snow_piles", []) for lat, lon in sp["points"]]
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Yarbo Dashboard</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <style>
    * {{ margin:0; padding:0; box-sizing:border-box; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
           background: #1a1a2e; color: #e0e0e0; display: flex; height: 100vh; overflow: hidden; }}

    /* Sidebar */
    .sidebar {{ width: 280px; min-width: 280px; background: #16213e; display: flex;
                flex-direction: column; border-right: 1px solid #0f3460; overflow-y: auto; }}
    .sidebar h2 {{ padding: 14px 16px 8px; font-size: 15px; color: #4fc3f7; text-transform: uppercase;
                   letter-spacing: 1px; border-bottom: 1px solid #0f3460; }}
    .sidebar h3 {{ padding: 10px 16px 4px; font-size: 12px; color: #888; text-transform: uppercase;
                   letter-spacing: 1px; }}

    /* Area items */
    .area-item {{ display: flex; align-items: center; justify-content: space-between;
                  padding: 8px 12px 8px 16px; margin: 4px 8px; border-radius: 6px;
                  background: #1a1a2e; }}
    .area-info {{ display: flex; flex-direction: column; }}
    .area-name {{ font-size: 14px; font-weight: 500; }}
    .area-size {{ font-size: 11px; color: #888; }}

    /* Buttons */
    .btn {{ border: none; border-radius: 6px; cursor: pointer; font-size: 14px;
            padding: 6px 12px; transition: all 0.2s; }}
    .btn-start {{ background: #2e7d32; color: #fff; font-size: 16px; padding: 6px 10px; }}
    .btn-start:hover {{ background: #4caf50; }}
    .btn-stop {{ background: #c62828; color: #fff; }}
    .btn-stop:hover {{ background: #ef5350; }}
    .btn-pause {{ background: #e65100; color: #fff; }}
    .btn-pause:hover {{ background: #ff9800; }}
    .btn-resume {{ background: #1565c0; color: #fff; }}
    .btn-resume:hover {{ background: #42a5f5; }}
    .btn-dock {{ background: #6a1b9a; color: #fff; }}
    .btn-dock:hover {{ background: #ab47bc; }}

    .controls {{ padding: 8px 12px; display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }}
    .controls .btn {{ padding: 10px 8px; font-size: 13px; }}
    .controls .btn-stop {{ grid-column: 1 / -1; }}

    /* Status bar */
    .status-bar {{ padding: 10px 16px; border-top: 1px solid #0f3460; background: #0f3460; margin-top: auto; }}
    .status-item {{ display: flex; justify-content: space-between; font-size: 12px; padding: 2px 0; }}
    .status-label {{ color: #888; }}
    .status-value {{ color: #4fc3f7; font-weight: 500; }}
    .status-value.busy {{ color: #ef5350; }}
    .status-value.ok {{ color: #4caf50; }}

    /* Toast */
    .toast {{ position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%);
              background: #333; color: #fff; padding: 10px 24px; border-radius: 8px;
              font-size: 14px; z-index: 9999; display: none; box-shadow: 0 4px 12px rgba(0,0,0,0.4); }}
    .toast.error {{ background: #c62828; }}
    .toast.success {{ background: #2e7d32; }}

    /* Map */
    #map {{ flex: 1; }}

    /* Responsive */
    @media (max-width: 700px) {{
      body {{ flex-direction: column; }}
      .sidebar {{ width: 100%; min-width: unset; max-height: 40vh; flex-direction: column; }}
      #map {{ height: 60vh; }}
    }}
  </style>
</head>
<body>
  <div class="sidebar">
    <h2>🤖 Yarbo Dashboard</h2>

    <h3>Areas</h3>
    {areas_html}

    <h3>Robot Controls</h3>
    <div class="controls">
      <button class="btn btn-stop" onclick="robotCmd('stop')">⏹ Emergency Stop</button>
      <button class="btn btn-pause" onclick="robotCmd('pause')">⏸ Pause</button>
      <button class="btn btn-resume" onclick="robotCmd('resume')">▶ Resume</button>
      <button class="btn btn-dock" onclick="robotCmd('dock')">🏠 Dock</button>
      <button class="btn btn-resume" onclick="robotCmd('undock')">↗ Undock</button>
    </div>

    <div class="status-bar" id="statusBar">
      <div class="status-item">
        <span class="status-label">Robot</span>
        <span class="status-value" id="st-robot">—</span>
      </div>
      <div class="status-item">
        <span class="status-label">Battery</span>
        <span class="status-value" id="st-battery">—</span>
      </div>
      <div class="status-item">
        <span class="status-label">Calendar</span>
        <span class="status-value" id="st-calendar">—</span>
      </div>
      <div class="status-item">
        <span class="status-label">MQTT</span>
        <span class="status-value" id="st-mqtt">—</span>
      </div>
    </div>
  </div>

  <div id="map"></div>
  <div class="toast" id="toast"></div>

  <script>
    // ── Map setup ──
    var osmLayer = L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
      maxZoom: 22, attribution: '&copy; OpenStreetMap'
    }});
    var esriSat = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}', {{
      maxZoom: 22, attribution: '&copy; Esri'
    }});
    var map = L.map('map', {{ layers: [esriSat] }}).setView([{geo["ref_lat"]}, {geo["ref_lon"]}], 17);

    var areasLayer = L.layerGroup().addTo(map);
    var pathwaysLayer = L.layerGroup().addTo(map);
    var nogoLayer = L.layerGroup().addTo(map);
    var snowLayer = L.layerGroup().addTo(map);
    var chargersLayer = L.layerGroup().addTo(map);
    var markersLayer = L.layerGroup().addTo(map);

    {_build_raster_overlay_js(geo)}
    {all_map_js}

    L.control.layers(
      {{"OpenStreetMap": osmLayer, "Satellite": esriSat}},
      {{"Areas": areasLayer, "Pathways": pathwaysLayer, "No-Go": nogoLayer,
       "Snow Piles": snowLayer, "Chargers": chargersLayer, "Ref Point": markersLayer}}
    ).addTo(map);

    var allPoints = {all_pts_json};
    if (allPoints.length > 0) map.fitBounds(allPoints, {{padding: [30,30]}});

    // ── Robot marker (updated via status polling) ──
    var robotMarker = null;

    // ── Toast notifications ──
    function showToast(msg, type) {{
      var t = document.getElementById('toast');
      t.textContent = msg;
      t.className = 'toast ' + (type || '');
      t.style.display = 'block';
      setTimeout(function() {{ t.style.display = 'none'; }}, 3500);
    }}

    // ── API helpers ──
    function apiPost(path, params) {{
      var url = path;
      if (params) {{
        var qs = Object.entries(params).map(function(e){{ return e[0]+'='+e[1]; }}).join('&');
        url += '?' + qs;
      }}
      return fetch(url, {{method:'POST'}}).then(function(r){{ return r.json(); }});
    }}
    function apiGet(path) {{
      return fetch(path).then(function(r){{ return r.json(); }});
    }}

    // ── Start job for an area ──
    function startJob(areaId) {{
      showToast('Starting job for Area ' + areaId + '...', '');
      apiPost('/api/robot/start_plan', {{plan_id: areaId, percent: 0}})
        .then(function(d) {{
          if (d.ok) {{
            showToast('✓ Job started for Area ' + areaId, 'success');
          }} else if (d.blocked) {{
            showToast('⛔ Blocked: ' + d.reason, 'error');
          }} else {{
            showToast('⚠ ' + JSON.stringify(d), 'error');
          }}
        }})
        .catch(function(e) {{ showToast('Error: ' + e.message, 'error'); }});
    }}

    // ── Robot commands ──
    function robotCmd(cmd) {{
      showToast('Sending ' + cmd + '...', '');
      apiPost('/api/robot/' + cmd)
        .then(function(d) {{
          if (d.ok) showToast('✓ ' + cmd + ' sent', 'success');
          else showToast('⚠ ' + JSON.stringify(d), 'error');
        }})
        .catch(function(e) {{ showToast('Error: ' + e.message, 'error'); }});
    }}

    // ── Status polling ──
    function updateStatus() {{
      // Robot status
      apiGet('/api/status').then(function(d) {{
        var el = document.getElementById('st-robot');
        el.textContent = d.state || d.status || '—';
        el.className = 'status-value' + (d.state === 'working' ? '' : ' ok');

        var bat = document.getElementById('st-battery');
        if (d.battery_percent !== undefined) {{
          bat.textContent = d.battery_percent + '%';
          bat.className = 'status-value' + (d.battery_percent < 20 ? ' busy' : ' ok');
        }}

        // MQTT
        var mqtt = document.getElementById('st-mqtt');
        mqtt.textContent = d.mqtt_connected ? 'Connected' : 'Disconnected';
        mqtt.className = 'status-value' + (d.mqtt_connected ? ' ok' : ' busy');

        // Robot position on map
        if (d.latitude && d.longitude) {{
          if (!robotMarker) {{
            robotMarker = L.circleMarker([d.latitude, d.longitude], {{
              radius: 8, color: '#ff5722', fillColor: '#ff5722', fillOpacity: 0.9, weight: 2
            }}).addTo(map).bindPopup('Yarbo Robot');
          }} else {{
            robotMarker.setLatLng([d.latitude, d.longitude]);
          }}
        }}
      }}).catch(function(){{}});

      // Calendar status
      apiGet('/api/calendar/status').then(function(d) {{
        var el = document.getElementById('st-calendar');
        if (d.busy) {{
          el.textContent = '🔴 ' + (d.event_summary || 'Busy');
          el.className = 'status-value busy';
        }} else {{
          el.textContent = '🟢 Free';
          el.className = 'status-value ok';
        }}
      }}).catch(function() {{
        document.getElementById('st-calendar').textContent = '—';
      }});
    }}

    // Poll every 10 seconds
    updateStatus();
    setInterval(updateStatus, 10000);
  </script>
</body>
</html>"""
    return HTMLResponse(content=html)


@app.get("/api/map/geojson")
def get_map_geojson(sn: str = None):
    """Export map data as GeoJSON (for use in other tools or HA custom cards)."""
    sn = sn or _get_default_sn()
    geo = _get_map_geometry(sn)

    features = []
    for area in geo["areas"]:
        coords = [[lon, lat] for lat, lon in area["points"]]
        if coords:
            coords.append(coords[0])  # close the polygon
        features.append({
            "type": "Feature",
            "properties": {"name": area["name"], "area_sqm": round(area["area_sqm"], 1), "type": "area"},
            "geometry": {"type": "Polygon", "coordinates": [coords]},
        })
    for pw in geo["pathways"]:
        coords = [[lon, lat] for lat, lon in pw["points"]]
        features.append({
            "type": "Feature",
            "properties": {"name": pw["name"], "type": "pathway"},
            "geometry": {"type": "LineString", "coordinates": coords},
        })
    for cp in geo["chargers"]:
        features.append({
            "type": "Feature",
            "properties": {"type": "charger", "enabled": cp["enabled"]},
            "geometry": {"type": "Point", "coordinates": [cp["lon"], cp["lat"]]},
        })
    for sp in geo.get("snow_piles", []):
        coords = [[lon, lat] for lat, lon in sp["points"]]
        if coords:
            coords.append(coords[0])
        features.append({
            "type": "Feature",
            "properties": {"name": sp["name"], "type": "snow_pile"},
            "geometry": {"type": "Polygon", "coordinates": [coords]},
        })

    return {
        "type": "FeatureCollection",
        "features": features,
    }


@app.get("/api/map/background")
def get_map_background(sn: str = None):
    """Get the raster background image URL and coordinate metadata."""
    sn = sn or _get_default_sn()
    data = api.get_raster_background(sn)
    obj = json.loads(data.get("object_data", "{}"))
    return {
        "image_url": data.get("accessUrl"),
        "top_left": obj.get("top_left_real"),
        "center": obj.get("center_real"),
        "bottom_right": obj.get("bottom_right_real"),
        "rotation_rad": obj.get("rad"),
        "last_modified": data.get("gmt_modified"),
    }


# ── Charging Station ──

@app.get("/api/charging")
def get_charging(sn: str = None):
    """Get charging station and docking station info."""
    sn = sn or _get_default_sn()
    map_data = api.get_map(sn)

    active_cp = map_data.get("chargingPoint", {})
    all_cps = map_data.get("chargingPoints", [])
    dc = map_data.get("dc", {})

    return {
        "active_charging_point": {
            "id": active_cp.get("id"),
            "x": active_cp.get("chargingPoint", {}).get("x"),
            "y": active_cp.get("chargingPoint", {}).get("y"),
            "enabled": active_cp.get("enable"),
        },
        "all_charging_points": [
            {
                "id": cp.get("id"),
                "x": cp.get("chargingPoint", {}).get("x"),
                "y": cp.get("chargingPoint", {}).get("y"),
                "enabled": cp.get("enable"),
            }
            for cp in all_cps
        ],
        "docking_station": {
            "name": dc.get("dc_name"),
            "mac": dc.get("dc_mac"),
            "ip": dc.get("dc_ip_address"),
        },
    }


# ── Messages ──

@app.get("/api/messages")
def get_messages(sn: str = None, limit: int = 20):
    """Get recent device messages (errors, plan events)."""
    sn = sn or _get_default_sn()
    msgs = api.get_messages(sn)
    return msgs[:limit]


@app.get("/api/messages/latest")
def get_latest_message(sn: str = None):
    """Get the most recent message (HA-friendly)."""
    sn = sn or _get_default_sn()
    msgs = api.get_messages(sn)
    if not msgs:
        return {"title": "No messages", "type": 0, "error_code": "", "timestamp": None}
    m = msgs[0]
    msg_types = {0: "info", 1: "error", 2: "warning"}
    return {
        "title": m.get("msgTitle", ""),
        "type": msg_types.get(m.get("msgType", 0), "unknown"),
        "error_code": m.get("errCode", ""),
        "sender": m.get("sender", ""),
        "timestamp": m.get("gmtCreate"),
        "url": m.get("msgUrl", ""),
    }


# ── Work Plan History ──

# Plan-related error code prefixes and their meanings
PLAN_CODE_PREFIXES = {"WP", "PP", "PS", "PC", "PF", "PE", "WE"}

PLAN_CODE_DESCRIPTIONS = {
    "WP000": "Plan completed successfully",
    "WP001": "Plan cancelled by user",
    "WP002": "Plan paused - obstacle detected",
    "WP003": "Plan paused - heavy rain detected",
    "WP004": "Plan paused - device tilted",
    "WP005": "Plan paused - device lifted",
    "WP006": "Plan paused - low battery",
    "WP007": "Plan paused - blade stalled",
    "WP008": "Plan paused - wheels stalled",
    "WP009": "Plan paused - lost RTK signal",
    "WP010": "Plan paused - returned to dock",
    "WP011": "Plan paused - emergency stop",
    "WP012": "Plan paused - blower blocked",
    "WP013": "Plan paused - communication lost",
    "WP014": "Plan paused - snow too deep",
    "WP015": "Plan paused - charging error",
    "WP016": "Plan paused - temperature too low",
    "WP017": "Plan paused - temperature too high",
    "WP018": "Plan paused - IMU error",
    "WP019": "Plan paused - motor overheating",
    "WP020": "Plan paused - device offline",
    "WP022": "Plan paused - vision system error",
    "WP023": "Plan paused - path blocked",
    "WP024": "Plan paused - geofence breach",
    "WP025": "Plan paused - ultrasonic sensor error",
    "WP026": "Plan paused - docking station error",
    "WP027": "Plan paused - firmware update required",
    "WP030": "Plan paused - unknown/general error",
    "WP032": "Plan paused - scheduled maintenance",
    "PP001": "Plan started",
    "PP002": "Plan resumed",
    "PP003": "Plan paused by user",
    "PP004": "Plan queued",
    "PP005": "Plan skipped - conditions not met",
    "PP006": "Plan rescheduled",
    "PP007": "Plan expired",
    "PP008": "Plan area changed",
    "PP009": "Plan settings updated",
    "PP010": "Plan auto-started (schedule)",
    "PP011": "Plan auto-paused (schedule)",
    "PP012": "Plan area completed",
    "PP013": "Plan returning to dock",
    "PP014": "Plan charging before resume",
}


def _is_plan_event(msg: dict) -> bool:
    """Check if a message is a work plan event based on error code."""
    code = msg.get("errCode", "")
    if not code:
        return False
    prefix = code[:2]
    return prefix in PLAN_CODE_PREFIXES


def _enrich_plan_event(msg: dict) -> dict:
    """Add human-readable description and category to a plan event."""
    code = msg.get("errCode", "")
    description = PLAN_CODE_DESCRIPTIONS.get(code, msg.get("msgTitle", "Unknown plan event"))

    # Categorise: completed, started, paused, error, info
    if code == "WP000":
        category = "completed"
    elif code in ("PP001", "PP002", "PP010"):
        category = "started"
    elif code.startswith("PP"):
        category = "info"
    elif code.startswith("WP"):
        category = "paused"
    else:
        category = "error"

    ts = msg.get("gmtCreate")
    return {
        "timestamp": ts,
        "datetime": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None,
        "code": code,
        "title": msg.get("msgTitle", ""),
        "description": description,
        "category": category,
        "message_id": msg.get("msgId", ""),
    }


@app.get("/api/plans/history")
def get_plan_history(sn: str = None, limit: int = 50):
    """Get work plan execution history (filtered from device messages).

    Returns plan start/pause/complete/error events ordered newest-first.
    NOTE: Detailed per-run statistics (area covered, duration, path) are stored
    on the robot and only available via MQTT when the device is online.
    """
    sn = sn or _get_default_sn()
    msgs = api.get_messages(sn)
    plan_events = [_enrich_plan_event(m) for m in msgs if _is_plan_event(m)]
    return plan_events[:limit]


@app.get("/api/plans/summary")
def get_plan_summary(sn: str = None):
    """Aggregated work plan statistics from message history."""
    sn = sn or _get_default_sn()
    msgs = api.get_messages(sn)
    plan_events = [_enrich_plan_event(m) for m in msgs if _is_plan_event(m)]

    total = len(plan_events)
    by_category = {}
    by_code = {}
    for ev in plan_events:
        cat = ev["category"]
        by_category[cat] = by_category.get(cat, 0) + 1
        code = ev["code"]
        by_code[code] = by_code.get(code, 0) + 1

    last_event = plan_events[0] if plan_events else None
    first_event = plan_events[-1] if plan_events else None

    return {
        "total_events": total,
        "by_category": by_category,
        "by_code": by_code,
        "last_event": last_event,
        "first_event": first_event,
        "note": "Detailed per-run stats (duration, area covered) require MQTT connection to the device",
    }


# ── Firmware ──

@app.get("/api/firmware")
def get_firmware():
    """Get latest firmware and app version info."""
    fw = api.get_firmware()
    return {
        "app_version": fw.get("appVersion"),
        "firmware_version": fw.get("firmwareVersion"),
        "dc_version": fw.get("dcVersion"),
        "firmware_description": fw.get("firmwareDescription", "")[:500],
    }


@app.get("/api/firmware/dc")
def get_dc_firmware(sn: str = None):
    """Get docking station firmware details and OTA URL."""
    sn = sn or _get_default_sn()
    return api.get_dc_version(sn)


# ── Notifications ──

@app.get("/api/notifications/settings")
def get_notification_settings():
    """Get notification preference settings."""
    return api.get_notification_settings()


# ── Shared Users ──

@app.get("/api/shared_users")
def get_shared_users(sn: str = None):
    """Get list of users with shared access."""
    sn = sn or _get_default_sn()
    return api.get_shared_users(sn)


# ── Real-time Status (MQTT) ──

@app.get("/api/status")
def get_status():
    """
    Get real-time robot status from MQTT.
    Returns live telemetry when connected to robot's local broker,
    or last-known data otherwise.
    """
    if mqtt_client is None:
        return {"connected": False, "state": "unknown", "error": "MQTT not initialized"}
    return mqtt_client.get()


@app.post("/api/status/update")
def update_status(topic: str, payload: dict):
    """
    Push an MQTT status update into the bridge manually.
    Useful for feeding data from an external MQTT client or logcat parser.
    """
    if mqtt_client is None:
        raise HTTPException(status_code=503, detail="MQTT not initialized")
    mqtt_client.update(topic, payload)
    return {"ok": True}


# ── Cache Control ──

@app.post("/api/cache/clear")
def clear_cache():
    """Clear all cached API responses, forcing fresh fetches."""
    cache.invalidate()
    return {"ok": True, "message": "Cache cleared"}


# ── MQTT Connection Info ──

@app.get("/api/mqtt")
def get_mqtt_info():
    """Get MQTT connection status and configuration."""
    mc = mqtt_client
    return {
        "connected": mc.is_connected if mc else False,
        "robot_ip": CONFIG["robot_ip"] or None,
        "robot_port": CONFIG["robot_mqtt_port"],
        "robot_tls": CONFIG["robot_mqtt_tls"],
        "robot_serial": mc.serial if mc else CONFIG["robot_serial"] or None,
        "status": mc.get() if mc else {"connected": False, "state": "unknown"},
    }


@app.post("/api/mqtt/reconnect")
def mqtt_reconnect():
    """Restart the MQTT connection to the robot."""
    mc = mqtt_client
    if mc is None:
        raise HTTPException(status_code=503, detail="MQTT not initialized")
    mc.stop()
    time.sleep(0.5)
    mc.start()
    return {"ok": True, "message": "MQTT reconnection initiated"}


# ── Robot Commands (via MQTT) ──

@app.post("/api/command/{command}")
def send_command(command: str, payload: dict = None, wait: bool = True,
                 timeout: float = 5.0):
    """
    Send any MQTT command to the robot.

    Common commands:
      get_map, read_all_plan, get_device_msg, read_gps_ref,
      read_global_params, read_schedules, read_tow_params,
      read_no_charge_period, get_connect_wifi_name, bag_record,
      start_plan ({"id":1,"percent":0}), stop, pause, resume,
      cmd_recharge ({"cmd":2}), shutdown, restart_container,
      cmd_vel ({"vel":0.0,"rev":0.0}), cmd_roller ({"vel":1000}),
      mower_speed_cmd ({"state":80}), mower_target_cmd ({"target":100}),
      push_rod_cmd ({"state":0}),
      light_ctrl ({"led_head":255,...}),
      set_sound_param ({"enable":true,"vol":0.2,"mode":0}),
      set_working_state ({"state":1}),
      save_global_params ({"id":1,...}),
      ctrl_net_module_4G ({"state":1}),
      set_manul_camera_check ({"state":true}),
      mower_head_sensor_switch ({"state":-99}),
      check_map_connectivity ({"ids":[...]})
    """
    mc = _get_mqtt()
    if payload is None:
        payload = {}
    try:
        resp = mc.send_command(command, payload, wait=wait, timeout=timeout)
        return {"ok": True, "command": command, "response": resp}
    except ConnectionError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Convenience command shortcuts ──

@app.post("/api/robot/stop")
def robot_stop():
    """Emergency stop the robot."""
    mc = _get_mqtt()
    mc.send_command("stop", {}, wait=False)
    return {"ok": True, "command": "stop"}


@app.post("/api/robot/pause")
def robot_pause():
    """Pause current operation."""
    mc = _get_mqtt()
    mc.send_command("pause", {}, wait=False)
    return {"ok": True, "command": "pause"}


@app.post("/api/robot/resume")
def robot_resume():
    """Resume paused operation."""
    mc = _get_mqtt()
    mc.send_command("resume", {}, wait=False)
    return {"ok": True, "command": "resume"}


@app.post("/api/robot/dock")
def robot_dock():
    """Send robot back to docking station."""
    mc = _get_mqtt()
    mc.send_command("cmd_recharge", {"cmd": 2}, wait=False)
    return {"ok": True, "command": "cmd_recharge"}


# ── Calendar Blocking ──

def _check_calendar_busy() -> dict:
    """
    Check if the HA calendar entity currently has an active event.
    Returns {"busy": bool, "event_summary": str|None, "error": str|None}
    """
    if not CONFIG["calendar_block_enabled"]:
        return {"busy": False, "event_summary": None, "error": None}
    if not CONFIG["ha_token"]:
        log.warning("Calendar block enabled but HA_TOKEN not set")
        return {"busy": False, "event_summary": None, "error": "HA_TOKEN not configured"}

    entity_id = CONFIG["ha_calendar_entity"]
    url = f"{CONFIG['ha_url']}/api/states/{entity_id}"
    headers = {"Authorization": f"Bearer {CONFIG['ha_token']}"}
    try:
        resp = http_requests.get(url, headers=headers, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        state = data.get("state", "off")
        attrs = data.get("attributes", {})
        if state == "on":
            summary = attrs.get("message", attrs.get("friendly_name", "Event"))
            return {"busy": True, "event_summary": summary, "error": None}
        return {"busy": False, "event_summary": None, "error": None}
    except Exception as e:
        log.error("Failed to check HA calendar %s: %s", entity_id, e)
        return {"busy": False, "event_summary": None, "error": str(e)}


@app.get("/api/calendar/status")
def calendar_status():
    """Check if the calendar currently blocks Yarbo operation."""
    result = _check_calendar_busy()
    result["calendar_entity"] = CONFIG["ha_calendar_entity"]
    result["blocking_enabled"] = CONFIG["calendar_block_enabled"]
    return result


@app.post("/api/robot/start_plan")
def robot_start_plan(plan_id: int = 1, percent: int = 0, force: bool = False):
    """Start a work plan by ID. Blocked if calendar event is active (use force=true to override)."""
    if not force:
        cal = _check_calendar_busy()
        if cal["busy"]:
            return {
                "ok": False,
                "command": "start_plan",
                "plan_id": plan_id,
                "blocked": True,
                "reason": f"Calendar event active: {cal['event_summary']}",
                "calendar_entity": CONFIG["ha_calendar_entity"],
            }
    mc = _get_mqtt()
    resp = mc.send_command("start_plan", {"id": plan_id, "percent": percent}, timeout=5.0)
    return {"ok": True, "command": "start_plan", "plan_id": plan_id, "response": resp}


@app.post("/api/robot/shutdown")
def robot_shutdown():
    """Shut down the robot."""
    mc = _get_mqtt()
    mc.send_command("shutdown", {}, wait=False)
    return {"ok": True, "command": "shutdown"}


@app.post("/api/robot/drive")
def robot_drive(vel: float = 0.0, rev: float = 0.0):
    """Manual drive control (velocity + rotation)."""
    mc = _get_mqtt()
    mc.send_command("cmd_vel", {"vel": vel, "rev": rev}, wait=False)
    return {"ok": True, "command": "cmd_vel", "vel": vel, "rev": rev}


@app.post("/api/robot/lights")
def robot_lights(head: int = 0, left: int = 0, right: int = 0,
                 body_left: int = 0, body_right: int = 0,
                 tail_left: int = 0, tail_right: int = 0):
    """Control robot LEDs (0-255 per channel)."""
    mc = _get_mqtt()
    payload = {
        "led_head": head,
        "led_left_w": left,
        "led_right_w": right,
        "body_left_r": body_left,
        "body_right_r": body_right,
        "tail_left_r": tail_left,
        "tail_right_r": tail_right,
    }
    mc.send_command("light_ctrl", payload, wait=False)
    return {"ok": True, "command": "light_ctrl", "payload": payload}


@app.post("/api/robot/sound")
def robot_sound(enable: bool = True, vol: float = 0.2, mode: int = 0):
    """Control robot sound (enable/disable, volume 0.0-1.0)."""
    mc = _get_mqtt()
    mc.send_command("set_sound_param", {"enable": enable, "vol": vol, "mode": mode}, wait=False)
    return {"ok": True, "command": "set_sound_param"}


# ── Live data from robot (via MQTT) ──

@app.get("/api/live/map")
def get_live_map(refresh: bool = False):
    """
    Get the LIVE map data from the robot (includes no-go zones, latest edits).
    This is the current map on the robot, not the stale cloud backup.
    Pass ?refresh=true to re-request from robot.
    """
    mc = _get_mqtt()
    if refresh:
        mc.send_command("get_map", {}, wait=True, timeout=5.0)
    data = mc.live_map
    if data is None:
        raise HTTPException(status_code=404, detail="No live map data yet; robot may not be connected")
    return data


@app.get("/api/live/plans")
def get_live_plans(refresh: bool = False):
    """Get live work plans from the robot."""
    mc = _get_mqtt()
    if refresh:
        mc.send_command("read_all_plan", {}, wait=True, timeout=5.0)
    data = mc.live_plans
    if data is None:
        raise HTTPException(status_code=404, detail="No live plan data yet")
    return data


@app.get("/api/live/gps_ref")
def get_live_gps_ref(refresh: bool = False):
    """Get live GPS reference point from the robot."""
    mc = _get_mqtt()
    if refresh:
        mc.send_command("read_gps_ref", {}, wait=True, timeout=5.0)
    data = mc.live_gps_ref
    if data is None:
        raise HTTPException(status_code=404, detail="No live GPS ref data yet")
    return data


@app.get("/api/live/schedules")
def get_live_schedules(refresh: bool = False):
    """Get live schedules from the robot."""
    mc = _get_mqtt()
    if refresh:
        mc.send_command("read_schedules", {}, wait=True, timeout=5.0)
    data = mc.live_schedules
    if data is None:
        raise HTTPException(status_code=404, detail="No schedule data yet")
    return data


@app.get("/api/live/params")
def get_live_params(refresh: bool = False):
    """Get live global parameters from the robot (speeds, battery thresholds, etc.)."""
    mc = _get_mqtt()
    if refresh:
        mc.send_command("read_global_params", {"id": 1}, wait=True, timeout=5.0)
    data = mc.live_global_params
    if data is None:
        raise HTTPException(status_code=404, detail="No params data yet")
    return data


# ── HA-Specific Convenience Endpoints ──

@app.get("/api/ha/sensors")
def ha_sensors(sn: str = None):
    """
    All-in-one endpoint for Home Assistant.
    Returns a flat dict of all key values suitable for HA template sensors.
    """
    sn = sn or _get_default_sn()
    device = api.get_devices()[0] if api.get_devices() else {}
    fw = api.get_firmware()
    msgs = api.get_messages(sn)
    map_data = api.get_map(sn)
    status = mqtt_client.get() if mqtt_client else {}

    head_types = {0: "mower", 1: "snow_blower", 2: "blower", 3: "trimmer"}
    ref = map_data.get("ref", {}).get("ref", {})
    dc = map_data.get("dc", {})
    areas = map_data.get("area", [])

    latest_msg = msgs[0] if msgs else {}
    plan_events = [_enrich_plan_event(m) for m in msgs if _is_plan_event(m)]

    return {
        # Device
        "serial_number": device.get("serialNum", sn),
        "device_name": device.get("deviceNickname", "Yarbo"),
        "head_type": head_types.get(device.get("headType", -1), "unknown"),

        # Status (MQTT)
        "state": status.get("state", "unknown"),
        "connected": status.get("connected", False),
        "last_heartbeat": status.get("last_heartbeat"),
        "battery_level": status.get("battery", {}).get("level") if isinstance(status.get("battery"), dict) else status.get("battery"),

        # Map
        "gps_latitude": ref.get("latitude"),
        "gps_longitude": ref.get("longitude"),
        "total_area_sq_meters": round(sum(a.get("area", 0) for a in areas), 1),
        "area_count": len(areas),
        "pathway_count": len(map_data.get("pathway", [])),
        "docking_station": dc.get("dc_name", ""),

        # Firmware
        "firmware_version": fw.get("firmwareVersion", ""),
        "app_version": fw.get("appVersion", ""),
        "dc_version": fw.get("dcVersion", ""),

        # Latest message
        "last_message_title": latest_msg.get("msgTitle", ""),
        "last_message_code": latest_msg.get("errCode", ""),
        "last_message_time": latest_msg.get("gmtCreate"),

        # Latest plan event
        "last_plan_code": plan_events[0]["code"] if plan_events else "",
        "last_plan_description": plan_events[0]["description"] if plan_events else "",
        "last_plan_time": plan_events[0]["timestamp"] if plan_events else None,
        "plan_events_total": len(plan_events),

        # Meta
        "bridge_version": "1.0.0",
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }


# ─── Main ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log.info("Starting Yarbo Bridge on %s:%d", CONFIG["bridge_host"], CONFIG["bridge_port"])
    log.info("API docs at http://localhost:%d/docs", CONFIG["bridge_port"])

    # Pre-authenticate — try refresh token first, then fall back to existing token
    try:
        if tokens.refresh_token:
            tokens._refresh()
            log.info("Initial authentication via refresh token successful")
        elif tokens.access_token:
            log.info("Using pre-loaded access token (will refresh on expiry)")
        else:
            tokens._login()
            log.info("Initial authentication via password grant successful")
    except Exception as e:
        if tokens.access_token:
            log.warning("Auth refresh failed, using pre-loaded token: %s", e)
        else:
            log.error("Initial auth failed completely: %s", e)
            log.error("Set YARBO_ACCESS_TOKEN env var with a valid JWT to start")

    # Initialize MQTT client to robot's local broker
    mqtt_client = _init_mqtt_client()
    if CONFIG["robot_ip"]:
        log.info("Starting MQTT connection to robot at %s:%d",
                 CONFIG["robot_ip"], CONFIG["robot_mqtt_port"])
        mqtt_client.start()
    else:
        log.info("─" * 60)
        log.info("MQTT: No robot IP configured.")
        log.info("  To enable real-time MQTT, connect your Yarbo to WiFi,")
        log.info("  find its IP on your router, then set YARBO_ROBOT_IP.")
        log.info("  e.g.: set YARBO_ROBOT_IP=192.168.1.42")
        log.info("  REST API endpoints still work without MQTT.")
        log.info("─" * 60)

    uvicorn.run(app, host=CONFIG["bridge_host"], port=CONFIG["bridge_port"], log_level="info")
