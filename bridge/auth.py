"""
Auth0 token lifecycle management for Yarbo Bridge.
"""

import time
import threading
from typing import Optional

import requests as http_requests

from bridge.config import CONFIG, log


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
            # Try to extract actual expiry from JWT
            try:
                import base64, json as _json
                parts = self.access_token.split(".")
                payload = parts[1] + "=" * (4 - len(parts[1]) % 4)
                claims = _json.loads(base64.urlsafe_b64decode(payload))
                self.expires_at = claims.get("exp", time.time() + 86400) - 300
                log.info("Loaded initial access token from config (expires %s)",
                         time.strftime("%Y-%m-%d %H:%M", time.localtime(self.expires_at + 300)))
            except Exception:
                self.expires_at = time.time() + 86400  # fallback: assume valid 1 day
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
