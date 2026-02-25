"""
FastAPI route handlers for the Yarbo Bridge REST API.

All routes are registered via ``register_routes(app, api, mqtt_ref, cache)``.
The ``mqtt_ref`` is a mutable list ``[mqtt_client]`` so the reference can be
updated after MQTT connects (the router captures it at import time).
"""

import json
import time
from datetime import datetime, timezone
from typing import Optional

from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response

from bridge.config import CONFIG, log
from bridge.discovery import discover_robot
from bridge.map_utils import (
    local_to_gps,
    get_map_geometry,
    get_mqtt_map_geometry,
    load_mqtt_map,
    build_raster_overlay_js,
    is_plan_event,
    enrich_plan_event,
    check_calendar_busy,
)


# AGORA_APP_ID = "affc62d646c840ceba4d374500fc7f92"  # INACTIVE: Video feed not working


def register_routes(app, api, mqtt_ref: list, cache):
    """Register all API route handlers on *app*.

    ``mqtt_ref`` is ``[mqtt_client]``; use ``mqtt_ref[0]`` to dereference
    because the MQTT client may be replaced after the initial app startup.
    """

    _PROJECT_ROOT = Path(__file__).resolve().parent.parent

    @app.get("/api/favicon.png")
    async def favicon_png():
        """Serve the Yarbo favicon as a PNG image."""
        fav = _PROJECT_ROOT / "images" / "favicon.ico"
        if not fav.exists():
            raise HTTPException(404, "favicon not found")
        return FileResponse(fav, media_type="image/png")

    @app.get("/api/datacenter.png")
    async def datacenter_png():
        """Serve the data-center marker icon."""
        img = _PROJECT_ROOT / "images" / "datacenter.png"
        if not img.exists():
            raise HTTPException(404, "datacenter icon not found")
        return FileResponse(img, media_type="image/png")

    def _mc():
        """Get the active MQTT client or raise 503."""
        mc = mqtt_ref[0]
        if mc is None:
            raise HTTPException(503, "MQTT not initialised — set YARBO_ROBOT_IP")
        return mc

    def _sn(sn: str = None) -> str:
        mc = mqtt_ref[0]
        if sn:
            return sn
        if mc and mc.serial:
            return mc.serial
        try:
            devs = api.get_devices()
            if devs:
                return devs[0]["serialNum"]
        except Exception:
            pass
        return ""

    # ── Health ───────────────────────────────────────────────────────

    @app.get("/")
    def root():
        return {"status": "ok", "service": "yarbo-bridge", "version": "1.0.0"}

    # ── Devices ──────────────────────────────────────────────────────

    @app.get("/api/devices")
    def get_devices():
        return api.get_devices()

    @app.get("/api/device")
    def get_device():
        devices = api.get_devices()
        if not devices:
            raise HTTPException(404, "No devices found")
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

    # ── User ─────────────────────────────────────────────────────────

    @app.get("/api/user")
    def get_user():
        return api.get_user_info()

    # ── Map & Areas ──────────────────────────────────────────────────

    @app.get("/api/map")
    def get_map(sn: str = None):
        return api.get_map(_sn(sn))

    @app.get("/api/areas")
    def get_areas(sn: str = None):
        map_data = api.get_map(_sn(sn))
        areas = map_data.get("area", [])
        return [
            {
                "id": a["id"],
                "name": a["name"],
                "area_sq_meters": round(a.get("area", 0), 1),
                "boundary_points": len(a.get("range", [])),
                "boundary": a.get("range", []),
                "gps_ref": a.get("ref", {}),
                "snow_piles": len(a.get("snowPiles", [])),
                "trimming_edges": len(a.get("trimming_edges", [])),
            }
            for a in areas
        ]

    @app.get("/api/map/summary")
    def get_map_summary(sn: str = None):
        s = _sn(sn)
        map_data = api.get_map(s)
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

    # ── MQTT Map View ────────────────────────────────────────────────

    @app.get("/api/map/mqtt", response_class=HTMLResponse)
    def get_mqtt_map_view():
        mc = mqtt_ref[0]
        mqtt_map = load_mqtt_map(mc)
        if not mqtt_map:
            raise HTTPException(404, "No MQTT map data available. Connect to robot MQTT or place get_map.json in mqtt/responses/")

        geo = get_mqtt_map_geometry(mqtt_map)

        def _esc(s):
            return str(s).replace('\\', '\\\\').replace('"', '\\"').replace("'", "\\'")

        colors = ["#4fc3f7", "#81c784", "#ffb74d", "#ba68c8", "#ef5350", "#26c6da"]
        area_polygons_js = []
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
            sw_id = sw.get("id")
            if sw_id is not None:
                sw_popup = (
                    f'`<b>{sw_name}</b><br>'
                    f'<button onclick="startJob({sw_id})" '
                    f'style="margin-top:6px;padding:4px 12px;'
                    f'background:#4caf50;color:#fff;border:none;border-radius:4px;cursor:pointer">'
                    f'\u25b6 Start</button>`'
                )
            else:
                sw_popup = f'"{sw_name}"'
            sidewalk_js.append(
                f'L.polyline({coords}, {{color:"#b0bec5",weight:4,opacity:0.7}})'
                f'.addTo(sidewalksLayer).bindPopup({sw_popup});'
            )

        fence_js = []
        for ef in geo.get("elec_fence", []):
            coords = json.dumps([[lat, lon] for lat, lon in ef["points"]])
            fence_js.append(
                f'L.polyline({coords}, {{color:"#ff9800",weight:2,dashArray:"4,4"}})'
                f'.addTo(fenceLayer).bindPopup("Electric Fence");'
            )

        ref_marker = f'L.marker([{geo["ref_lat"]},{geo["ref_lon"]}], {{icon:L.icon({{iconUrl:"/api/datacenter.png",iconSize:[36,36],iconAnchor:[18,18],popupAnchor:[0,-18]}})}}).addTo(markersLayer).bindPopup("GPS Reference Point");'
        all_js = "\n".join(area_polygons_js + pathway_lines_js + nogo_js + snow_js + charger_js + sidewalk_js + fence_js + [ref_marker])

        all_points = []
        for a in geo["areas"]:
            all_points.extend(a["points"])
        for sp in geo.get("snow_piles", []):
            all_points.extend(sp["points"])
        for nz in geo["nogo"]:
            all_points.extend(nz["points"])

        source_label = "Live MQTT" if (mc and mc._live_map) else "Cached (get_map.json)"

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
    var baseLayers = {{ "OpenStreetMap": osmLayer, "Esri Satellite": esriSat }};
    var overlays = {{
      "Areas": areasLayer, "Pathways": pathwaysLayer, "No-Go Zones": nogoLayer,
      "Snow Piles": snowLayer, "Chargers": chargersLayer, "Sidewalks": sidewalksLayer,
      "Electric Fence": fenceLayer, "Reference Point": markersLayer
    }};
    L.control.layers(baseLayers, overlays).addTo(map);
    var allPoints = {json.dumps([[lat, lon] for lat, lon in all_points])};
    if (allPoints.length > 0) {{ map.fitBounds(allPoints, {{padding: [30,30]}}); }}
    function showToast(msg, type) {{
      var t = document.getElementById('toast');
      if (!t) {{ t = document.createElement('div'); t.id='toast'; t.style.cssText='position:fixed;bottom:20px;left:50%;transform:translateX(-50%);padding:10px 20px;border-radius:8px;color:#fff;font-size:14px;z-index:9999;display:none;'; document.body.appendChild(t); }}
      t.textContent = msg; t.style.background = type==='error'?'#ef5350':type==='success'?'#4caf50':'#333';
      t.style.display = 'block'; setTimeout(function(){{ t.style.display = 'none'; }}, 3500);
    }}
    function apiPost(path, params) {{
      var url = path;
      if (params) {{ var qs = Object.entries(params).map(function(e){{ return e[0]+'='+e[1]; }}).join('&'); url += '?' + qs; }}
      return fetch(url, {{method:'POST'}}).then(function(r){{ return r.json(); }});
    }}
    function startJob(areaId) {{
      showToast('Starting plan ' + areaId + '...', '');
      apiPost('/api/robot/start_plan', {{plan_id: areaId, percent: 0}})
        .then(function(d) {{
          if (d.ok) showToast('Plan ' + areaId + ' started', 'success');
          else if (d.blocked) showToast('Blocked: ' + d.reason, 'error');
          else showToast(JSON.stringify(d), 'error');
        }}).catch(function(e) {{ showToast('Error: ' + e.message, 'error'); }});
    }}
  </script>
</body>
</html>"""
        return HTMLResponse(content=html)

    # ── SVG Map ──────────────────────────────────────────────────────

    @app.get("/api/map/svg")
    def get_map_svg(sn: str = None, width: int = 800, height: int = 600):
        s = _sn(sn)
        mc = mqtt_ref[0]
        geo = get_map_geometry(s, api, mc)

        all_pts = []
        for a in geo["areas"]:
            all_pts.extend(a["local_points"])
        for p in geo["pathways"]:
            all_pts.extend(p["local_points"])
        for sp in geo.get("snow_piles", []):
            all_pts.extend(sp["local_points"])
        for sw in geo.get("sidewalks", []):
            all_pts.extend(sw.get("local_points", []))

        if not all_pts:
            return Response(content="<svg xmlns='http://www.w3.org/2000/svg'/>",
                            media_type="image/svg+xml")

        xs = [p[0] for p in all_pts]
        ys = [p[1] for p in all_pts]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        pad = 3
        min_x -= pad; max_x += pad; min_y -= pad; max_y += pad
        range_x = max_x - min_x or 1
        range_y = max_y - min_y or 1
        scale = min(width / range_x, height / range_y)

        def tx(x, y):
            sx = (max_x - x) * scale
            sy = height - (y - min_y) * scale
            return f"{sx:.1f},{sy:.1f}"

        import math  # noqa: F811 (already imported at module level but local is fine)
        parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}"'
                 f' viewBox="0 0 {width} {height}" style="background:#1a1a2e">']

        # Grid
        parts.append('<g stroke="#2a2a4a" stroke-width="0.5" opacity="0.5">')
        grid_step = 10
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
        svg_colors = ["#4fc3f7", "#81c784", "#ffb74d", "#ba68c8"]
        for i, area in enumerate(geo["areas"]):
            color = svg_colors[i % len(svg_colors)]
            pts_str = " ".join(tx(x, y) for x, y in area["local_points"])
            parts.append(f'<polygon points="{pts_str}" fill="{color}" fill-opacity="0.25"'
                         f' stroke="{color}" stroke-width="2"/>')
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

        # Snow piles
        for sp in geo.get("snow_piles", []):
            pts_str = " ".join(tx(x, y) for x, y in sp["local_points"])
            parts.append(f'<polygon points="{pts_str}" fill="#90caf9" fill-opacity="0.15"'
                         f' stroke="#90caf9" stroke-width="1.5" stroke-dasharray="6,3"/>')

        # Sidewalks
        for sw in geo.get("sidewalks", []):
            if sw.get("local_points"):
                pts_str = " ".join(tx(x, y) for x, y in sw["local_points"])
                parts.append(f'<polyline points="{pts_str}" fill="none"'
                             f' stroke="#b0bec5" stroke-width="4" stroke-opacity="0.7"/>')
                if sw["local_points"]:
                    lx, ly = tx(*sw["local_points"][0]).split(",")
                    parts.append(f'<text x="{lx}" y="{float(ly)-8:.1f}" fill="#b0bec5"'
                                 f' font-family="sans-serif" font-size="11">{sw["name"]}</text>')

        # Charging stations
        for cp in geo["raw"].get("chargingPoints", []):
            pt = cp.get("chargingPoint", {})
            cx_val, cy_val = tx(pt.get("x", 0), pt.get("y", 0)).split(",")
            color = "#4caf50" if cp.get("enable") else "#757575"
            parts.append(f'<circle cx="{cx_val}" cy="{cy_val}" r="6" fill="{color}" stroke="white" stroke-width="1.5"/>')
            zap_label = "\u26a1 Active" if cp.get("enable") else "\u26a1"
            parts.append(f'<text x="{cx_val}" y="{float(cy_val)-10:.1f}" fill="{color}"'
                         f' font-family="sans-serif" font-size="10" text-anchor="middle">'
                         f'{zap_label}</text>')

        # Origin marker
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

    # ── Leaflet Map View ─────────────────────────────────────────────

    @app.get("/api/map/view", response_class=HTMLResponse)
    def get_map_view(sn: str = None):
        s = _sn(sn)
        mc = mqtt_ref[0]
        geo = get_map_geometry(s, api, mc)

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
            charger_js.append(
                f'L.circleMarker([{cp["lat"]},{cp["lon"]}], {{radius:8,color:"{cp_color}",fillColor:"{cp_color}",fillOpacity:0.8}})'
                f'.addTo(chargersLayer).bindPopup("Charging Station ({cp_icon})");'
            )

        snow_js = []
        for sp in geo.get("snow_piles", []):
            coords = json.dumps([[lat, lon] for lat, lon in sp["points"]])
            snow_js.append(f'L.polygon({coords}, {{color:"#90caf9",weight:1.5,dashArray:"6,3",fillOpacity:0.15}}).addTo(snowLayer).bindPopup("Snow Pile Zone");')

        sidewalk_js = []
        for sw in geo.get("sidewalks", []):
            coords = json.dumps([[lat, lon] for lat, lon in sw["points"]])
            sw_name = sw["name"]
            sw_id = sw.get("id")
            if sw_id is not None:
                sw_popup = (
                    f'`<b>{sw_name}</b><br>'
                    f'<button onclick="startJob({sw_id})" '
                    f'style="margin-top:6px;padding:4px 12px;'
                    f'background:#4caf50;color:#fff;border:none;border-radius:4px;cursor:pointer">'
                    f'\u25b6 Start</button>`'
                )
            else:
                sw_popup = f'"{sw_name}"'
            sidewalk_js.append(
                f'L.polyline({coords}, {{color:"#b0bec5",weight:4,opacity:0.7}})'
                f'.addTo(sidewalksLayer).bindPopup({sw_popup});'
            )

        ref_marker = f'L.marker([{geo["ref_lat"]},{geo["ref_lon"]}], {{icon:L.icon({{iconUrl:"/api/datacenter.png",iconSize:[36,36],iconAnchor:[18,18],popupAnchor:[0,-18]}})}}).addTo(markersLayer).bindPopup("GPS Reference Point");'
        all_js = "\n".join(area_polygons_js + pathway_lines_js + nogo_js + snow_js + sidewalk_js + charger_js + [ref_marker])

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
    var sidewalksLayer = L.layerGroup().addTo(map);
    var chargersLayer = L.layerGroup().addTo(map);
    var markersLayer = L.layerGroup().addTo(map);
    {build_raster_overlay_js(geo)}
    {all_js}
    var baseLayers = {{ "OpenStreetMap": osmLayer, "Esri Satellite": esriSat }};
    var overlays = {{}};
    if (rasterOverlay) overlays["Yarbo Satellite"] = rasterOverlay;
    overlays["Areas"] = areasLayer;
    overlays["Pathways"] = pathwaysLayer;
    overlays["No-Go Zones"] = nogoLayer;
    overlays["Snow Piles"] = snowLayer;
    overlays["Sidewalks"] = sidewalksLayer;
    overlays["Chargers"] = chargersLayer;
    overlays["Reference Point"] = markersLayer;
    L.control.layers(baseLayers, overlays).addTo(map);
    var allPoints = {json.dumps([[lat,lon] for a in geo['areas'] for lat,lon in a['points']] + [[lat,lon] for sp in geo.get('snow_piles',[]) for lat,lon in sp['points']] + [[lat,lon] for sw in geo.get('sidewalks',[]) for lat,lon in sw['points']])};
    if (allPoints.length > 0) {{ map.fitBounds(allPoints, {{padding: [30,30]}}); }}
    function showToast(msg, type) {{
      var t = document.getElementById('toast');
      if (!t) {{ t = document.createElement('div'); t.id='toast'; t.style.cssText='position:fixed;bottom:20px;left:50%;transform:translateX(-50%);padding:10px 20px;border-radius:8px;color:#fff;font-size:14px;z-index:9999;display:none;'; document.body.appendChild(t); }}
      t.textContent = msg; t.style.background = type==='error'?'#ef5350':type==='success'?'#4caf50':'#333';
      t.style.display = 'block'; setTimeout(function(){{ t.style.display = 'none'; }}, 3500);
    }}
    function apiPost(path, params) {{
      var url = path;
      if (params) {{ var qs = Object.entries(params).map(function(e){{ return e[0]+'='+e[1]; }}).join('&'); url += '?' + qs; }}
      return fetch(url, {{method:'POST'}}).then(function(r){{ return r.json(); }});
    }}
    function startJob(areaId) {{
      showToast('Starting plan ' + areaId + '...', '');
      apiPost('/api/robot/start_plan', {{plan_id: areaId, percent: 0}})
        .then(function(d) {{
          if (d.ok) showToast('Plan ' + areaId + ' started', 'success');
          else if (d.blocked) showToast('Blocked: ' + d.reason, 'error');
          else showToast(JSON.stringify(d), 'error');
        }}).catch(function(e) {{ showToast('Error: ' + e.message, 'error'); }});
    }}
  </script>
</body>
</html>"""
        return HTMLResponse(content=html)

    # ── Dashboard ────────────────────────────────────────────────────

    @app.get("/api/dashboard", response_class=HTMLResponse)
    def get_dashboard(sn: str = None):
        s = _sn(sn)
        mc = mqtt_ref[0]
        geo = get_map_geometry(s, api, mc)

        # ── Fetch robot plans for sidebar ──
        plans = []
        if mc:
            try:
                if mc.live_plans is None:
                    mc.send_command("read_all_plan", {}, wait=True, timeout=5.0)
                plan_data = mc.live_plans or {}
                plans = plan_data.get("data", []) if isinstance(plan_data, dict) else plan_data
            except Exception:
                pass

        colors = ["#4fc3f7", "#81c784", "#ffb74d", "#ba68c8", "#ef5350", "#26c6da"]
        area_polygons_js = []
        for i, area in enumerate(geo["areas"]):
            color = colors[i % len(colors)]
            coords = json.dumps([[lat, lon] for lat, lon in area["points"]])
            sqm = round(area["area_sqm"])
            area_id = area.get("id", i + 1)
            area_polygons_js.append(
                f'L.polygon({coords}, {{color:"{color}",weight:2,fillOpacity:0.25}})'
                f'.addTo(areasLayer).bindPopup(`<b>{area["name"]}</b><br>{sqm} m\u00b2<br>'
                f'<button onclick="startJob({area_id})" '
                f'style="margin-top:6px;padding:4px 12px;'
                f'background:#4caf50;color:#fff;border:none;border-radius:4px;cursor:pointer">'
                f'\u25b6 Start</button>`);'
            )

        plan_list_html = []
        for p in plans:
            pid = p.get("id", 0)
            pname = p.get("name", f"Plan {pid}").strip()
            pcolor = colors[(pid - 1) % len(colors)]
            area_count = len(p.get("areaIds", []))
            area_label = f"{area_count} area{'s' if area_count != 1 else ''}"
            plan_list_html.append(
                f'<div class="entity-row">'
                f'  <div class="entity-dot" style="background:{pcolor}"></div>'
                f'  <div class="entity-info">'
                f'    <span class="entity-name">{pname}</span>'
                f'    <span class="entity-secondary">{area_label}</span>'
                f'  </div>'
                f'  <button class="preview-btn" onclick="previewPath({pid})" title="Preview path">'
                f'    <svg width="16" height="16" viewBox="0 0 24 24"><path d="M12 2C8.13 2 5 5.13 5 9c0 5.25 7 13 7 13s7-7.75 7-13c0-3.87-3.13-7-7-7zm0 9.5a2.5 2.5 0 010-5 2.5 2.5 0 010 5z" fill="currentColor"/></svg>'
                f'  </button>'
                f'  <button class="play-btn" onclick="startJob({pid})" title="Start {pname}">'
                f'    <svg width="18" height="18" viewBox="0 0 24 24"><path d="M8 5v14l11-7z" fill="currentColor"/></svg>'
                f'  </button>'
                f'</div>'
            )

        pathway_lines_js = []
        for pw in geo["pathways"]:
            coords = json.dumps([[lat, lon] for lat, lon in pw["points"]])
            pathway_lines_js.append(
                f'L.polyline({coords}, {{color:"#ffd54f",weight:3,dashArray:"8,4"}})'
                f'.addTo(pathwaysLayer).bindPopup("{pw["name"]}");'
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

        sidewalk_js = []
        for sw in geo.get("sidewalks", []):
            coords = json.dumps([[lat, lon] for lat, lon in sw["points"]])
            sw_name = sw["name"]
            sw_id = sw.get("id")
            if sw_id is not None:
                sw_popup = (
                    f'`<b>{sw_name}</b><br>'
                    f'<button onclick="startJob({sw_id})" '
                    f'style="margin-top:6px;padding:4px 12px;'
                    f'background:#4caf50;color:#fff;border:none;border-radius:4px;cursor:pointer">'
                    f'\u25b6 Start</button>`'
                )
            else:
                sw_popup = f'"{sw_name}"'
            sidewalk_js.append(
                f'L.polyline({coords}, {{color:"#b0bec5",weight:4,opacity:0.7}})'
                f'.addTo(sidewalksLayer).bindPopup({sw_popup});'
            )

        ref_marker = f'L.marker([{geo["ref_lat"]},{geo["ref_lon"]}], {{icon:L.icon({{iconUrl:"/api/datacenter.png",iconSize:[36,36],iconAnchor:[18,18],popupAnchor:[0,-18]}})}}).addTo(markersLayer).bindPopup("GPS Reference Point");'
        all_map_js = "\n".join(area_polygons_js + pathway_lines_js + nogo_js + snow_js + sidewalk_js + charger_js + [ref_marker])
        areas_html = "\n".join(plan_list_html) if plan_list_html else '<div class="entity-row"><span class="entity-name" style="color:var(--secondary-text)">No plans available</span></div>'
        all_pts_json = json.dumps(
            [[lat, lon] for a in geo["areas"] for lat, lon in a["points"]]
            + [[lat, lon] for sp in geo.get("snow_piles", []) for lat, lon in sp["points"]]
            + [[lat, lon] for sw in geo.get("sidewalks", []) for lat, lon in sw["points"]]
        )

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Yarbo Dashboard</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Roboto:wght@400;500&display=swap" rel="stylesheet" media="print" onload="this.media='all'">
  <style>
    :root {{
      --primary-color: #03a9f4;
      --accent-color: #ff9800;
      --background: #f0f0f5;
      --card-bg: #ffffff;
      --primary-text: #212121;
      --secondary-text: #727272;
      --divider: rgba(0,0,0,.06);
      --card-radius: 12px;
      --card-shadow: 0 1px 3px 0 rgba(0,0,0,.1), 0 1px 2px -1px rgba(0,0,0,.1);
      --green: #4caf50;
      --red: #f44336;
      --orange: #ff9800;
      --blue: #2196f3;
      --purple: #9c27b0;
      --teal: #009688;
      --sidebar-width: 320px;
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --background: #1b1b1f;
        --card-bg: #2c2c30;
        --primary-text: #e3e3e8;
        --secondary-text: #9e9ea6;
        --divider: rgba(255,255,255,.08);
        --card-shadow: 0 1px 4px 0 rgba(0,0,0,.4);
      }}
    }}
    * {{ margin:0; padding:0; box-sizing:border-box; }}
    body {{
      font-family: Roboto, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      background: var(--background); color: var(--primary-text);
      display: flex; height: 100vh; overflow: hidden;
    }}

    /* ── Sidebar ── */
    .sidebar {{
      width: var(--sidebar-width); min-width: var(--sidebar-width);
      background: var(--background); display: flex; flex-direction: column;
      overflow-y: auto; padding: 12px; gap: 12px;
    }}
    .sidebar::-webkit-scrollbar {{ width: 6px; }}
    .sidebar::-webkit-scrollbar-thumb {{ background: rgba(128,128,128,.3); border-radius: 3px; }}

    /* ── HA-style Card ── */
    .ha-card {{
      background: var(--card-bg); border-radius: var(--card-radius);
      box-shadow: var(--card-shadow); overflow: visible; flex-shrink: 0;
    }}
    .ha-card-header {{
      padding: 16px 16px 0; font-size: 16px; font-weight: 500;
      color: var(--primary-text); display: flex; align-items: center; gap: 8px;
    }}
    .ha-card-header .header-icon {{
      width: 24px; height: 24px; color: var(--secondary-text);
    }}
    .ha-card-content {{ padding: 12px 0 4px; }}

    /* ── Entity Row ── */
    .entity-row {{
      display: flex; align-items: center; padding: 8px 16px; gap: 12px;
      min-height: 48px; transition: background .15s;
    }}
    .entity-row:not(:last-child) {{ border-bottom: 1px solid var(--divider); }}
    .entity-row:hover {{ background: rgba(128,128,128,.06); }}
    .entity-dot {{
      width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0;
    }}
    .entity-info {{ flex: 1; min-width: 0; }}
    .entity-name {{
      font-size: 14px; font-weight: 400; color: var(--primary-text);
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }}
    .entity-secondary {{
      font-size: 12px; color: var(--secondary-text); margin-top: 1px;
    }}
    .entity-state {{
      font-size: 14px; color: var(--primary-text); white-space: nowrap;
      font-weight: 400;
    }}
    .entity-state.active {{ color: var(--green); }}
    .entity-state.warning {{ color: var(--orange); }}
    .entity-state.error {{ color: var(--red); }}

    /* ── Play Button ── */
    .play-btn {{
      width: 36px; height: 36px; border-radius: 50%; border: none;
      background: rgba(76,175,80,.12); color: var(--green); cursor: pointer;
      display: flex; align-items: center; justify-content: center;
      transition: all .2s; flex-shrink: 0;
    }}
    .play-btn:hover {{ background: rgba(76,175,80,.25); transform: scale(1.08); }}
    .preview-btn {{
      width: 32px; height: 32px; border-radius: 50%; border: none;
      background: rgba(255,152,0,.1); color: var(--orange); cursor: pointer;
      display: flex; align-items: center; justify-content: center;
      transition: all .2s; flex-shrink: 0;
    }}
    .preview-btn:hover {{ background: rgba(255,152,0,.25); transform: scale(1.08); }}
    .refresh-btn {{
      width: 28px; height: 28px; border-radius: 50%; border: none;
      background: transparent; color: var(--secondary-text); cursor: pointer;
      display: flex; align-items: center; justify-content: center;
      transition: all .25s; margin-left: auto; flex-shrink: 0;
    }}
    .refresh-btn:hover {{ background: rgba(128,128,128,.12); color: var(--primary-text); }}
    .refresh-btn.spinning svg {{ animation: spin .8s linear infinite; }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}

    /* ── Tile Buttons ── */
    .tile-grid {{
      display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 8px;
      padding: 12px 16px 16px;
    }}
    .tile-btn {{
      display: flex; flex-direction: column; align-items: center;
      justify-content: center; gap: 6px; padding: 14px 4px;
      border-radius: 12px; border: none; cursor: pointer;
      font-size: 12px; font-weight: 500; font-family: inherit;
      transition: all .2s; min-height: 64px;
    }}
    .tile-btn svg {{ width: 24px; height: 24px; }}
    .tile-btn:hover {{ filter: brightness(1.1); transform: translateY(-1px); }}
    .tile-btn:active {{ transform: scale(.97); }}
    .tile-stop {{
      background: rgba(244,67,54,.1); color: var(--red);
      grid-column: 1 / -1; flex-direction: row; gap: 8px; padding: 12px;
    }}
    .tile-pause {{ background: rgba(255,152,0,.1); color: var(--orange); }}
    .tile-resume {{ background: rgba(33,150,243,.1); color: var(--blue); }}
    .tile-dock {{ background: rgba(156,39,176,.1); color: var(--purple); }}
    .tile-undock {{ background: rgba(0,150,136,.1); color: var(--teal); }}
    .tile-trail {{ background: rgba(0,229,255,.1); color: #00e5ff; }}
    .tile-preview {{ background: rgba(255,152,0,.1); color: var(--orange); }}

    /* ── Status Card ── */
    .status-icon {{
      width: 36px; height: 36px; border-radius: 50%;
      display: flex; align-items: center; justify-content: center;
      flex-shrink: 0;
    }}
    .status-icon svg {{ width: 20px; height: 20px; }}
    .status-icon.robot {{ background: rgba(3,169,244,.1); color: var(--primary-color); }}
    .status-icon.battery {{ background: rgba(76,175,80,.1); color: var(--green); }}
    .status-icon.calendar {{ background: rgba(255,152,0,.1); color: var(--orange); }}
    .status-icon.mqtt {{ background: rgba(0,150,136,.1); color: var(--teal); }}
    .status-icon.map {{ background: rgba(156,39,176,.1); color: var(--purple); }}

    /* ── Toast ── */
    .toast {{
      position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%);
      background: var(--card-bg); color: var(--primary-text);
      padding: 12px 24px; border-radius: 12px; font-size: 14px;
      z-index: 9999; display: none;
      box-shadow: 0 4px 16px rgba(0,0,0,.2); border: 1px solid var(--divider);
    }}
    .toast.error {{ background: var(--red); color: #fff; border: none; }}
    .toast.success {{ background: var(--green); color: #fff; border: none; }}

    /* ── Map ── */
    #map {{ flex: 1; border-radius: 0; }}

    @media (max-width: 700px) {{
      body {{ flex-direction: column; }}
      .sidebar {{ width: 100%; min-width: unset; max-height: 40vh; flex-direction: column; }}
      #map {{ height: 60vh; }}
    }}
  </style>
</head>
<body>
  <div class="sidebar">
    <!-- ── Header Card ── -->
    <div class="ha-card">
      <div class="ha-card-header">
        <img class="header-icon" src="/api/favicon.png" style="width:24px;height:24px;object-fit:contain;">
        <span>Yarbo</span>
      </div>
      <div class="ha-card-content" style="padding: 8px 16px 12px;">
        <span class="entity-secondary" id="header-summary">\u2014</span>
      </div>
    </div>

    <!-- ── Plans Card ── -->
    <div class="ha-card">
      <div class="ha-card-header">
        <svg class="header-icon" viewBox="0 0 24 24"><path d="M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm-2 10H7v-2h10v2z" fill="currentColor"/></svg>
        Plans
        <button class="refresh-btn" id="refreshPlansBtn" onclick="refreshPlans()" title="Refresh plans">
          <svg width="16" height="16" viewBox="0 0 24 24"><path d="M17.65 6.35A7.958 7.958 0 0012 4c-4.42 0-7.99 3.58-7.99 8s3.57 8 7.99 8c3.73 0 6.84-2.55 7.73-6h-2.08A5.99 5.99 0 0112 18c-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z" fill="currentColor"/></svg>
        </button>
      </div>
      <div class="ha-card-content" id="plansList">
        {areas_html}
      </div>
    </div>

    <!-- ── Controls Card ── -->
    <div class="ha-card">
      <div class="tile-grid">
        <button class="tile-btn tile-stop" onclick="robotCmd('stop')">
          <svg viewBox="0 0 24 24"><path d="M6 6h12v12H6z" fill="currentColor"/></svg>
          Emergency Stop
        </button>
        <button class="tile-btn tile-pause" onclick="robotCmd('pause')">
          <svg viewBox="0 0 24 24"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z" fill="currentColor"/></svg>
          Pause
        </button>
        <button class="tile-btn tile-resume" onclick="robotCmd('resume')">
          <svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z" fill="currentColor"/></svg>
          Resume
        </button>
        <button class="tile-btn tile-dock" onclick="robotCmd('dock')">
          <svg viewBox="0 0 24 24"><path d="M10 20v-6h4v6h5v-8h3L12 3 2 12h3v8z" fill="currentColor"/></svg>
          Dock
        </button>
        <button class="tile-btn tile-undock" onclick="robotCmd('undock')">
          <svg viewBox="0 0 24 24"><path d="M9 5v2h6.59L4 18.59 5.41 20 17 8.41V15h2V5z" fill="currentColor"/></svg>
          Undock
        </button>
        <button class="tile-btn tile-trail" onclick="clearTrail()">
          <svg viewBox="0 0 24 24"><path d="M12 2C8.13 2 5 5.13 5 9c0 5.25 7 13 7 13s7-7.75 7-13c0-3.87-3.13-7-7-7zm0 9.5a2.5 2.5 0 010-5 2.5 2.5 0 010 5z" fill="currentColor"/></svg>
          Clear Trail
        </button>
        <button class="tile-btn tile-preview" onclick="clearPreview()">
          <svg viewBox="0 0 24 24"><path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z" fill="currentColor"/></svg>
          Clear Path
        </button>
      </div>
    </div>

    <!-- ── Status Card ── -->
    <div class="ha-card" style="margin-top:auto;">
      <div class="ha-card-header">
        <svg class="header-icon" viewBox="0 0 24 24"><path d="M11 17h2v-6h-2v6zm1-15C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 18c-4.41 0-8-3.59-8-8s3.59-8 8-8 8 3.59 8 8-3.59 8-8 8zM11 9h2V7h-2v2z" fill="currentColor"/></svg>
        Status
      </div>
      <div class="ha-card-content">
        <div class="entity-row">
          <div class="status-icon robot">
            <img src="/api/favicon.png" style="width:20px;height:20px;object-fit:contain;">
          </div>
          <div class="entity-info"><span class="entity-name">Robot</span></div>
          <span class="entity-state" id="st-robot">\u2014</span>
        </div>
        <div class="entity-row">
          <div class="status-icon battery">
            <svg viewBox="0 0 24 24"><path d="M15.67 4H14V2h-4v2H8.33C7.6 4 7 4.6 7 5.33v15.34C7 21.4 7.6 22 8.33 22h7.33c.74 0 1.34-.6 1.34-1.33V5.33C17 4.6 16.4 4 15.67 4z" fill="currentColor"/></svg>
          </div>
          <div class="entity-info"><span class="entity-name">Battery</span></div>
          <span class="entity-state" id="st-battery">\u2014</span>
        </div>
        <div class="entity-row">
          <div class="status-icon calendar">
            <svg viewBox="0 0 24 24"><path d="M19 3h-1V1h-2v2H8V1H6v2H5c-1.11 0-2 .9-2 2v14a2 2 0 002 2h14a2 2 0 002-2V5a2 2 0 00-2-2zm0 16H5V8h14v11z" fill="currentColor"/></svg>
          </div>
          <div class="entity-info"><span class="entity-name">Calendar</span></div>
          <span class="entity-state" id="st-calendar">\u2014</span>
        </div>
        <div class="entity-row">
          <div class="status-icon mqtt">
            <svg viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 17.93c-3.95-.49-7-3.85-7-7.93 0-.62.08-1.21.21-1.79L9 15v1c0 1.1.9 2 2 2v1.93zm6.9-2.54c-.26-.81-1-1.39-1.9-1.39h-1v-3c0-.55-.45-1-1-1H8v-2h2c.55 0 1-.45 1-1V7h2c1.1 0 2-.9 2-2v-.41c2.93 1.19 5 4.06 5 7.41 0 2.08-.8 3.97-2.1 5.39z" fill="currentColor"/></svg>
          </div>
          <div class="entity-info"><span class="entity-name">MQTT</span></div>
          <span class="entity-state" id="st-mqtt">\u2014</span>
        </div>
        <div class="entity-row">
          <div class="status-icon map">
            <svg viewBox="0 0 24 24"><path d="M20.5 3l-.16.03L15 5.1 9 3 3.36 4.9c-.21.07-.36.25-.36.48V20.5a.5.5 0 00.5.5l.16-.03L9 18.9l6 2.1 5.64-1.9c.21-.07.36-.25.36-.48V3.5a.5.5 0 00-.5-.5zM15 19l-6-2.11V5l6 2.11V19z" fill="currentColor"/></svg>
          </div>
          <div class="entity-info"><span class="entity-name">Map Source</span></div>
          <span class="entity-state active">{geo.get("_source", "Unknown")}</span>
        </div>
      </div>
    </div>
  </div>

  <div id="map"></div>
  <div class="toast" id="toast"></div>

  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
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
    var sidewalksLayer = L.layerGroup().addTo(map);
    var chargersLayer = L.layerGroup().addTo(map);
    var markersLayer = L.layerGroup().addTo(map);
    {build_raster_overlay_js(geo)}
    {all_map_js}
    L.control.layers(
      {{"OpenStreetMap": osmLayer, "Satellite": esriSat}},
      {{"Areas": areasLayer, "Pathways": pathwaysLayer, "No-Go": nogoLayer,
       "Snow Piles": snowLayer, "Sidewalks": sidewalksLayer, "Chargers": chargersLayer, "Ref Point": markersLayer}}
    ).addTo(map);
    var trailLine = L.polyline([], {{color:'#00e5ff', weight:3, opacity:0.8, dashArray:'6,4'}}).addTo(map);
    var previewLine = L.polyline([], {{color:'#ff9800', weight:2, opacity:0.7, dashArray:'4,6'}}).addTo(map);
    var allPoints = {all_pts_json};
    if (allPoints.length > 0) map.fitBounds(allPoints, {{padding: [30,30]}});
    var robotMarker = null;
    var yarboIcon = L.icon({{
      iconUrl: '/api/favicon.png',
      iconSize: [32, 32],
      iconAnchor: [16, 16],
      popupAnchor: [0, -18]
    }});
    function showToast(msg, type) {{
      var t = document.getElementById('toast');
      t.textContent = msg; t.className = 'toast ' + (type || '');
      t.style.display = 'block';
      setTimeout(function(){{ t.style.display = 'none'; }}, 3500);
    }}
    function apiPost(path, params) {{
      var url = path;
      if (params) {{ var qs = Object.entries(params).map(function(e){{ return e[0]+'='+e[1]; }}).join('&'); url += '?' + qs; }}
      return fetch(url, {{method:'POST'}}).then(function(r){{ return r.json(); }});
    }}
    function apiGet(path) {{ return fetch(path).then(function(r){{ return r.json(); }}); }}
    function startJob(areaId) {{
      showToast('Starting plan ' + areaId + '...', '');
      apiPost('/api/robot/start_plan', {{plan_id: areaId, percent: 0}})
        .then(function(d) {{
          if (d.ok) showToast('Plan ' + areaId + ' started', 'success');
          else if (d.blocked) showToast('Blocked: ' + d.reason, 'error');
          else showToast(JSON.stringify(d), 'error');
        }}).catch(function(e) {{ showToast('Error: ' + e.message, 'error'); }});
    }}
    function refreshPlans() {{
      var btn = document.getElementById('refreshPlansBtn');
      btn.classList.add('spinning');
      fetch('/api/live/plans?refresh=true')
        .then(function(r) {{ return r.json(); }})
        .then(function(data) {{
          var plans = (data && data.data) ? data.data : [];
          var colors = ['#4fc3f7','#81c784','#ffb74d','#ba68c8','#ef5350','#26c6da'];
          var html = '';
          if (plans.length === 0) {{
            html = '<div class="entity-row"><span class="entity-name" style="color:var(--secondary-text)">No plans available</span></div>';
          }} else {{
            plans.forEach(function(p) {{
              var pid = p.id || 0;
              var pname = (p.name || 'Plan ' + pid).trim();
              var pcolor = colors[(pid - 1) % colors.length];
              var ac = (p.areaIds || []).length;
              var al = ac + ' area' + (ac !== 1 ? 's' : '');
              html += '<div class="entity-row">'
                + '  <div class="entity-dot" style="background:' + pcolor + '"></div>'
                + '  <div class="entity-info">'
                + '    <span class="entity-name">' + pname + '</span>'
                + '    <span class="entity-secondary">' + al + '</span>'
                + '  </div>'
                + '  <button class="preview-btn" onclick="previewPath(' + pid + ')" title="Preview path">'
                + '    <svg width="16" height="16" viewBox="0 0 24 24"><path d="M12 2C8.13 2 5 5.13 5 9c0 5.25 7 13 7 13s7-7.75 7-13c0-3.87-3.13-7-7-7zm0 9.5a2.5 2.5 0 010-5 2.5 2.5 0 010 5z" fill="currentColor"/></svg>'
                + '  </button>'
                + '  <button class="play-btn" onclick="startJob(' + pid + ')" title="Start ' + pname + '">'
                + '    <svg width="18" height="18" viewBox="0 0 24 24"><path d="M8 5v14l11-7z" fill="currentColor"/></svg>'
                + '  </button>'
                + '</div>';
            }});
          }}
          document.getElementById('plansList').innerHTML = html;
          showToast('Plans refreshed (' + plans.length + ')', 'success');
        }})
        .catch(function(e) {{ showToast('Refresh failed: ' + e.message, 'error'); }})
        .finally(function() {{ btn.classList.remove('spinning'); }});
    }}
    function robotCmd(cmd) {{
      showToast('Sending ' + cmd + '...', '');
      apiPost('/api/robot/' + cmd)
        .then(function(d) {{
          if (d.ok) showToast(cmd.charAt(0).toUpperCase() + cmd.slice(1) + ' sent', 'success');
          else showToast(JSON.stringify(d), 'error');
        }}).catch(function(e) {{ showToast('Error: ' + e.message, 'error'); }});
    }}
    function updateStatus() {{
      apiGet('/api/status').then(function(d) {{
        var stEl = document.getElementById('st-robot');
        var state = d.state || d.status || 'Unknown';
        stEl.textContent = state.charAt(0).toUpperCase() + state.slice(1);
        stEl.className = 'entity-state' + (state === 'working' ? ' active' : state === 'error' ? ' error' : '');
        var bat = document.getElementById('st-battery');
        if (d.battery_percent !== undefined) {{
          bat.textContent = d.battery_percent + '%';
          bat.className = 'entity-state' + (d.battery_percent < 20 ? ' error' : ' active');
        }}
        var mqtt = document.getElementById('st-mqtt');
        mqtt.textContent = d.mqtt_connected ? 'Connected' : 'Disconnected';
        mqtt.className = 'entity-state' + (d.mqtt_connected ? ' active' : ' error');
        var summary = document.getElementById('header-summary');
        if (summary) {{
          var parts = [];
          if (state) parts.push(state.charAt(0).toUpperCase() + state.slice(1));
          if (d.battery_percent !== undefined) parts.push(d.battery_percent + '% battery');
          summary.textContent = parts.join(' \u2022 ');
        }}
        if (d.latitude && d.longitude) {{
          if (!robotMarker) {{
            robotMarker = L.marker([d.latitude, d.longitude], {{
              icon: yarboIcon
            }}).addTo(map).bindPopup('Yarbo Robot');
          }} else {{ robotMarker.setLatLng([d.latitude, d.longitude]); }}
        }}
        // Show/hide trail status in header summary
        if (d.on_going_planning) {{
          updateTrail();
        }}
      }}).catch(function(){{}});
      apiGet('/api/calendar/status').then(function(d) {{
        var el = document.getElementById('st-calendar');
        if (d.busy) {{ el.textContent = d.event_summary || 'Busy'; el.className = 'entity-state warning'; }}
        else {{ el.textContent = 'Free'; el.className = 'entity-state active'; }}
      }}).catch(function() {{ document.getElementById('st-calendar').textContent = '\u2014'; }});
    }}
    updateStatus();
    setInterval(updateStatus, 10000);

    function updateTrail() {{
      apiGet('/api/live/trail').then(function(d) {{
        if (d.points && d.points.length > 0) {{
          trailLine.setLatLngs(d.points);
        }}
      }}).catch(function(){{}});
    }}

    function previewPath(planId) {{
      showToast('Requesting plan path...', '');
      fetch('/api/live/preview_path?plan_id=' + planId, {{method:'POST'}})
        .then(function(r) {{ return r.json(); }})
        .then(function(d) {{
          if (d.ok && d.points && d.points.length > 0) {{
            previewLine.setLatLngs(d.points);
            showToast('Plan path: ' + d.count + ' points', 'success');
          }} else if (d.error) {{
            showToast('Preview failed: ' + d.error, 'error');
          }} else {{
            showToast('No path data returned', 'error');
          }}
        }})
        .catch(function(e) {{ showToast('Error: ' + e.message, 'error'); }});
    }}

    function clearTrail() {{
      fetch('/api/live/trail/clear', {{method:'POST'}}).then(function() {{
        trailLine.setLatLngs([]);
        showToast('Trail cleared', 'success');
      }}).catch(function(e) {{ showToast('Error: ' + e.message, 'error'); }});
    }}

    function clearPreview() {{
      previewLine.setLatLngs([]);
      showToast('Preview path cleared', 'success');
    }}
  </script>
</body>
</html>"""
        return HTMLResponse(content=html)

    # ── GeoJSON ──────────────────────────────────────────────────────

    @app.get("/api/map/geojson")
    def get_map_geojson(sn: str = None):
        s = _sn(sn)
        mc = mqtt_ref[0]
        geo = get_map_geometry(s, api, mc)
        features = []
        for area in geo["areas"]:
            coords = [[lon, lat] for lat, lon in area["points"]]
            if coords:
                coords.append(coords[0])
            features.append({"type": "Feature",
                             "properties": {"name": area["name"], "area_sqm": round(area["area_sqm"], 1), "type": "area"},
                             "geometry": {"type": "Polygon", "coordinates": [coords]}})
        for pw in geo["pathways"]:
            coords = [[lon, lat] for lat, lon in pw["points"]]
            features.append({"type": "Feature",
                             "properties": {"name": pw["name"], "type": "pathway"},
                             "geometry": {"type": "LineString", "coordinates": coords}})
        for cp in geo["chargers"]:
            features.append({"type": "Feature",
                             "properties": {"type": "charger", "enabled": cp["enabled"]},
                             "geometry": {"type": "Point", "coordinates": [cp["lon"], cp["lat"]]}})
        for sp in geo.get("snow_piles", []):
            coords = [[lon, lat] for lat, lon in sp["points"]]
            if coords:
                coords.append(coords[0])
            features.append({"type": "Feature",
                             "properties": {"name": sp["name"], "type": "snow_pile"},
                             "geometry": {"type": "Polygon", "coordinates": [coords]}})
        for sw in geo.get("sidewalks", []):
            coords = [[lon, lat] for lat, lon in sw["points"]]
            features.append({"type": "Feature",
                             "properties": {"name": sw["name"], "type": "sidewalk"},
                             "geometry": {"type": "LineString", "coordinates": coords}})
        return {"type": "FeatureCollection", "features": features}

    @app.get("/api/map/background")
    def get_map_background(sn: str = None):
        s = _sn(sn)
        data = api.get_raster_background(s)
        obj = json.loads(data.get("object_data", "{}"))
        return {
            "image_url": data.get("accessUrl"),
            "top_left": obj.get("top_left_real"),
            "center": obj.get("center_real"),
            "bottom_right": obj.get("bottom_right_real"),
            "rotation_rad": obj.get("rad"),
            "last_modified": data.get("gmt_modified"),
        }

    # ── Charging ─────────────────────────────────────────────────────

    @app.get("/api/charging")
    def get_charging(sn: str = None):
        s = _sn(sn)
        map_data = api.get_map(s)
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
                {"id": cp.get("id"), "x": cp.get("chargingPoint", {}).get("x"),
                 "y": cp.get("chargingPoint", {}).get("y"), "enabled": cp.get("enable")}
                for cp in all_cps
            ],
            "docking_station": {
                "name": dc.get("dc_name"), "mac": dc.get("dc_mac"),
                "ip": dc.get("dc_ip_address"),
            },
        }

    # ── Messages ─────────────────────────────────────────────────────

    @app.get("/api/messages")
    def get_messages(sn: str = None, limit: int = 20):
        return api.get_messages(_sn(sn))[:limit]

    @app.get("/api/messages/latest")
    def get_latest_message(sn: str = None):
        msgs = api.get_messages(_sn(sn))
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

    # ── Plan History ─────────────────────────────────────────────────

    @app.get("/api/plans/history")
    def get_plan_history(sn: str = None, limit: int = 50):
        msgs = api.get_messages(_sn(sn))
        plan_events = [enrich_plan_event(m) for m in msgs if is_plan_event(m)]
        return plan_events[:limit]

    @app.get("/api/plans/summary")
    def get_plan_summary(sn: str = None):
        msgs = api.get_messages(_sn(sn))
        plan_events = [enrich_plan_event(m) for m in msgs if is_plan_event(m)]
        total = len(plan_events)
        by_category = {}
        by_code = {}
        for ev in plan_events:
            by_category[ev["category"]] = by_category.get(ev["category"], 0) + 1
            by_code[ev["code"]] = by_code.get(ev["code"], 0) + 1
        return {
            "total_events": total,
            "by_category": by_category,
            "by_code": by_code,
            "last_event": plan_events[0] if plan_events else None,
            "first_event": plan_events[-1] if plan_events else None,
            "note": "Detailed per-run stats (duration, area covered) require MQTT connection to the device",
        }

    # ── Firmware ─────────────────────────────────────────────────────

    @app.get("/api/firmware")
    def get_firmware():
        fw = api.get_firmware()
        return {
            "app_version": fw.get("appVersion"),
            "firmware_version": fw.get("firmwareVersion"),
            "dc_version": fw.get("dcVersion"),
            "firmware_description": fw.get("firmwareDescription", "")[:500],
        }

    @app.get("/api/firmware/dc")
    def get_dc_firmware(sn: str = None):
        return api.get_dc_version(_sn(sn))

    # ── Notifications & Shared Users ─────────────────────────────────

    @app.get("/api/notifications/settings")
    def get_notification_settings():
        return api.get_notification_settings()

    @app.get("/api/shared_users")
    def get_shared_users(sn: str = None):
        return api.get_shared_users(_sn(sn))

    # ── Video Feed (Agora RTC) ───────────────────────────────────────
    # INACTIVE: Video feed not working (OperationError on client.join)
    # See CONTEXT_VIDEO_FEED.md for details. All endpoints commented out.
    #
    # @app.post("/api/video/start")
    # @app.post("/api/video/stop")
    # @app.get("/api/video/token")
    # @app.get("/api/video/cameras")
    # @app.get("/api/video")    # Full HTML page with Agora Web SDK

    # ── Status ───────────────────────────────────────────────────────

    @app.get("/api/status")
    def get_status():
        mc = mqtt_ref[0]
        if mc is None:
            return {"connected": False, "state": "unknown", "error": "MQTT not initialized"}
        result = mc.get()

        dev_msg = mc._device_msg
        if dev_msg:
            bat_msg = dev_msg.get("BatteryMSG", {})
            bat_level = result.get("battery_level")
            if bat_level is None and bat_msg.get("capacity") is not None:
                bat_level = bat_msg["capacity"]
            result["battery_percent"] = bat_level
            voltage_mv = bat_msg.get("voltage")
            result["battery_voltage"] = round(voltage_mv / 1000, 1) if voltage_mv else None
            result["battery_health"] = bat_msg.get("health")
            temps = [bat_msg.get("temperature%d" % i) for i in range(1, 7)
                     if bat_msg.get("temperature%d" % i) is not None]
            result["battery_temp"] = round(sum(temps) / len(temps), 1) if temps else None

            state_msg = dev_msg.get("StateMSG", {})
            working_states = {0: "standby", 1: "idle", 2: "working", 3: "charging",
                              4: "docking", 5: "error", 6: "returning", 7: "paused"}
            ws = state_msg.get("working_state")
            activity = working_states.get(ws, result.get("state", "unknown"))
            if result.get("state", "unknown") != "unknown":
                activity = result["state"]
            result["activity"] = activity
            result["charging"] = state_msg.get("charging_status", 0) > 0
            result["on_going_planning"] = bool(state_msg.get("on_going_planning", 0))
            result["planning_paused"] = bool(state_msg.get("planning_paused", 0))

            if "CombinedOdom" in dev_msg:
                odom = dev_msg["CombinedOdom"]
                try:
                    s = mc.serial or _sn()
                    map_data = api.get_map(s)
                    ref = map_data.get("ref", {}).get("ref", {})
                    ref_lat = ref.get("latitude", 0)
                    ref_lon = ref.get("longitude", 0)
                    if ref_lat and ref_lon:
                        lat, lon = local_to_gps(odom.get("x", 0), odom.get("y", 0), ref_lat, ref_lon)
                        result["latitude"] = lat
                        result["longitude"] = lon
                except Exception:
                    pass

            pos = result.get("position", {})
            if isinstance(pos, dict) and pos.get("latitude"):
                result["latitude"] = pos["latitude"]
                result["longitude"] = pos["longitude"]

        result["mqtt_connected"] = mc.is_connected
        return result

    @app.post("/api/status/update")
    def update_status(topic: str, payload: dict):
        mc = mqtt_ref[0]
        if mc is None:
            raise HTTPException(503, "MQTT not initialized")
        mc.update(topic, payload)
        return {"ok": True}

    # ── Cache Control ────────────────────────────────────────────────

    @app.post("/api/cache/clear")
    def clear_cache():
        cache.invalidate()
        return {"ok": True, "message": "Cache cleared"}

    # ── MQTT Info ────────────────────────────────────────────────────

    @app.get("/api/mqtt")
    def get_mqtt_info():
        mc = mqtt_ref[0]
        status = mc.get() if mc else {"connected": False, "state": "unknown"}
        return {
            "connected": mc.is_connected if mc else False,
            "robot_ip": CONFIG["robot_ip"] or None,
            "robot_wifi_ip": status.get("robot_wifi_ip"),
            "robot_port": CONFIG["robot_mqtt_port"],
            "robot_tls": CONFIG["robot_mqtt_tls"],
            "robot_serial": mc.serial if mc else CONFIG["robot_serial"] or None,
            "wifi_info": status.get("wifi_info"),
            "status": status,
        }

    @app.post("/api/mqtt/reconnect")
    def mqtt_reconnect():
        mc = mqtt_ref[0]
        if mc is None:
            raise HTTPException(503, "MQTT not initialized")
        mc.stop()
        time.sleep(0.5)
        mc.start()
        return {"ok": True, "message": "MQTT reconnection initiated"}

    @app.post("/api/mqtt/discover")
    def mqtt_discover():
        """Scan the network for the robot and reconnect if IP changed."""
        mc = mqtt_ref[0]
        old_ip = CONFIG.get("robot_ip", "")
        port = CONFIG.get("robot_mqtt_port", 8883)

        new_ip = discover_robot(old_ip, port)
        if not new_ip:
            return {
                "ok": False,
                "message": "No robot found on network",
                "old_ip": old_ip,
            }

        if new_ip == old_ip and mc and mc.is_connected:
            return {
                "ok": True,
                "message": f"Robot already connected at {new_ip}",
                "robot_ip": new_ip,
                "changed": False,
            }

        # IP changed or not connected — reconnect
        if mc:
            mc.update_ip(new_ip)
        else:
            CONFIG["robot_ip"] = new_ip

        return {
            "ok": True,
            "message": f"Robot found at {new_ip}" + (
                f" (was {old_ip})" if old_ip and old_ip != new_ip else ""
            ),
            "robot_ip": new_ip,
            "old_ip": old_ip,
            "changed": new_ip != old_ip,
        }

    # ── Robot Commands ───────────────────────────────────────────────

    @app.post("/api/command/{command}")
    def send_command(command: str, payload: dict = None, wait: bool = True, timeout: float = 5.0):
        mc = _mc()
        if payload is None:
            payload = {}
        try:
            resp = mc.send_command(command, payload, wait=wait, timeout=timeout)
            return {"ok": True, "command": command, "response": resp}
        except ConnectionError as e:
            raise HTTPException(503, str(e))
        except RuntimeError as e:
            raise HTTPException(500, str(e))

    @app.post("/api/robot/stop")
    def robot_stop():
        _mc().send_command("stop", {}, wait=False)
        return {"ok": True, "command": "stop"}

    @app.post("/api/robot/pause")
    def robot_pause():
        _mc().send_command("pause", {}, wait=False)
        return {"ok": True, "command": "pause"}

    @app.post("/api/robot/resume")
    def robot_resume():
        _mc().send_command("resume", {}, wait=False)
        return {"ok": True, "command": "resume"}

    @app.post("/api/robot/dock")
    def robot_dock():
        _mc().send_command("cmd_recharge", {"cmd": 2}, wait=False)
        return {"ok": True, "command": "cmd_recharge"}

    # ── Calendar ─────────────────────────────────────────────────────

    @app.get("/api/calendar/status")
    def calendar_status():
        result = check_calendar_busy()
        result["calendar_entity"] = CONFIG["ha_calendar_entity"]
        result["blocking_enabled"] = CONFIG["calendar_block_enabled"]
        return result

    @app.post("/api/robot/start_plan")
    def robot_start_plan(plan_id: int = 1, percent: int = 0, force: bool = False):
        if not force:
            cal = check_calendar_busy()
            if cal["busy"]:
                return {
                    "ok": False, "command": "start_plan", "plan_id": plan_id,
                    "blocked": True,
                    "reason": f"Calendar event active: {cal['event_summary']}",
                    "calendar_entity": CONFIG["ha_calendar_entity"],
                }
        mc = _mc()
        resp = mc.send_command("start_plan", {"id": plan_id, "percent": percent}, timeout=5.0)
        return {"ok": True, "command": "start_plan", "plan_id": plan_id, "response": resp}

    @app.post("/api/robot/shutdown")
    def robot_shutdown():
        _mc().send_command("shutdown", {}, wait=False)
        return {"ok": True, "command": "shutdown"}

    @app.post("/api/robot/drive")
    def robot_drive(vel: float = 0.0, rev: float = 0.0):
        _mc().send_command("cmd_vel", {"vel": vel, "rev": rev}, wait=False)
        return {"ok": True, "command": "cmd_vel", "vel": vel, "rev": rev}

    @app.post("/api/robot/lights")
    def robot_lights(head: int = 0, left: int = 0, right: int = 0,
                     body_left: int = 0, body_right: int = 0,
                     tail_left: int = 0, tail_right: int = 0):
        payload = {
            "led_head": head, "led_left_w": left, "led_right_w": right,
            "body_left_r": body_left, "body_right_r": body_right,
            "tail_left_r": tail_left, "tail_right_r": tail_right,
        }
        _mc().send_command("light_ctrl", payload, wait=False)
        return {"ok": True, "command": "light_ctrl", "payload": payload}

    @app.post("/api/robot/sound")
    def robot_sound(enable: bool = True, vol: float = 0.2, mode: int = 0):
        _mc().send_command("set_sound_param", {"enable": enable, "vol": vol, "mode": mode}, wait=False)
        return {"ok": True, "command": "set_sound_param"}

    # ── Live data from robot ─────────────────────────────────────────

    @app.get("/api/live/map")
    def get_live_map(refresh: bool = False):
        mc = _mc()
        if refresh:
            mc.send_command("get_map", {}, wait=True, timeout=5.0)
        data = mc._live_map
        if data is None:
            raise HTTPException(404, "No live map data yet; robot may not be connected")
        return data

    @app.get("/api/live/plans")
    def get_live_plans(refresh: bool = False):
        mc = _mc()
        if refresh:
            mc.send_command("read_all_plan", {}, wait=True, timeout=5.0)
        data = mc.live_plans
        if data is None:
            raise HTTPException(404, "No live plan data yet")
        return data

    @app.get("/api/live/gps_ref")
    def get_live_gps_ref(refresh: bool = False):
        mc = _mc()
        if refresh:
            mc.send_command("read_gps_ref", {}, wait=True, timeout=5.0)
        data = mc.live_gps_ref
        if data is None:
            raise HTTPException(404, "No live GPS ref data yet")
        return data

    @app.get("/api/live/schedules")
    def get_live_schedules(refresh: bool = False):
        mc = _mc()
        if refresh:
            mc.send_command("read_schedules", {}, wait=True, timeout=5.0)
        data = mc.live_schedules
        if data is None:
            raise HTTPException(404, "No schedule data yet")
        return data

    @app.get("/api/live/params")
    def get_live_params(refresh: bool = False):
        mc = _mc()
        if refresh:
            mc.send_command("read_global_params", {"id": 1}, wait=True, timeout=5.0)
        data = mc.live_global_params
        if data is None:
            raise HTTPException(404, "No params data yet")
        return data

    @app.get("/api/live/trail")
    def get_trail():
        """Return the breadcrumb trail of positions recorded during active plans."""
        mc = _mc()
        trail = mc.trail
        # Convert local x/y to GPS lat/lon
        ref = None
        try:
            map_data = api.get_map(mc.serial or _sn())
            ref = map_data.get("ref", {}).get("ref", {})
        except Exception:
            pass
        if ref and ref.get("latitude") and ref.get("longitude"):
            ref_lat = ref["latitude"]
            ref_lon = ref["longitude"]
            points = []
            for pt in trail:
                lat, lon = local_to_gps(pt["x"], pt["y"], ref_lat, ref_lon)
                points.append([lat, lon])
        else:
            points = [[pt["x"], pt["y"]] for pt in trail]
        return {
            "active": mc.trail_active,
            "count": len(points),
            "points": points,
        }

    @app.post("/api/live/trail/clear")
    def clear_trail():
        mc = _mc()
        mc.clear_trail()
        return {"ok": True, "message": "Trail cleared"}

    @app.post("/api/live/preview_path")
    def preview_plan_path(plan_id: int):
        """Request the planned path for a given plan. Robot must be in the area."""
        mc = _mc()
        mc._preview_plan_path = None  # clear old data
        mc.send_command("preview_plan_path", {"id": plan_id}, wait=True, timeout=8.0)
        data = mc.preview_plan_path
        if data is None:
            raise HTTPException(504, "No response from robot (timeout)")
        if isinstance(data, dict) and data.get("state", 0) < 0:
            msg = data.get("msg", "Unknown error")
            return {"ok": False, "error": msg, "data": data}
        # Convert path points to GPS
        path_pts = data.get("data", data.get("path", []))
        ref = None
        try:
            map_data = api.get_map(mc.serial or _sn())
            ref = map_data.get("ref", {}).get("ref", {})
        except Exception:
            pass
        if ref and ref.get("latitude") and ref.get("longitude") and isinstance(path_pts, list):
            ref_lat = ref["latitude"]
            ref_lon = ref["longitude"]
            gps_path = []
            for pt in path_pts:
                if isinstance(pt, dict):
                    lat, lon = local_to_gps(pt.get("x", 0), pt.get("y", 0), ref_lat, ref_lon)
                elif isinstance(pt, (list, tuple)) and len(pt) >= 2:
                    lat, lon = local_to_gps(pt[0], pt[1], ref_lat, ref_lon)
                else:
                    continue
                gps_path.append([lat, lon])
            return {"ok": True, "count": len(gps_path), "points": gps_path}
        return {"ok": True, "count": len(path_pts), "points": path_pts, "raw": True}

    # ── HA Sensors ───────────────────────────────────────────────────

    @app.get("/api/ha/sensors")
    def ha_sensors(sn: str = None):
        s = _sn(sn)
        mc = mqtt_ref[0]
        device = api.get_devices()[0] if api.get_devices() else {}
        fw = api.get_firmware()
        msgs = api.get_messages(s)
        map_data = api.get_map(s)
        geo = get_map_geometry(s, api, mc)
        status = mc.get() if mc else {}

        head_types = {0: "mower", 1: "snow_blower", 2: "blower", 3: "trimmer"}
        ref = map_data.get("ref", {}).get("ref", {})
        dc = map_data.get("dc", {})
        areas = map_data.get("area", [])

        latest_msg = msgs[0] if msgs else {}
        plan_events = [enrich_plan_event(m) for m in msgs if is_plan_event(m)]

        # Battery: prefer MQTT batteryInfo, fall back to device_msg BatteryMSG
        battery_level = None
        battery_voltage = None
        battery_health = None
        battery_temp = None
        bat = status.get("battery")
        if isinstance(bat, dict) and bat.get("level") is not None:
            battery_level = bat["level"]
        elif isinstance(bat, (int, float)):
            battery_level = bat
        dev_msg = mc._device_msg if mc else None
        if dev_msg:
            bat_msg = dev_msg.get("BatteryMSG", {})
            if battery_level is None and bat_msg.get("capacity") is not None:
                battery_level = bat_msg["capacity"]
            battery_voltage = bat_msg.get("voltage")
            battery_health = bat_msg.get("health")
            temps = [bat_msg.get(f"temperature{i}") for i in range(1, 7)
                     if bat_msg.get(f"temperature{i}") is not None]
            battery_temp = round(sum(temps) / len(temps), 1) if temps else None

        state_msg = dev_msg.get("StateMSG", {}) if dev_msg else {}
        working_states = {0: "standby", 1: "idle", 2: "working", 3: "charging",
                          4: "docking", 5: "error", 6: "returning", 7: "paused"}
        working_state_code = state_msg.get("working_state")
        activity = working_states.get(working_state_code, status.get("state", "unknown"))
        on_going_planning = bool(state_msg.get("on_going_planning", 0))
        planning_paused = bool(state_msg.get("planning_paused", 0))
        charging_status = state_msg.get("charging_status", 0)
        schedule_id = state_msg.get("schedule_id", -1)
        schedule_msg = state_msg.get("schedule_msg", "")
        plan_msg = state_msg.get("plan_msg", "")
        error_code = state_msg.get("error_code", 0)

        mqtt_state = status.get("state", "unknown")
        if mqtt_state != "unknown":
            activity = mqtt_state

        robot_lat = None
        robot_lon = None
        if dev_msg and "CombinedOdom" in dev_msg:
            odom = dev_msg["CombinedOdom"]
            map_ref_lat = ref.get("latitude", 0)
            map_ref_lon = ref.get("longitude", 0)
            if map_ref_lat and map_ref_lon:
                robot_lat, robot_lon = local_to_gps(odom.get("x", 0), odom.get("y", 0),
                                                     map_ref_lat, map_ref_lon)
        pos = status.get("position", {})
        if isinstance(pos, dict) and pos.get("latitude"):
            robot_lat = pos["latitude"]
            robot_lon = pos["longitude"]

        dc_name = dc.get("dc_name", "")
        if not dc_name and dev_msg:
            dc_name = dev_msg.get("base_name", "")

        return {
            "serial_number": device.get("serialNum", s),
            "device_name": device.get("deviceNickname", "Yarbo"),
            "head_type": head_types.get(device.get("headType", -1), "unknown"),
            "state": mqtt_state,
            "activity": activity,
            "connected": status.get("connected", False),
            "last_heartbeat": status.get("last_heartbeat"),
            "battery_level": battery_level,
            "battery_voltage": round(battery_voltage / 1000, 1) if battery_voltage else None,
            "battery_health": battery_health,
            "battery_temp": battery_temp,
            "on_going_planning": on_going_planning,
            "planning_paused": planning_paused,
            "charging": charging_status > 0,
            "schedule_id": schedule_id if schedule_id >= 0 else None,
            "schedule_msg": schedule_msg or None,
            "plan_msg": plan_msg or None,
            "error_code": error_code if error_code else None,
            "robot_latitude": robot_lat,
            "robot_longitude": robot_lon,
            "gps_latitude": ref.get("latitude"),
            "gps_longitude": ref.get("longitude"),
            "total_area_sq_meters": round(sum(a.get("area_sqm", 0) for a in geo["areas"]), 1),
            "area_count": len(geo["areas"]),
            "pathway_count": len(geo["pathways"]),
            "docking_station": dc_name,
            "firmware_version": fw.get("firmwareVersion", ""),
            "app_version": fw.get("appVersion", ""),
            "dc_version": fw.get("dcVersion", ""),
            "last_message_title": latest_msg.get("msgTitle", ""),
            "last_message_code": latest_msg.get("errCode", ""),
            "last_message_time": latest_msg.get("gmtCreate"),
            "last_plan_code": plan_events[0]["code"] if plan_events else "",
            "last_plan_description": plan_events[0]["description"] if plan_events else "",
            "last_plan_time": plan_events[0]["timestamp"] if plan_events else None,
            "plan_events_total": len(plan_events),
            "bridge_version": "1.0.0",
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }

    # ──────────────────────────────────────────────────────────────────────────
    # HA: Control Commands Feed
    # ──────────────────────────────────────────────────────────────────────────
    @app.get("/api/ha/control_commands")
    async def ha_control_commands():
        """
        Return recent control commands for Home Assistant display.
        Shows manual joystick movements, roller control, state changes, etc.
        """
        mc = mqtt_ref[0]
        if not mc:
            return {"commands": [], "count": 0}
        
        commands = mc.control_commands
        
        # Add friendly descriptions to commands
        friendly_commands = []
        for cmd in commands:
            cmd_name = cmd.get("command", "")
            payload = cmd.get("payload", {})
            
            # Generate friendly description
            description = ""
            if cmd_name == "cmd_vel":
                vel = payload.get("vel", 0)
                rev = payload.get("rev", 0)
                if vel == 0 and rev == 0:
                    description = "Stop"
                elif vel > 0:
                    description = f"Forward {vel} m/s"
                elif vel < 0:
                    description = f"Backward {abs(vel)} m/s"
                if rev != 0:
                    direction = "right" if rev > 0 else "left"
                    description += f", turn {direction}"
            elif cmd_name == "cmd_roller":
                vel = payload.get("vel", 0)
                description = f"Roller speed: {vel} RPM"
            elif cmd_name == "set_working_state":
                state = payload.get("state", 0)
                state_map = {0: "Standby", 1: "Idle", 2: "Working", 3: "Charging",
                           4: "Docking", 5: "Error", 6: "Returning", 7: "Paused"}
                description = f"Change state to: {state_map.get(state, 'Unknown')}"
            elif cmd_name == "set_plan_roller":
                state = payload.get("state", 0)
                description = f"Enable roller for plan (state: {state})"
            else:
                description = cmd_name.replace("_", " ").title()
            
            friendly_commands.append({
                "timestamp": cmd.get("timestamp"),
                "command": cmd_name,
                "description": description,
                "payload": payload,
            })
        
        return {
            "commands": friendly_commands,
            "count": len(friendly_commands),
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }

