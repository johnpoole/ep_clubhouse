"""
Dynamic data center IP discovery for Yarbo Bridge.

The Yarbo Data Center (docking station) runs EMQX on port 8883 (TLS)
and connects to the home network via ethernet (e.g. 192.168.68.102).
The robot itself connects to the data center over WiFi (e.g. 192.168.68.105).
We connect to the DATA CENTER broker, not the robot directly.

Strategies (in order):
  1. Try the configured/cached IP — fast TLS handshake on port 8883
  2. Scan the local subnet for hosts with port 8883 open (EMQX broker)
  3. After connecting, confirm via `get_connect_wifi_name` MQTT command

Port 8883 TLS is unusual enough on a home network that a subnet scan
reliably identifies the data center.
"""

import socket
import ssl
import ipaddress
import threading
import time
import logging
from typing import Optional
from pathlib import Path

log = logging.getLogger("yarbo-bridge.discovery")

# File to persist the last-known good data center IP across restarts
_CACHE_FILE = Path(__file__).parent.parent / ".robot_ip_cache"

# How long to wait for a TLS handshake (seconds)
_PROBE_TIMEOUT = 1.5

# Max concurrent scan threads
_MAX_SCAN_THREADS = 50


def _probe_port(ip: str, port: int = 8883, timeout: float = _PROBE_TIMEOUT) -> bool:
    """Try a TLS connection to ip:port. Returns True if it responds."""
    try:
        sock = socket.create_connection((ip, port), timeout=timeout)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with ctx.wrap_socket(sock, server_hostname=ip) as ssock:
            # If handshake completes, this is an MQTT-TLS broker
            ssock.close()
        return True
    except (OSError, ssl.SSLError, ConnectionRefusedError, TimeoutError):
        return False


def _get_local_subnet() -> Optional[str]:
    """Detect the local subnet (CIDR) of the default route interface."""
    try:
        # Connect to an external IP to find which local interface is used
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        # Assume /24 — typical home network
        net = ipaddress.IPv4Network(f"{local_ip}/24", strict=False)
        return str(net)
    except Exception:
        return None


def _scan_subnet(subnet: str, port: int = 8883,
                 exclude_ip: str = None) -> Optional[str]:
    """Scan a /24 subnet for hosts with `port` open. Returns first hit or None."""
    try:
        network = ipaddress.IPv4Network(subnet, strict=False)
    except ValueError:
        return None

    results = []
    lock = threading.Lock()

    def _check(ip_str):
        if ip_str == exclude_ip:
            return
        if _probe_port(ip_str, port):
            with lock:
                results.append(ip_str)

    threads = []
    for host in network.hosts():
        ip_str = str(host)
        t = threading.Thread(target=_check, args=(ip_str,), daemon=True)
        threads.append(t)
        t.start()
        # Limit concurrency
        if len(threads) >= _MAX_SCAN_THREADS:
            for tt in threads:
                tt.join(timeout=_PROBE_TIMEOUT + 0.5)
            threads.clear()
        # Early exit if found
        if results:
            break

    # Wait for remaining threads
    for t in threads:
        t.join(timeout=_PROBE_TIMEOUT + 0.5)

    return results[0] if results else None


def load_cached_ip() -> Optional[str]:
    """Load the last-known-good data center IP from disk cache."""
    try:
        if _CACHE_FILE.exists():
            ip = _CACHE_FILE.read_text().strip()
            if ip:
                return ip
    except Exception:
        pass
    return None


def save_cached_ip(ip: str):
    """Persist a known-good data center IP to disk."""
    try:
        _CACHE_FILE.write_text(ip)
        log.info("Cached data center IP: %s → %s", ip, _CACHE_FILE)
    except Exception as e:
        log.warning("Failed to cache data center IP: %s", e)


def discover_robot(configured_ip: str = "",
                   port: int = 8883,
                   subnet: str = None) -> Optional[str]:
    """
    Find the Yarbo Data Center's IP address (where the MQTT broker runs).

    Note: The data center (docking station) runs EMQX on ethernet.
    The robot itself is a separate device on WiFi — we don't connect to it.

    Order of attempts:
      1. configured_ip (from env/config) — probe it
      2. Cached IP from last successful connection
      3. Subnet scan for port 8883

    Returns the IP string, or None if not found.
    """
    # 1. Try configured IP
    if configured_ip:
        log.info("Probing configured IP %s:%d ...", configured_ip, port)
        if _probe_port(configured_ip, port):
            log.info("✓ Configured IP %s responds on port %d", configured_ip, port)
            save_cached_ip(configured_ip)
            return configured_ip
        log.warning("✗ Configured IP %s not responding on port %d", configured_ip, port)

    # 2. Try cached IP (if different from configured)
    cached = load_cached_ip()
    if cached and cached != configured_ip:
        log.info("Probing cached IP %s:%d ...", cached, port)
        if _probe_port(cached, port):
            log.info("✓ Cached IP %s responds on port %d", cached, port)
            return cached
        log.warning("✗ Cached IP %s not responding", cached)

    # 3. Subnet scan
    scan_subnet = subnet or _get_local_subnet()
    if not scan_subnet:
        log.warning("Cannot determine local subnet for scanning")
        return None

    log.info("Scanning subnet %s for MQTT broker on port %d ...", scan_subnet, port)
    t0 = time.time()
    found = _scan_subnet(scan_subnet, port)
    elapsed = time.time() - t0

    if found:
        log.info("✓ Found data center at %s (scan took %.1fs)", found, elapsed)
        save_cached_ip(found)
        return found

    log.warning("✗ No MQTT broker found on subnet %s (scan took %.1fs)", scan_subnet, elapsed)
    return None
