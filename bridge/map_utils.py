"""
Map utilities: coordinate conversion, geometry parsing, plan event helpers.
"""

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from bridge.config import CONFIG, log

try:
    import requests as http_requests
except ImportError:
    http_requests = None


# ── Coordinate conversion ────────────────────────────────────────────────

def local_to_gps(x: float, y: float, ref_lat: float, ref_lon: float) -> tuple:
    """Convert local x/y (meters) to GPS lat/lon.

    The Yarbo coordinate system uses:
      x = west (+) / east (-)   (mirrored from standard)
      y = north (+) / south (-)
    relative to the GPS reference point.
    """
    lat = ref_lat + y / 111320.0
    lon = ref_lon - x / (111320.0 * math.cos(math.radians(ref_lat)))
    return lat, lon


# ── Map geometry ─────────────────────────────────────────────────────────

def get_map_geometry(sn: str, api, mqtt_client) -> dict:
    """Extract areas, pathways, charging points as GPS polygons.

    Prefers the MQTT map (live from robot or cached get_map.json) over
    the cloud API map, which is often weeks out of date.
    """
    mqtt_map = load_mqtt_map(mqtt_client)
    if mqtt_map:
        geo = get_mqtt_map_geometry(mqtt_map)
        if mqtt_client and mqtt_client._live_map:
            geo["_source"] = "Live MQTT"
        else:
            geo["_source"] = "MQTT (get_map.json)"
        return geo

    # Fallback to cloud API (may be stale)
    map_data = api.get_map(sn)
    ref = map_data.get("ref", {}).get("ref", {})
    ref_lat = ref.get("latitude", 0)
    ref_lon = ref.get("longitude", 0)

    areas_geo = []
    for area in map_data.get("area", []):
        pts = area.get("range", [])
        gps_pts = [local_to_gps(p["x"], p["y"], ref_lat, ref_lon) for p in pts]
        areas_geo.append({
            "name": area.get("name", "Area"),
            "area_sqm": area.get("area", 0),
            "points": gps_pts,
            "local_points": [(p["x"], p["y"]) for p in pts],
        })

    pathways_geo = []
    for pw in map_data.get("pathway", []):
        pts = pw.get("range", [])
        gps_pts = [local_to_gps(p["x"], p["y"], ref_lat, ref_lon) for p in pts]
        pathways_geo.append({
            "name": pw.get("name", "Pathway"),
            "points": gps_pts,
            "local_points": [(p["x"], p["y"]) for p in pts],
        })

    nogo_geo = []
    for nz in map_data.get("nogozone", []):
        pts = nz.get("range", [])
        gps_pts = [local_to_gps(p["x"], p["y"], ref_lat, ref_lon) for p in pts]
        nogo_geo.append({"name": "No-Go Zone", "points": gps_pts})

    chargers = []
    for cp in map_data.get("chargingPoints", []):
        pt = cp.get("chargingPoint", {})
        lat, lon = local_to_gps(pt.get("x", 0), pt.get("y", 0), ref_lat, ref_lon)
        chargers.append({"lat": lat, "lon": lon, "enabled": cp.get("enable", False)})

    # Snow pile zones (within each area)
    snow_piles_geo = []
    for area in map_data.get("area", []):
        for sp in area.get("snowPiles", []):
            pts = sp.get("range", [])
            gps_pts = [local_to_gps(p["x"], p["y"], ref_lat, ref_lon) for p in pts]
            snow_piles_geo.append({
                "name": "Snow Pile Zone",
                "points": gps_pts,
                "local_points": [(p["x"], p["y"]) for p in pts],
            })

    # Sidewalks (cloud key is singular 'sidewalk')
    sidewalks_geo = []
    for sw in map_data.get("sidewalk", []):
        pts = sw.get("range", [])
        gps_pts = [local_to_gps(p["x"], p["y"], ref_lat, ref_lon) for p in pts]
        sidewalks_geo.append({
            "id": sw.get("id"),
            "name": sw.get("name", "Sidewalk"),
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
            tl_lat, tl_lon = local_to_gps(tl.get("x", 0), tl.get("y", 0), ref_lat, ref_lon)
            br_lat, br_lon = local_to_gps(br.get("x", 0), br.get("y", 0), ref_lat, ref_lon)
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
        "sidewalks": sidewalks_geo,
        "raster": raster,
        "raw": map_data,
        "_source": "Cloud API (may be stale)",
    }


def get_mqtt_map_geometry(mqtt_map_data) -> dict:
    """Parse MQTT get_map response into GPS geometry for Leaflet rendering.

    The MQTT map uses slightly different keys than the cloud map:
      - 'areas' not 'area'
      - 'allchargingData' not 'chargingPoints'
      - 'nogozones' not 'nogozone'
      - 'pathways' not 'pathway'
      - Each area/pathway has its own 'ref' with lat/lon
    """
    # Safety: robot sometimes returns map as a JSON string
    if isinstance(mqtt_map_data, str):
        try:
            mqtt_map_data = json.loads(mqtt_map_data)
        except (json.JSONDecodeError, ValueError):
            log.warning("MQTT map data is an unparseable string, skipping")
            return {"ref_lat": 0, "ref_lon": 0, "areas": [], "pathways": [],
                    "nogo": [], "chargers": [], "snow_piles": [],
                    "sidewalks": [], "raster": None, "raw": None,
                    "_source": "MQTT (parse error)"}
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
        gps_pts = [local_to_gps(p["x"], p["y"], a_ref_lat, a_ref_lon) for p in pts]
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
            sp_gps = [local_to_gps(p["x"], p["y"], sp_ref_lat, sp_ref_lon) for p in sp_pts]
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
        gps_pts = [local_to_gps(p["x"], p["y"], pw_ref_lat, pw_ref_lon) for p in pts]
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
        gps_pts = [local_to_gps(p["x"], p["y"], nz_ref_lat, nz_ref_lon) for p in pts]
        nogo_geo.append({
            "name": nz.get("name", "No-Go Zone"),
            "points": gps_pts,
            "enabled": nz.get("enable", True),
        })

    chargers = []
    for cp in mqtt_map_data.get("allchargingData", []):
        pt = cp.get("chargingPoint", {})
        lat, lon = local_to_gps(pt.get("x", 0), pt.get("y", 0), ref_lat, ref_lon)
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
        gps_pts = [local_to_gps(p["x"], p["y"], sw_ref_lat, sw_ref_lon) for p in pts]
        sidewalks_geo.append({
            "id": sw.get("id"),
            "name": sw.get("name", "Sidewalk"),
            "points": gps_pts,
        })

    elec_fence = []
    for ef in mqtt_map_data.get("elec_fence", []):
        pts = ef.get("range", [])
        ef_ref = ef.get("ref", {})
        ef_ref_lat = ef_ref.get("latitude", ref_lat)
        ef_ref_lon = ef_ref.get("longitude", ref_lon)
        gps_pts = [local_to_gps(p["x"], p["y"], ef_ref_lat, ef_ref_lon) for p in pts]
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


def load_mqtt_map(mqtt_client) -> Optional[dict]:
    """Load MQTT map data from the live bridge cache or the saved response file."""
    if mqtt_client and mqtt_client._live_map:
        return mqtt_client._live_map

    responses_file = Path(__file__).parent.parent / "mqtt" / "responses" / "get_map.json"
    if responses_file.exists():
        with open(responses_file) as f:
            data = json.load(f)
        resp = data.get("response", {})
        return resp.get("data", resp)

    return None


def build_raster_overlay_js(geo: dict) -> str:
    """Generate JS code to overlay the raster background image on Leaflet."""
    raster = geo.get("raster")
    if not raster or not raster.get("image_url"):
        return "var rasterOverlay = null; // No raster background available"
    bounds = raster["bounds"]
    img_url = raster["image_url"]
    lats = [bounds[0][0], bounds[1][0]]
    lons = [bounds[0][1], bounds[1][1]]
    sw = [min(lats), min(lons)]
    ne = [max(lats), max(lons)]
    return (
        f"var rasterOverlay = L.imageOverlay('{img_url}', "
        f"[{json.dumps(sw)}, {json.dumps(ne)}], "
        f"{{opacity: 0.7}}).addTo(map);"
    )


# ── Plan event helpers ───────────────────────────────────────────────────

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


def is_plan_event(msg: dict) -> bool:
    """Check if a message is a work plan event based on error code."""
    code = msg.get("errCode", "")
    if not code:
        return False
    prefix = code[:2]
    return prefix in PLAN_CODE_PREFIXES


def enrich_plan_event(msg: dict) -> dict:
    """Add human-readable description and category to a plan event."""
    code = msg.get("errCode", "")
    description = PLAN_CODE_DESCRIPTIONS.get(code, msg.get("msgTitle", "Unknown plan event"))

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


# ── Calendar blocking ───────────────────────────────────────────────────

def check_calendar_busy() -> dict:
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
