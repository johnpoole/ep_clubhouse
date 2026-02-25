"""
MQTT client for direct connection to the Yarbo robot's local broker.
"""

import json
import time
import zlib
import gzip
import io
import ssl
import uuid
import threading
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone
from typing import Optional

from bridge.config import CONFIG, log
from bridge.discovery import discover_robot, save_cached_ip, _probe_port

try:
    import paho.mqtt.client as paho_mqtt
except ImportError:
    paho_mqtt = None


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

        # MQTT traffic logger (optional)
        self._mqtt_logger = None
        if CONFIG.get("mqtt_log_enabled"):
            self._setup_mqtt_logger()

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
        self._preview_plan_path: Optional[dict] = None

        # ── breadcrumb trail ──
        self._trail: list = []           # list of {"x": float, "y": float, "ts": float}
        self._trail_active: bool = False # True while a plan is running
        self._max_trail_points: int = 2000

        # ── control command tracking ──
        self._control_commands: list = []  # Recent control commands (max 50)
        self._max_control_commands: int = 50

        # ── periodic refresh ──
        self._refresh_thread: Optional[threading.Thread] = None
        self._stop_refresh: threading.Event = threading.Event()

        # ── rediscovery state ──
        self._disconnect_count: int = 0
        self._rediscovery_in_progress: bool = False
        self._last_rediscovery: float = 0

    # ── MQTT traffic logging ─────────────────────────────────────────────

    def _setup_mqtt_logger(self):
        """Set up separate logger for MQTT traffic with rotation."""
        log_file = CONFIG.get("mqtt_log_file", "/opt/yarbo-bridge/mqtt_traffic.log")
        self._mqtt_logger = logging.getLogger("yarbo-mqtt-traffic")
        self._mqtt_logger.setLevel(logging.DEBUG)
        # Remove any existing handlers
        self._mqtt_logger.handlers = []
        # Rotating file handler: 10MB per file, keep 3 backups (40MB max)
        fh = RotatingFileHandler(log_file, maxBytes=10*1024*1024, backupCount=3)
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        ))
        self._mqtt_logger.addHandler(fh)
        # Also log to console if main log is DEBUG
        if log.level <= logging.DEBUG:
            ch = logging.StreamHandler()
            ch.setLevel(logging.DEBUG)
            ch.setFormatter(logging.Formatter("[MQTT] %(message)s"))
            self._mqtt_logger.addHandler(ch)
        self._mqtt_logger.propagate = False
        log.info("MQTT traffic logging enabled: %s", log_file)

    def _log_mqtt_rx(self, topic: str, data: dict, raw_size: int = 0):
        """Log received MQTT message."""
        if self._mqtt_logger:
            payload_preview = json.dumps(data, ensure_ascii=False)[:500]
            size_info = f" ({raw_size} bytes)" if raw_size else ""
            self._mqtt_logger.debug("RX [%s]%s: %s", topic, size_info, payload_preview)

    def _log_mqtt_tx(self, topic: str, payload: dict, raw_size: int = 0):
        """Log transmitted MQTT message."""
        if self._mqtt_logger:
            payload_preview = json.dumps(payload, ensure_ascii=False)[:500]
            size_info = f" ({raw_size} bytes)" if raw_size else ""
            self._mqtt_logger.debug("TX [%s]%s: %s", topic, size_info, payload_preview)

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
        self._stop_refresh.set()
        if self._refresh_thread:
            self._refresh_thread.join(timeout=2)
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
            self._disconnect_count = 0  # reset on successful connect
            with self._lock:
                self._status["connected"] = True

            sn = self.serial
            # Single wildcard to capture ALL topics (joystick, control, everything)
            topics = [(f"snowbot/{sn}/#", 0)]
            client.subscribe(topics)
            log.info("Subscribed to blanket wildcard snowbot/%s/# (all topics)", sn)

            # Request full state on connect
            self._request_initial_state()

            # Confirm robot IP via get_connect_wifi_name
            self._confirm_robot_ip()

            # Start periodic refresh thread
            self._start_refresh_thread()
        else:
            log.error("MQTT connection refused: rc=%s", reason_code)
            self._connected = False

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self._disconnect_count += 1
        self._connected = False
        with self._lock:
            self._status["connected"] = False

        if self._disconnect_count <= 1:
            log.warning("Disconnected from robot MQTT (rc=%s), paho will auto-reconnect", reason_code)
        elif self._disconnect_count == 3:
            log.warning("MQTT reconnect failing (attempt %d) — will try rediscovery",
                        self._disconnect_count)
            self._try_rediscovery()
        elif self._disconnect_count % 10 == 0:
            log.warning("MQTT still disconnected (attempt %d) — retrying discovery",
                        self._disconnect_count)
            self._try_rediscovery()

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
            raw_size = len(raw)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                log.debug("Non-JSON on %s: %s", msg.topic, raw[:100])
                return

            tp = msg.topic

            # Track control commands (before logging)
            self._track_control_command(tp, data)

            # Log all incoming messages
            self._log_mqtt_rx(tp, data, raw_size)

            if "heart_beat" in tp:
                with self._lock:
                    self._status["connected"] = True
                    self._status["last_heartbeat"] = datetime.now(timezone.utc).isoformat()
                    # Store working_state from heartbeat
                    if "working_state" in data:
                        ws_code = data["working_state"]
                        state_map = {0: "standby", 1: "idle", 2: "working", 3: "charging",
                                   4: "docking", 5: "error", 6: "returning", 7: "paused"}
                        self._status["state"] = state_map.get(ws_code, "unknown")
                        self._status["working_state_code"] = ws_code
                return

            if "data_feedback" in tp:
                self._handle_data_feedback(data)
                return

            if "DeviceMSG" in tp:
                # Real-time telemetry messages
                with self._lock:
                    if not self._device_msg:
                        self._device_msg = {}
                    # Update device_msg with real-time data
                    for key, value in data.items():
                        if isinstance(value, dict):
                            if key not in self._device_msg:
                                self._device_msg[key] = {}
                            self._device_msg[key].update(value)
                        else:
                            self._device_msg[key] = value
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
            elif topic == "get_connect_wifi_name":
                # Robot reports its own WiFi connection info including IP
                wifi_ip = payload.get("ip", "") if isinstance(payload, dict) else ""
                self._status["wifi_info"] = payload
                if wifi_ip:
                    self._status["robot_wifi_ip"] = wifi_ip
                    log.info("Robot reports WiFi IP: %s (connected to '%s', signal %s)",
                             wifi_ip,
                             payload.get("name", "?"),
                             payload.get("signal", "?"))
                    # Detect IP mismatch
                    if wifi_ip != self.robot_ip:
                        log.warning(
                            "IP MISMATCH: connected to %s but robot reports %s — "
                            "caching new IP for next reconnect",
                            self.robot_ip, wifi_ip)
                        save_cached_ip(wifi_ip)
                    else:
                        save_cached_ip(wifi_ip)

            # ── command responses ──
            elif topic == "get_map":
                # Robot sometimes double-encodes the map as a JSON string
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except (json.JSONDecodeError, ValueError):
                        pass
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
                # ── breadcrumb trail: record odom while plan is active ──
                if isinstance(payload, dict):
                    state_msg = payload.get("StateMSG", {})
                    is_planning = bool(state_msg.get("on_going_planning", 0))
                    if is_planning and not self._trail_active:
                        self._trail_active = True
                        self._trail.clear()
                        log.info("Breadcrumb trail started (plan active)")
                    elif not is_planning and self._trail_active:
                        self._trail_active = False
                        log.info("Breadcrumb trail stopped (%d points)", len(self._trail))
                    if self._trail_active:
                        odom = payload.get("CombinedOdom", {})
                        if odom.get("x") is not None and odom.get("y") is not None:
                            self._trail.append({
                                "x": odom["x"],
                                "y": odom["y"],
                                "ts": odom.get("timestamp", time.time()),
                            })
                            if len(self._trail) > self._max_trail_points:
                                self._trail = self._trail[-self._max_trail_points:]
            elif topic == "preview_plan_path":
                self._preview_plan_path = payload
                log.info("Received preview_plan_path (%s)",
                         "error" if payload.get("state", 0) < 0 else "%d pts" % len(payload.get("data", payload.get("path", []))))
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

    def _track_control_command(self, topic: str, data: dict):
        """Track control commands (cmd_vel, cmd_roller, set_working_state, etc) for HA display."""
        # Only track control commands (from app/* topics that are NOT data requests)
        control_topics = [
            "cmd_vel",          # Joystick movement
            "cmd_roller",       # Roller/auger control
            "set_working_state", # Start/stop/pause/dock
            "set_plan_roller",  # Enable roller for plans
            "start_plan",       # Start plan execution
            "stop",             # Emergency stop
            "pause",            # Pause operation
            "resume",           # Resume operation
            "dock",             # Return to dock
            "preview_plan_path", # Plan preview
        ]
        
        # Check if this is a control command
        is_control = any(ct in topic for ct in control_topics)
        if not is_control:
            return
        
        # Extract command name from topic
        cmd_name = topic.split("/")[-1] if "/" in topic else topic
        
        # Create command entry
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "command": cmd_name,
            "topic": topic,
            "payload": data,
        }
        
        # Add to list (thread-safe)
        with self._lock:
            self._control_commands.append(entry)
            # Keep only last N commands
            if len(self._control_commands) > self._max_control_commands:
                self._control_commands = self._control_commands[-self._max_control_commands:]

    # ── IP management ────────────────────────────────────────────────────

    def _confirm_robot_ip(self):
        """After connecting, ask the robot what IP it thinks it has."""
        try:
            self.send_command("get_connect_wifi_name", {}, wait=False)
        except Exception as e:
            log.warning("get_connect_wifi_name request failed: %s", e)

    def _try_rediscovery(self):
        """Run IP discovery in a background thread and reconnect if a new IP is found."""
        if self._rediscovery_in_progress:
            return
        # Rate-limit: no more than once per 60 seconds
        if time.time() - self._last_rediscovery < 60:
            return

        def _do_rediscovery():
            self._rediscovery_in_progress = True
            self._last_rediscovery = time.time()
            try:
                old_ip = self.robot_ip
                log.info("Running IP rediscovery (current: %s) ...", old_ip)
                new_ip = discover_robot(old_ip, self.port)
                if new_ip and new_ip != old_ip:
                    log.info("Rediscovery found robot at %s (was %s)", new_ip, old_ip)
                    self.update_ip(new_ip)
                elif new_ip:
                    log.info("Rediscovery confirmed IP %s — paho will keep retrying", new_ip)
                else:
                    log.warning("Rediscovery found no robot on the network")
            except Exception as e:
                log.warning("Rediscovery failed: %s", e)
            finally:
                self._rediscovery_in_progress = False

        threading.Thread(target=_do_rediscovery, daemon=True, name="mqtt-rediscovery").start()

    def update_ip(self, new_ip: str):
        """Update the robot IP and reconnect."""
        if new_ip == self.robot_ip:
            log.info("Robot IP unchanged (%s), skipping reconnect", new_ip)
            return False
        old_ip = self.robot_ip
        log.info("Updating robot IP: %s → %s", old_ip, new_ip)
        self.robot_ip = new_ip
        CONFIG["robot_ip"] = new_ip
        save_cached_ip(new_ip)
        # Reconnect with new IP
        self.stop()
        time.sleep(0.5)
        # Reset stop event for new refresh thread
        self._stop_refresh = threading.Event()
        self.start()
        return True

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

    def _start_refresh_thread(self):
        """Background thread: refresh device_msg (60s) and health-check IP (5 min)."""
        def refresh_loop():
            log.info("Device refresh thread started (60s data / 5min health-check)")
            tick = 0
            while not self._stop_refresh.wait(60):  # wake every 60 seconds
                tick += 1
                if self._connected:
                    try:
                        self.send_command("get_device_msg", {}, wait=False)
                    except Exception as e:
                        log.warning("Periodic device_msg refresh failed: %s", e)

                # Every 5 minutes: verify the robot IP is still reachable
                if tick % 5 == 0 and self.robot_ip:
                    if not _probe_port(self.robot_ip, self.port, timeout=3.0):
                        log.warning("Health-check: robot at %s not responding — running rediscovery",
                                    self.robot_ip)
                        self._try_rediscovery()
                    else:
                        log.debug("Health-check: robot at %s OK", self.robot_ip)

            log.info("Device refresh thread stopped")

        self._refresh_thread = threading.Thread(target=refresh_loop, daemon=True)
        self._refresh_thread.start()

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
        self._log_mqtt_tx(topic, payload, len(raw))

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

    @property
    def live_plans(self):
        return self._live_plans

    @property
    def live_gps_ref(self):
        return self._live_gps_ref

    @property
    def live_schedules(self):
        return self._live_schedules

    @property
    def live_global_params(self):
        return self._live_global_params

    @property
    def device_msg(self):
        return self._device_msg

    @property
    def control_commands(self):
        """Return list of recent control commands."""
        with self._lock:
            return list(self._control_commands)

    @property
    def preview_plan_path(self):
        return self._preview_plan_path

    @property
    def trail(self):
        return list(self._trail)

    @property
    def trail_active(self):
        return self._trail_active

    def clear_trail(self):
        """Clear the breadcrumb trail."""
        with self._lock:
            self._trail.clear()

    @staticmethod
    def _decode_state(code) -> str:
        states = {
            0: "idle", 1: "working", 2: "paused", 3: "charging",
            4: "error", 5: "docking", 6: "returning",
        }
        return states.get(code, "unknown_%s" % code)


def init_mqtt_client(api) -> 'YarboMQTTClient':
    """Create the MQTT client, discovering robot IP and serial automatically."""
    configured_ip = CONFIG["robot_ip"]
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

    # Dynamic IP discovery
    robot_ip = discover_robot(configured_ip, port)
    if robot_ip:
        if robot_ip != configured_ip:
            log.info("Robot discovered at %s (configured was %s)",
                     robot_ip, configured_ip or "<empty>")
        CONFIG["robot_ip"] = robot_ip  # update runtime config
    else:
        robot_ip = configured_ip  # fall back to configured
        if robot_ip:
            log.warning("Discovery failed — using configured IP %s (may be stale)", robot_ip)
        else:
            log.warning("No robot IP found. Set YARBO_ROBOT_IP or ensure robot is on WiFi.")

    return YarboMQTTClient(robot_ip, serial, port, use_tls)
