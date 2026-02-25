"""
Yarbo Bridge internal package.

Provides modular components for the Yarbo Bridge REST API:
  - config: Configuration and logging
  - auth: Auth0 token management
  - cache: TTL-based response caching
  - api_client: Yarbo cloud REST API wrapper
  - mqtt_client: Robot local MQTT client
  - map_utils: Coordinate conversion, map geometry, plan events, calendar
  - routes: FastAPI route handlers
  - views: HTML template endpoints (Leaflet maps, dashboard, video)
  - routes: FastAPI REST endpoint definitions
"""
