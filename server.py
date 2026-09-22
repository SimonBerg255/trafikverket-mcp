"""
server.py – Trafikverket Open Data MCP server for Intric.

Run:
    uvicorn server:app --host 0.0.0.0 --port 8000
MCP endpoint:   http://<host>:8000/mcp      (paste "<public-url>/mcp" into Intric)
Health:         http://<host>:8000/health

Environment (.env):
    TRAFIKVERKET_API_KEY      – free key from https://data.trafikverket.se/   (required)
    MCP_SERVER_JWT_SECRET     – HS256 shared secret, min 32 chars              (required)
    MCP_SERVER_JWT_ISSUER     – default "intric-mcp"
    MCP_SERVER_JWT_AUDIENCE   – default "intric-client"
    ALLOWED_IPS               – comma-separated allowlist, default "*"
"""

import os

from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import JWTVerifier
from mcp.server.fastmcp import Icon
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

load_dotenv()

from tools_trafikverket import (  # noqa: E402
    get_camera_image,
    get_cameras,
    get_cameras_near_location,
    get_road_conditions,
    get_traffic_flow,
    get_traffic_situation_summary,
    get_traffic_situations,
    get_weather_observations,
    get_weather_station,
    get_weather_stations_near_location,
    list_counties,
    list_weather_stations,
)

####### CONFIG VALIDATION #######

_jwt_secret = os.getenv("MCP_SERVER_JWT_SECRET", "")
if len(_jwt_secret) < 32:
    raise RuntimeError(
        "MCP_SERVER_JWT_SECRET must be set and at least 32 characters. "
        "Generate one with: python3 -c \"import secrets; print(secrets.token_hex(32))\""
    )
if not os.getenv("TRAFIKVERKET_API_KEY"):
    raise RuntimeError(
        "TRAFIKVERKET_API_KEY is not set. Register for a free key at https://data.trafikverket.se/"
    )

####### AUTH – HS256 JWT (Intric 'Api Key' field) #######

verifier = JWTVerifier(
    public_key=_jwt_secret,
    issuer=os.getenv("MCP_SERVER_JWT_ISSUER", "intric-mcp"),
    audience=os.getenv("MCP_SERVER_JWT_AUDIENCE", "intric-client"),
    algorithm="HS256",
)

####### CUSTOM MIDDLEWARE – IP allowlist #######


class IPAllowlistMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, allowed_ips: list[str]):
        super().__init__(app)
        self.allowed_ips = {ip.strip() for ip in allowed_ips if ip.strip()}
        self.allow_all = "*" in self.allowed_ips

    async def dispatch(self, request, call_next):
        if self.allow_all or request.url.path == "/health":
            return await call_next(request)
        client_ip = request.client.host if request.client else None
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            client_ip = forwarded.split(",")[0].strip()
        if client_ip not in self.allowed_ips:
            return JSONResponse(status_code=403, content={"error": "Forbidden", "your_ip": client_ip})
        return await call_next(request)


ALLOWED_IPS = os.getenv("ALLOWED_IPS", "*").split(",")
middleware = [Middleware(IPAllowlistMiddleware, allowed_ips=ALLOWED_IPS)]

####### SERVER METADATA #######

icon = Icon(
    src=os.getenv(
        "MCP_ICON_URL",
        "https://raw.githubusercontent.com/SimonBerg255/trafikverket-mcp/main/icon.png",
    ),
)

INSTRUCTION_STRING = """
You are connected to Trafikverket Open Data – live Swedish road weather, traffic cameras,
traffic situations (accidents, road works, closures), traffic flow and road surface conditions
from the Swedish Transport Administration (Trafikverket). All data is real-time and covers Sweden only.

## When to use which tool

**get_weather_station** — USE FIRST when:
  - User asks about current temperature, wind, rain/snow, ice or road surface at a named
    place, road or station ("vädret vid Gävle på E4", "is it icy near Kiruna?")
  → No match: call list_weather_stations with a shorter pattern, or
    get_weather_stations_near_location with the place's coordinates.
  → For history/trend: get_weather_observations with the numeric station ID.

**get_weather_stations_near_location** — USE when:
  - User gives a place but no station name; convert the place to WGS84 coordinates yourself
    (Stockholm 59.33,18.07 · Göteborg 57.71,11.97 · Malmö 55.60,13.00 · Umeå 63.83,20.26 ·
    Luleå 65.58,22.15 · Kiruna 67.86,20.23 · Sundsvall 62.39,17.31 · Östersund 63.18,14.64)

**list_weather_stations** — USE when browsing station names or looking up a station ID.

**get_weather_observations** — USE when the user wants the last hours of readings (trend)
  at one station. Needs the numeric station ID.

**get_cameras** / **get_cameras_near_location** — USE when the user asks for a traffic camera
  by name/road/county, or near a place.
  → Show the PhotoUrl as a markdown image: ![camera name](PhotoUrl)
  → Call get_camera_image ONLY if the user explicitly asks you to fetch or analyse the picture.

**get_traffic_situations** — USE when the user asks about accidents, road works, closures,
  obstacles or restrictions on a road or in a county ("olycka på E4?", "vägarbeten i Skåne").
  Always pass county when the user mentions a region. road_number examples: "E4", "E20", "73".

**get_traffic_situation_summary** — USE for counts and overviews ("how many road works in
  Sweden?", "which county has the most incidents?"). Aggregation is done server-side.

**get_traffic_flow** — USE for congestion / speeds ("is E4 in Stockholm slow right now?").
  Requires a county number or coordinates. Rows are per measurement site (SiteId + coordinates).

**get_road_conditions** — USE for reported road surface state (halka, snö, is, blöt vägbana)
  on a road or in a county. Mostly populated in winter; if empty, fall back to get_weather_station
  which has measured surface temperature and ice/snow flags.

**list_counties** — USE only if unsure of a county number. Never guess county numbers.

## Swedish county numbers (länskod)
1 Stockholm · 3 Uppsala · 4 Södermanland · 5 Östergötland · 6 Jönköping · 7 Kronoberg ·
8 Kalmar · 9 Gotland · 10 Blekinge · 12 Skåne · 13 Halland · 14 Västra Götaland ·
17 Värmland · 18 Örebro · 19 Västmanland · 20 Dalarna · 21 Gävleborg · 22 Västernorrland ·
23 Jämtland · 24 Västerbotten · 25 Norrbotten

## Tips
- Station and camera names are Swedish; search with Swedish place names ("Göteborg" not "Gothenburg").
- Times are ISO 8601 in Swedish local time (CET/CEST). Say when a reading was observed.
- Answer in the user's language. Data comes from Trafikverket and should be attributed as such.
- Every tool returns at most 50 rows (default 20). Narrow the filters rather than paginating.

## What this server CANNOT do
- No forecasts – only current observations and short history (Trafikverket is not SMHI).
- No train, ferry or travel-time data.
- No routing or navigation; coordinates must be supplied by you.
Data source: https://data.trafikverket.se/ | License: CC0 (Trafikverket Open Data)
""".strip()

VERSION = "1.0.0"
WEBSITE_URL = "https://data.trafikverket.se/"

####### SERVER #######

mcp = FastMCP(
    name="Trafikverket Open Data",
    instructions=INSTRUCTION_STRING,
    version=VERSION,
    website_url=WEBSITE_URL,
    icons=[icon],
    auth=verifier,
)

####### TOOLS – all run without user confirmation in Intric #######

for _tool in (
    get_weather_station,
    list_weather_stations,
    get_weather_stations_near_location,
    get_weather_observations,
    get_cameras,
    get_cameras_near_location,
    get_camera_image,
    get_traffic_situations,
    get_traffic_situation_summary,
    get_traffic_flow,
    get_road_conditions,
    list_counties,
):
    mcp.tool(meta={"requires_permission": False})(_tool)

####### HEALTH #######


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "trafikverket-mcp", "version": VERSION})


####### ASGI APP – MCP endpoint at /mcp #######

app = mcp.http_app(middleware=middleware)
