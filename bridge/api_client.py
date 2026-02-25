"""
Yarbo cloud REST API client.
"""

import json

import requests as http_requests
from fastapi import HTTPException

from bridge.config import CONFIG, log
from bridge.auth import TokenManager
from bridge.cache import Cache


class YarboAPI:
    """Wrapper around Yarbo's cloud REST API."""

    BASE = CONFIG["api_base"]
    MQTT_BASE = CONFIG["mqtt_api_base"]

    def __init__(self, tokens: TokenManager, cache: Cache):
        self._tokens = tokens
        self._cache = cache

    def _get(self, path: str, base: str = None) -> dict:
        url = (base or self.BASE) + path
        r = http_requests.get(url, headers=self._tokens.get_headers(), timeout=15)
        if r.status_code == 401:
            # Token expired mid-flight, force refresh and retry
            self._tokens.access_token = None
            r = http_requests.get(url, headers=self._tokens.get_headers(), timeout=15)
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail=r.text[:300])
        data = r.json()
        if data.get("code") != "00000":
            raise HTTPException(status_code=502, detail=data.get("message", "API error"))
        return data["data"]

    def _post(self, path: str, body: dict, base: str = None) -> dict:
        url = (base or self.BASE) + path
        r = http_requests.post(url, headers=self._tokens.get_headers(), json=body, timeout=15)
        if r.status_code == 401:
            self._tokens.access_token = None
            r = http_requests.post(url, headers=self._tokens.get_headers(), json=body, timeout=15)
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail=r.text[:300])
        data = r.json()
        if data.get("code") != "00000":
            raise HTTPException(status_code=502, detail=data.get("message", "API error"))
        return data["data"]

    # ── Device ──

    def get_devices(self) -> list:
        cached = self._cache.get("devices", CONFIG["cache_ttl_device"])
        if cached is not None:
            return cached
        data = self._get("/yarbo/robot-service/commonUser/userRobotBind/getUserRobotBindVos")
        result = data.get("deviceList", [])
        self._cache.set("devices", result)
        return result

    def get_user_info(self) -> dict:
        cached = self._cache.get("user_info", CONFIG["cache_ttl_device"])
        if cached is not None:
            return cached
        data = self._get("/yarbo/robot-service/robot/commonUser/getUesrInfo")
        self._cache.set("user_info", data)
        return data

    # ── Map ──

    def get_map(self, sn: str) -> dict:
        cached = self._cache.get(f"map_{sn}", CONFIG["cache_ttl_map"])
        if cached is not None:
            return cached
        data = self._get(f"/yarbo/commonUser/getUploadMap?sn={sn}")
        maps = data.get("mapList", [])
        if not maps:
            return {}
        map_json = json.loads(maps[0].get("mapJson", "{}"))
        self._cache.set(f"map_{sn}", map_json)
        return map_json

    def get_raster_background(self, sn: str) -> dict:
        cached = self._cache.get(f"raster_{sn}", CONFIG["cache_ttl_map"])
        if cached is not None:
            return cached
        data = self._get(f"/yarbo/robot/rasterBackground/get?sn={sn}")
        self._cache.set(f"raster_{sn}", data)
        return data

    # ── Messages ──

    def get_messages(self, sn: str) -> list:
        cached = self._cache.get(f"messages_{sn}", CONFIG["cache_ttl_messages"])
        if cached is not None:
            return cached
        data = self._get(f"/yarbo/msg/userDeviceMsg?sn={sn}")
        msgs = []
        for dev_msg in data.get("deviceMsg", []):
            for msg in dev_msg.get("msgs", []):
                msgs.append(msg)
        self._cache.set(f"messages_{sn}", msgs)
        return msgs

    # ── Firmware ──

    def get_firmware(self) -> dict:
        cached = self._cache.get("firmware", CONFIG["cache_ttl_firmware"])
        if cached is not None:
            return cached
        data = self._get("/yarbo/commonUser/getLatestPubVersion")
        self._cache.set("firmware", data)
        return data

    def get_dc_version(self, sn: str) -> dict:
        cached = self._cache.get(f"dc_version_{sn}", CONFIG["cache_ttl_firmware"])
        if cached is not None:
            return cached
        data = self._post("/yarbo/robot/getDcVersion", {"sn": sn})
        self._cache.set(f"dc_version_{sn}", data)
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

    # ── Agora Video ──
    # INACTIVE: Video feed not working, see CONTEXT_VIDEO_FEED.md

    # def get_agora_token(self, sn: str, uid: str = "10001") -> dict:
    #     """Get Agora RTC token + encryption config for video streaming."""
    #     url = self.BASE + "/yarbo/robot-service/robot/commonUser/getAgoraToken"
    #     body = {"sn": sn, "channel_name": sn, "uid": uid}
    #     r = http_requests.post(url, headers=self._tokens.get_headers(), json=body, timeout=15)
    #     if r.status_code == 401:
    #         self._tokens.access_token = None
    #         r = http_requests.post(url, headers=self._tokens.get_headers(), json=body, timeout=15)
    #     if r.status_code != 200:
    #         raise HTTPException(status_code=r.status_code, detail=r.text[:300])
    #     data = r.json()
    #     if data.get("code") != "00000":
    #         raise HTTPException(status_code=502,
    #                             detail=data.get("message", "Agora token error"))
    #     return data["data"]
