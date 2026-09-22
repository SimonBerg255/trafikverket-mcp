# Trafikverket Open Data MCP Server (Intric edition)

Live Swedish road weather, traffic cameras, traffic situations (accidents, road works,
closures), traffic flow and road surface conditions from **Trafikverket** – packaged as an
MCP server that plugs straight into **Intric**.

Ported and extended from [hniska/trafikverket-mcp](https://github.com/hniska/trafikverket-mcp)
(TypeScript, stdio) to the Intric MCP conventions: Python + FastMCP, HTTP transport at `/mcp`,
Intric `api_key` auth mode (the Trafikverket key is pasted in Intric and forwarded per request),
IP allowlist, `/health`, tools that run without confirmation prompts.

## Tools

| Tool | What it answers |
|---|---|
| `get_weather_station` | Current air/road temperature, wind, precipitation, ice/snow/water at a named station |
| `list_weather_stations` | Browse the ~850 stations, get station IDs |
| `get_weather_stations_near_location` | Stations within N km of a coordinate, with current readings |
| `get_weather_observations` | Last N observations (10-min series) for one station |
| `get_cameras` | Traffic cameras by name / location text / county, with photo URLs |
| `get_cameras_near_location` | Cameras within N km of a coordinate |
| `get_camera_image` | Fetch the JPEG from a camera (returns MCP image content) |
| `get_traffic_situations` | Accidents, road works, closures, obstacles – by road, county, type |
| `get_traffic_situation_summary` | Server-side counts by type / county / severity + most severe items |
| `get_traffic_flow` | Vehicles per hour and average speed at measurement sites (county or coordinates) |
| `get_road_conditions` | Reported road surface state (halka, snö, is) by road / county |
| `list_counties` | Swedish county numbers (länskod) |

Every tool returns readable text (never raw JSON), caps output at 20 rows by default (max 50),
and turns API failures into a plain `Error: …` message.

## Quick start (local)

```bash
python3.12 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# for local testing put your free Trafikverket key in TRAFIKVERKET_API_KEY (https://data.trafikverket.se/)

python3 test_tools.py            # live verification against Trafikverket – must print ALL PASSED
pytest -q                        # offline unit tests

python server.py                 # or: uvicorn server:app --host 0.0.0.0 --port 8000
curl http://localhost:8000/health   # {"status":"ok",...}
```

## Connect to Intric

1. Deploy (Kubernetes via `kustomize/trafikverket-mcp/`, Railway via `railway.json`, or the `Dockerfile`).
   **No environment variables are required.**
2. In Intric: **Settings → MCP servers → Add**
   * URL: `https://<your-public-host>/mcp`  (the path **must** end with `/mcp`)
   * Auth mode: **API key**
   * API key: your **Trafikverket** API key (free at https://data.trafikverket.se/)
3. Ask: *"Hur är vädret på E4 vid Uppsala just nu?"* – `get_weather_station` should run without a prompt.

**How auth works.** Intric sends the API key you pasted as `Authorization: Bearer <key>` on every
call. The server forwards that key to Trafikverket, so the Trafikverket key *is* the credential and
nothing secret has to be stored on the server. Anyone calling the server without a valid Trafikverket
key gets a clear error and no data. If you prefer a shared key for all callers, set
`TRAFIKVERKET_API_KEY` on the server and use auth mode **None** in Intric instead.

## Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `TRAFIKVERKET_API_KEY` | no | – | Shared fallback key, used only when a request carries no Bearer token |
| `ALLOWED_IPS` | no | `*` | Comma-separated allowlist (honours `X-Forwarded-For`) |
| `MCP_ICON_URL` | no | GitHub raw URL of `icon.png` | Icon shown in Intric – must be an absolute public URL |

## Project layout

```
server.py                 FastMCP app: IP allowlist, metadata, /health, /mcp (key forwarded per request)
tools_trafikverket.py     12 tool implementations (formatted text, row caps, decision-tree docstrings)
trafikverket_client.py    XML query builder + JSON response handling + TTL cache + geo helpers
test_tools.py             Live verification runner (exit 0 = done)
tests/test_unit.py        Offline pytest suite
kustomize/                Deployment / Service / Ingress / secrets template for the mcp namespace
Dockerfile, railway.json, Procfile, runtime.txt
CATALOGUE.md              Entry for the mcp-collection catalogue
```

## Data notes

* API: `POST https://api.trafikinfo.trafikverket.se/v2/data.json` with an XML query.
  Errors come back as `RESPONSE.RESULT[0].ERROR` even with HTTP 200 – the client checks that first.
* Weather stations and cameras are cached in memory for 5 minutes (one fetch each, ~850 / ~1000 rows)
  and searched client-side, so name search is robust and fast.
* Situations, traffic flow and road conditions are fetched live with API-side county filters;
  road-number matching is normalised (`E4` = `E 4`, `Väg 73` = `73`). Suspended and already-ended
  deviations are dropped; future-dated ones are tagged PLANNED.
* Situation and Camera queries must carry a `namespace` attribute (`Road.TrafficInfo`,
  `Road.Infrastructure`) or the API reports the object type as non-existent – handled in the client.
* Coordinates are WGS84 `lat, lon`. The tools reject coordinates outside Sweden.
* Dataset versions (Situation 1.6, TrafficFlow 1.5, RoadCondition 1.2, Camera 1.1, Weather 2.1) live in
  `trafikverket_client.SCHEMA`; valid versions can be checked without a key at
  `https://data.trafikverket.se/apb/prod/schema?endpoint=data`.
* License: Trafikverket Open Data (CC0). Attribute Trafikverket as the source.
