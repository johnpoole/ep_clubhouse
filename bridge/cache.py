"""
Simple TTL cache for API responses.
"""

import time
import threading
from typing import Optional


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
