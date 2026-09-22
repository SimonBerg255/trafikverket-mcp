## trafikverket-mcp

| Field | Value |
|---|---|
| Purpose | Live Swedish road weather, traffic cameras, traffic situations, traffic flow and road conditions from Trafikverket Open Data |
| Auth | `api_key` – the user's **Trafikverket** key, pasted in Intric, sent as `Authorization: Bearer <key>` and forwarded upstream per request (shared or per-connection). Optional `TRAFIKVERKET_API_KEY` env as fallback for auth mode `none`. |
| Ingress path | `/trafikverket/mcp` → app serves `/mcp` (prefix stripped by ingress rewrite) |
| Health | `GET /health` → `{"status":"ok"}` |
| Env vars | all optional: `TRAFIKVERKET_API_KEY` (fallback secret), `ALLOWED_IPS`, `MCP_ICON_URL` |
| Tools (12) | get_weather_station, list_weather_stations, get_weather_stations_near_location, get_weather_observations, get_cameras, get_cameras_near_location, get_camera_image, get_traffic_situations, get_traffic_situation_summary, get_traffic_flow, get_road_conditions, list_counties |
| Resources | none |
| Unit tests | `tests/test_unit.py` (offline), `test_tools.py` (live) |
| Deps | standard set (fastmcp 3.4.4, mcp 1.28.1, fastapi 0.138.2, uvicorn 0.49.0, pydantic 2.13.4, httpx) – no extra third-party deps |
| Secrets | none required; optional `trafikverket-mcp-secrets` for a shared fallback key |

Quirks:
* A request with no Bearer token and no server fallback key returns a clear `Error: No Trafikverket API key available…` text, never a 401 – so Intric's tool list still loads and the user sees what to fix.
* Trafikverket returns errors as `RESPONSE.RESULT[0].ERROR` with HTTP 200 (e.g. `Security: Invalid authentication`); the client translates these into tool-level `Error: …` text.
* Weather stations and cameras are cached in-process for 5 min; a fresh pod does one ~500 KB fetch per dataset on first use.
* `get_camera_image` returns MCP image content (JPEG); every other tool returns text.
* Upstream requests use an XML body posted to the JSON endpoint; `<INCLUDE>` lists keep payloads small.
* Schema changes on Trafikverket's side (dataset `schemaversion`) are centralised in `trafikverket_client.SCHEMA`.
