"""
tools_trafikverket.py – MCP tool implementations for Trafikverket Open API.

Every tool returns formatted text (never raw JSON), caps rows at MAX_ROWS,
and converts API failures into an "Error: ..." string so Intric can show it.
"""

from __future__ import annotations

import re
from datetime import datetime

from fastmcp.exceptions import ToolError
from fastmcp.utilities.types import Image
import httpx

from trafikverket_client import (
    COUNTIES,
    TrafikverketError,
    cached,
    county_list,
    county_name,
    f_and,
    f_eq,
    first,
    get,
    haversine_km,
    parse_point,
    query,
)

DEFAULT_ROWS = 20
MAX_ROWS = 50
STATION_CACHE_TTL = 300  # seconds – weather stations refresh every 10 min upstream
CAMERA_CACHE_TTL = 300
SITUATION_FETCH_CAP = 10000
ROADCONDITION_FETCH_CAP = 3000
TRAFFICFLOW_FETCH_CAP = 3000

# ── Field lists (INCLUDE) ────────────────────────────────────────────

WEATHER_FIELDS = [
    "Id",
    "Name",
    "Geometry.WGS84",
    "ModifiedTime",
    "Observation.Sample",
    "Observation.Air.Temperature.Value",
    "Observation.Air.RelativeHumidity.Value",
    "Observation.Air.Dewpoint.Value",
    "Observation.Air.VisibleDistance.Value",
    "Observation.Surface.Temperature.Value",
    "Observation.Surface.Ice",
    "Observation.Surface.Snow",
    "Observation.Surface.Water",
    "Observation.Surface.IceDepth.Value",
    "Observation.Surface.SnowDepth.Solid.Value",
    "Observation.Surface.WaterDepth.Value",
    "Observation.Surface.Grip.Value",
    "Observation.Wind.Speed.Value",
    "Observation.Wind.Direction.Value",
    "Observation.Wind.Height",
    "Observation.Weather.Precipitation",
    "Observation.Aggregated30minutes.Wind.SpeedMax.Value",
    "Observation.Aggregated30minutes.Precipitation.TotalWaterEquivalent.Value",
    "Observation.Aggregated30minutes.Precipitation.Rain",
    "Observation.Aggregated30minutes.Precipitation.Snow",
]

OBSERVATION_FIELDS = [
    "Id",
    "Sample",
    "Measurepoint.Id",
    "Measurepoint.Name",
    "Air.Temperature.Value",
    "Air.RelativeHumidity.Value",
    "Air.Dewpoint.Value",
    "Surface.Temperature.Value",
    "Surface.Ice",
    "Surface.Snow",
    "Surface.Water",
    "Wind.Speed.Value",
    "Wind.Direction.Value",
    "Weather.Precipitation",
    "Aggregated30minutes.Precipitation.TotalWaterEquivalent.Value",
    "Aggregated30minutes.Wind.SpeedMax.Value",
]

CAMERA_FIELDS = [
    "Id",
    "Name",
    "Type",
    "Description",
    "Direction",
    "Location",
    "PhotoUrl",
    "PhotoUrlFullsize",
    "PhotoUrlThumbnail",
    "PhotoTime",
    "HasFullSizePhoto",
    "CameraGroup",
    "Active",
    "Status",
    "CountyNo",
    "Geometry.WGS84",
    "ModifiedTime",
]

SITUATION_FIELDS = [
    "Id",
    "ModifiedTime",
    "Deviation.Id",
    "Deviation.Header",
    "Deviation.Message",
    "Deviation.MessageType",
    "Deviation.MessageCode",
    "Deviation.SeverityCode",
    "Deviation.SeverityText",
    "Deviation.RoadNumber",
    "Deviation.RoadNumberNumeric",
    "Deviation.RoadName",
    "Deviation.LocationDescriptor",
    "Deviation.StartTime",
    "Deviation.EndTime",
    "Deviation.CountyNo",
    "Deviation.TrafficRestrictionType",
    "Deviation.Suspended",
    "Deviation.Geometry.Point.WGS84",
]

TRAFFICFLOW_FIELDS = [
    "SiteId",
    "MeasurementTime",
    "MeasurementOrCalculationPeriod",
    "VehicleFlowRate",
    "AverageVehicleSpeed",
    "VehicleType",
    "SpecificLane",
    "MeasurementSide",
    "CountyNo",
    "RegionId",
    "DataQuality",
    "Geometry.WGS84",
]

ROADCONDITION_FIELDS = [
    "Id",
    "RoadNumber",
    "RoadNumberNumeric",
    "LocationText",
    "CountyNo",
    "ConditionCode",
    "ConditionText",
    "ConditionInfo",
    "Cause",
    "Warning",
    "Measurement",
    "StartTime",
    "EndTime",
    "ModifiedTime",
]


# ── Generic helpers ──────────────────────────────────────────────────


def _clamp(limit: int | None, default: int = DEFAULT_ROWS) -> int:
    try:
        n = int(limit) if limit is not None else default
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, MAX_ROWS))


def _err(e: Exception) -> str:
    if isinstance(e, TrafikverketError):
        return f"Error: {e}"
    return f"Error: unexpected failure ({type(e).__name__}: {e})"


def _num(v, digits: int = 1, unit: str = "") -> str:
    try:
        return f"{float(v):.{digits}f}{unit}"
    except (TypeError, ValueError):
        return "–"


def _yesno(v) -> str:
    if v is None:
        return "–"
    return "yes" if v else "no"


def _time(v) -> str:
    if not v:
        return "–"
    s = str(v)
    # 2026-09-22T18:10:00.000+02:00 → 2026-09-22 18:10 (+02:00)
    m = re.match(r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})(?::\d{2}(?:\.\d+)?)?(Z|[+-]\d{2}:\d{2})?", s)
    if not m:
        return s
    tz = m.group(3) or ""
    return f"{m.group(1)} {m.group(2)}{(' ' + tz) if tz else ''}"


def _coords(wkt) -> str:
    p = parse_point(wkt)
    return f"{p[0]:.4f}, {p[1]:.4f}" if p else "–"


_WIND_DIRS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def _wind_dir(deg) -> str:
    try:
        d = float(deg)
    except (TypeError, ValueError):
        return "–"
    return f"{_WIND_DIRS[int(round(d / 22.5)) % 16]} ({d:.0f}°)"


def _pattern_to_regex(pattern: str) -> re.Pattern:
    """'%' / '*' wildcards → regex, otherwise case-insensitive substring."""
    p = pattern.strip()
    if "%" in p or "*" in p:
        rx = "".join(".*" if ch in "%*" else re.escape(ch) for ch in p)
        return re.compile(f"^{rx}$", re.IGNORECASE)
    return re.compile(re.escape(p), re.IGNORECASE)


def _norm_road(value: str | None) -> str:
    """Normalise road labels so 'E4', 'E 4', 'väg 73', 'Väg 73', '73' compare equal."""
    if not value:
        return ""
    s = str(value).lower().strip()
    s = re.sub(r"^(väg|vag|road|riksväg|rv|länsväg|lv)\s*", "", s)
    s = re.sub(r"[\s\-\.]", "", s)
    return s


def _road_matches(row_road: str | None, wanted: str) -> bool:
    return bool(wanted) and _norm_road(row_road) == _norm_road(wanted)


def _validate_county(county) -> int | None:
    if county is None:
        return None
    try:
        c = int(county)
    except (TypeError, ValueError):
        raise TrafikverketError(f"county must be a Swedish county number (1–25), got {county!r}")
    if c not in COUNTIES:
        valid = ", ".join(f"{k}={v}" for k, v in COUNTIES.items())
        raise TrafikverketError(f"Unknown county number {c}. Valid: {valid}")
    return c


def _validate_latlon(lat, lon) -> tuple[float, float]:
    try:
        la, lo = float(lat), float(lon)
    except (TypeError, ValueError):
        raise TrafikverketError("latitude and longitude must be decimal numbers (WGS84)")
    if not (-90 <= la <= 90 and -180 <= lo <= 180):
        raise TrafikverketError("latitude must be -90..90 and longitude -180..180")
    if not (54 <= la <= 70 and 9 <= lo <= 26):
        raise TrafikverketError(
            f"Coordinates ({la}, {lo}) are outside Sweden. Trafikverket only has data for Sweden "
            "(lat 55–69, lon 10–24). Did you swap latitude and longitude?"
        )
    return la, lo


# ── Weather stations ─────────────────────────────────────────────────


async def _all_stations() -> list[dict]:
    async def fetch():
        rows = await query("WeatherMeasurepoint", WEATHER_FIELDS, f_eq("Deleted", False), orderby="Name")
        return rows

    return await cached("weather_stations", STATION_CACHE_TTL, fetch)


def _station_matches(stations: list[dict], name: str) -> list[dict]:
    rx = _pattern_to_regex(name)
    hits = [s for s in stations if rx.search(str(s.get("Name", "")))]
    if hits:
        # exact / prefix matches first
        q = name.strip().lower()
        hits.sort(key=lambda s: (
            0 if str(s.get("Name", "")).lower() == q else
            1 if str(s.get("Name", "")).lower().startswith(q) else
            2 if re.search(rf"\b{re.escape(q)}\b", str(s.get("Name", "")).lower()) else 3,
            str(s.get("Name", "")),
        ))
        return hits
    # fallback: all words present in any order
    words = [w for w in re.split(r"\s+", name.strip().lower()) if w]
    if len(words) > 1:
        return [s for s in stations if all(w in str(s.get("Name", "")).lower() for w in words)]
    return []


def _format_station(s: dict, distance_km: float | None = None) -> str:
    obs = s.get("Observation") or {}
    wind = first(get(obs, "Wind")) or {}
    agg = get(obs, "Aggregated30minutes") or {}
    lines = []
    dist = f" – {distance_km:.1f} km away" if distance_km is not None else ""
    lines.append(f"**{s.get('Name', 'Unknown')}** (station ID {s.get('Id', '–')}){dist}")
    if not obs:
        lines.append("  No current observation available.")
        lines.append(f"  Coordinates: {_coords(get(s, 'Geometry', 'WGS84'))}")
        return "\n".join(lines)
    lines.append(f"  Observed: {_time(obs.get('Sample'))}")
    temps = [
        ("Air", get(obs, "Air", "Temperature", "Value"), "°C", 1),
        ("Road surface", get(obs, "Surface", "Temperature", "Value"), "°C", 1),
        ("Dew point", get(obs, "Air", "Dewpoint", "Value"), "°C", 1),
        ("Humidity", get(obs, "Air", "RelativeHumidity", "Value"), "%", 0),
    ]
    temps = [(k, v, u, d) for k, v, u, d in temps if v is not None]
    if temps:
        lines.append("  " + " | ".join(f"{k}: {_num(v, d, u)}" for k, v, u, d in temps))
    if wind and get(wind, "Speed", "Value") is not None:
        w = f"  Wind: {_num(get(wind, 'Speed', 'Value'), 1, ' m/s')} from {_wind_dir(get(wind, 'Direction', 'Value'))}"
        gust = get(agg, "Wind", "SpeedMax", "Value")
        if gust is not None:
            w += f", gusts {_num(gust, 1, ' m/s')} (30 min max)"
        lines.append(w)
    precip = get(obs, "Weather", "Precipitation")
    amount = get(agg, "Precipitation", "TotalWaterEquivalent", "Value")
    if precip is not None or amount is not None:
        p = f"  Precipitation: {precip or '–'}"
        if amount is not None:
            p += f" ({_num(amount, 1, ' mm')} water equivalent last 30 min)"
        lines.append(p)
    surf = obs.get("Surface") or {}
    if surf and any(surf.get(k) is not None for k in ("Ice", "Snow", "Water", "Grip")):
        parts = [
            f"ice {_yesno(surf.get('Ice'))}" + (f" ({_num(get(surf, 'IceDepth', 'Value'), 1, ' mm')})" if get(surf, "IceDepth", "Value") is not None else ""),
            f"snow {_yesno(surf.get('Snow'))}" + (f" ({_num(get(surf, 'SnowDepth', 'Solid', 'Value'), 1, ' mm')})" if get(surf, "SnowDepth", "Solid", "Value") is not None else ""),
            f"water {_yesno(surf.get('Water'))}" + (f" ({_num(get(surf, 'WaterDepth', 'Value'), 1, ' mm')})" if get(surf, "WaterDepth", "Value") is not None else ""),
        ]
        grip = get(surf, "Grip", "Value")
        if grip is not None:
            parts.append(f"grip {_num(grip, 2)} (0–1)")
        lines.append("  Road surface: " + " | ".join(parts))
    vis = get(obs, "Air", "VisibleDistance", "Value")
    if vis is not None:
        lines.append(f"  Visibility: {_num(vis, 0, ' m')}")
    if len(lines) == 2:
        lines.append("  Only road-surface temperature is measured at this point (remote surface sensor).")
    lines.append(f"  Coordinates: {_coords(get(s, 'Geometry', 'WGS84'))}")
    return "\n".join(lines)


async def get_weather_station(station_name: str) -> str:
    """
    Get CURRENT road weather at a named Trafikverket road-weather station (väderstation / VViS).

    USE THIS TOOL WHEN:
    - User asks about current temperature, wind, precipitation, ice, snow or road surface
      at a specific place: "Hur är vädret på E4 vid Gävle?", "Is the road icy near Kiruna?"
    - User names a station, town, road or bridge (station names usually contain a town or
      road name, e.g. "Lidingöbron", "Uppsala", "Timrå", "Skellefteå")

    THEN:
    → Several matches? Present the closest/most relevant one first.
    → No match? Call list_weather_stations with a shorter pattern, or
      get_weather_stations_near_location with coordinates of the place.

    DO NOT USE when:
    - User wants stations near coordinates → get_weather_stations_near_location
    - User wants a time series / history → get_weather_observations with the station ID

    Parameters:
    - station_name: full or partial station name, case-insensitive substring
      ("Kiruna", "Uppsala", "E4 Gävle"). '%' or '*' wildcards allowed. Returns up to 3 matches.
    """
    try:
        if not station_name or not station_name.strip():
            return "Error: station_name is required."
        stations = await _all_stations()
        hits = _station_matches(stations, station_name)
        if not hits:
            return (
                f"No weather station matches '{station_name}'. Try a shorter name, a nearby town, "
                "or call get_weather_stations_near_location with coordinates."
            )
        shown = hits[:3]
        out = [f"Found {len(hits)} station(s) matching '{station_name}'"
               + (f", showing {len(shown)}:" if len(hits) > len(shown) else ":"), ""]
        for s in shown:
            out.append(_format_station(s))
            out.append("")
        if len(hits) > len(shown):
            out.append("Other matches: " + ", ".join(str(s.get("Name")) for s in hits[3:13]))
        return "\n".join(out).strip()
    except Exception as e:  # noqa: BLE001
        return _err(e)


async def list_weather_stations(name_pattern: str | None = None, limit: int = DEFAULT_ROWS) -> str:
    """
    List Trafikverket road-weather stations (name + ID + coordinates) to discover what exists.

    USE THIS TOOL WHEN:
    - User asks "which weather stations are there in/near X?" or "list stations on E4"
    - get_weather_station found nothing and you need to browse names
    - You need a station ID for get_weather_observations

    THEN CALL:
    → get_weather_station with a name for current readings
    → get_weather_observations with the numeric station ID for history

    DO NOT USE when:
    - User wants current readings at a known place → get_weather_station
    - User has coordinates → get_weather_stations_near_location

    Parameters:
    - name_pattern: optional case-insensitive substring or '%' wildcard ("Gävle", "E4%", "%bron").
      Weather stations carry no county field – to browse by region use
      get_weather_stations_near_location with the region's coordinates.
    - limit: max rows, default 20, maximum 50
    """
    try:
        n = _clamp(limit)
        stations = await _all_stations()
        hits = _station_matches(stations, name_pattern) if name_pattern else list(stations)
        if not hits:
            return f"No weather stations match '{name_pattern}'. Total stations available: {len(stations)}."
        out = [f"{len(hits)} weather station(s)" + (f" matching '{name_pattern}'" if name_pattern else "")
               + f" (showing {min(n, len(hits))} of {len(hits)}):", ""]
        for s in hits[:n]:
            obs = s.get("Observation") or {}
            temp = get(obs, "Air", "Temperature", "Value")
            extra = f" – air {_num(temp, 1, '°C')}" if temp is not None else ""
            out.append(f"- {s.get('Name')} (ID {s.get('Id')}) – {_coords(get(s, 'Geometry', 'WGS84'))}{extra}")
        if len(hits) > n:
            out.append(f"\n…{len(hits) - n} more. Narrow with name_pattern or raise limit (max {MAX_ROWS}).")
        return "\n".join(out)
    except Exception as e:  # noqa: BLE001
        return _err(e)


async def get_weather_stations_near_location(latitude: float, longitude: float, radius_km: float = 30, limit: int = 10) -> str:
    """
    Find road-weather stations within a radius of a WGS84 coordinate, with current readings.

    USE THIS TOOL WHEN:
    - User asks about road weather "near", "around" or "on the way to" a place and no
      station name is known. Convert the place to coordinates yourself, e.g.
      Stockholm 59.33,18.07 · Göteborg 57.71,11.97 · Malmö 55.60,13.00 · Uppsala 59.86,17.64 ·
      Umeå 63.83,20.26 · Luleå 65.58,22.15 · Kiruna 67.86,20.23 · Sundsvall 62.39,17.31 ·
      Östersund 63.18,14.64 · Skellefteå 64.75,20.95 · Jönköping 57.78,14.16 · Örebro 59.27,15.21

    THEN:
    → Results include current air/road temperature; call get_weather_station with the
      station name for the full observation, or get_weather_observations for history.

    DO NOT USE when: user names a station → get_weather_station.

    Parameters:
    - latitude, longitude: WGS84 decimal degrees (Sweden: lat 55–69, lon 10–24)
    - radius_km: search radius, default 30
    - limit: max stations, default 10, maximum 50
    """
    try:
        la, lo = _validate_latlon(latitude, longitude)
        n = _clamp(limit, 10)
        radius = float(radius_km) if radius_km else 30.0
        stations = await _all_stations()
        near = []
        for s in stations:
            p = parse_point(get(s, "Geometry", "WGS84"))
            if not p:
                continue
            d = haversine_km(la, lo, p[0], p[1])
            if d <= radius:
                near.append((d, s))
        near.sort(key=lambda t: t[0])
        if not near:
            return f"No weather stations within {radius:.0f} km of ({la}, {lo}). Try a larger radius_km."
        out = [f"{len(near)} station(s) within {radius:.0f} km of ({la:.3f}, {lo:.3f}), nearest first (showing {min(n, len(near))}):", ""]
        for d, s in near[:n]:
            obs = s.get("Observation") or {}
            out.append(
                f"- {s.get('Name')} (ID {s.get('Id')}) – {d:.1f} km – "
                f"air {_num(get(obs, 'Air', 'Temperature', 'Value'), 1, '°C')}, "
                f"road {_num(get(obs, 'Surface', 'Temperature', 'Value'), 1, '°C')}, "
                f"precip {get(obs, 'Weather', 'Precipitation') or '–'}, "
                f"observed {_time(obs.get('Sample'))}"
            )
        return "\n".join(out)
    except Exception as e:  # noqa: BLE001
        return _err(e)


async def get_weather_observations(measurepoint_id: int, limit: int = 10) -> str:
    """
    Get the most recent HISTORICAL observations (time series) for one weather station.

    USE THIS TOOL WHEN:
    - User asks how temperature/wind/precipitation has developed over the last hours
      at a station, or wants a trend ("har det blivit kallare i natt?")

    REQUIRES a numeric station ID – get it from get_weather_station, list_weather_stations
    or get_weather_stations_near_location first.

    DO NOT USE for current conditions → get_weather_station.

    Parameters:
    - measurepoint_id: numeric station ID (e.g. 1005)
    - limit: number of observations, newest first. Default 10, maximum 50.
      Observations are typically every 10 minutes.
    """
    try:
        try:
            mp_id = int(measurepoint_id)
        except (TypeError, ValueError):
            return "Error: measurepoint_id must be a numeric station ID (e.g. 1005)."
        n = _clamp(limit, 10)
        rows = await query(
            "WeatherObservation",
            OBSERVATION_FIELDS,
            f_and(f_eq("Measurepoint.Id", mp_id), f_eq("Deleted", False)),
            limit=n,
            orderby="Sample desc",
        )
        if not rows:
            return f"No observations found for station ID {mp_id}. Check the ID with list_weather_stations."
        name = get(rows[0], "Measurepoint", "Name") or f"station {mp_id}"
        out = [f"Last {len(rows)} observation(s) for {name} (ID {mp_id}), newest first:", ""]
        for o in rows:
            wind = first(o.get("Wind")) or {}
            surf = o.get("Surface") or {}
            flags = []
            if surf.get("Ice"):
                flags.append("ICE")
            if surf.get("Snow"):
                flags.append("snow")
            if surf.get("Water"):
                flags.append("wet")
            out.append(
                f"- {_time(o.get('Sample'))}: air {_num(get(o, 'Air', 'Temperature', 'Value'), 1, '°C')}, "
                f"road {_num(get(o, 'Surface', 'Temperature', 'Value'), 1, '°C')}, "
                f"humidity {_num(get(o, 'Air', 'RelativeHumidity', 'Value'), 0, '%')}, "
                f"wind {_num(get(wind, 'Speed', 'Value'), 1, ' m/s')} {_wind_dir(get(wind, 'Direction', 'Value'))}, "
                f"precip {get(o, 'Weather', 'Precipitation') or '–'}"
                + (f" [{' '.join(flags)}]" if flags else "")
            )
        return "\n".join(out)
    except Exception as e:  # noqa: BLE001
        return _err(e)


# ── Cameras ──────────────────────────────────────────────────────────


async def _all_cameras() -> list[dict]:
    async def fetch():
        return await query("Camera", CAMERA_FIELDS, f_eq("Deleted", False), orderby="Name")

    return await cached("cameras", CAMERA_CACHE_TTL, fetch)


def _format_camera(c: dict, distance_km: float | None = None) -> str:
    dist = f" – {distance_km:.1f} km away" if distance_km is not None else ""
    lines = [f"**{c.get('Name', 'Unknown')}** (camera ID {c.get('Id', '–')}){dist}"]
    loc = c.get("Location")
    desc = c.get("Description")
    if loc:
        lines.append(f"  Location: {loc}")
    if desc and desc != loc:
        lines.append(f"  Description: {desc}")
    meta = []
    if c.get("Direction") not in (None, ""):
        meta.append(f"direction {c['Direction']}°")
    if c.get("Type"):
        meta.append(f"type {c['Type']}")
    if c.get("Status"):
        meta.append(f"status {c['Status']}")
    if c.get("Active") is False:
        meta.append("INACTIVE")
    cn = county_list(c.get("CountyNo"))
    if cn:
        meta.append("county " + ", ".join(county_name(x) for x in cn))
    if meta:
        lines.append("  " + " | ".join(meta))
    if c.get("PhotoUrl"):
        lines.append(f"  Photo: {c['PhotoUrl']} (taken {_time(c.get('PhotoTime'))})")
        if c.get("PhotoUrlFullsize"):
            lines.append(f"  Full-size photo: {c['PhotoUrlFullsize']}")
        elif c.get("HasFullSizePhoto"):
            lines.append(f"  Full-size photo: {c['PhotoUrl']}?type=fullsize")
    lines.append(f"  Coordinates: {_coords(get(c, 'Geometry', 'WGS84'))}")
    return "\n".join(lines)


async def get_cameras(name_pattern: str | None = None, county: int | None = None, limit: int = 10) -> str:
    """
    Find Trafikverket traffic cameras (trafikkameror) by name/location text and/or county.

    USE THIS TOOL WHEN:
    - User asks "is there a camera at/on X?", "show me the traffic camera at Essingeleden",
      "vilka kameror finns på E4 i Stockholms län?"
    - You need a camera's photo URL

    THEN:
    → Show the photo URL as a markdown image ![name](PhotoUrl) so the user sees it.
    → Only call get_camera_image if the user explicitly wants the image fetched/analysed.
    → For "cameras near <place>" with no name → get_cameras_near_location.

    Parameters:
    - name_pattern: case-insensitive substring matched against camera name, location and
      description ("E4", "Essingeleden", "Sundsvall"). '%' wildcards allowed.
    - county: optional Swedish county number: 1 Stockholm, 3 Uppsala, 4 Södermanland,
      5 Östergötland, 6 Jönköping, 7 Kronoberg, 8 Kalmar, 9 Gotland, 10 Blekinge, 12 Skåne,
      13 Halland, 14 Västra Götaland, 17 Värmland, 18 Örebro, 19 Västmanland, 20 Dalarna,
      21 Gävleborg, 22 Västernorrland, 23 Jämtland, 24 Västerbotten, 25 Norrbotten
    - limit: max cameras, default 10, maximum 50
    """
    try:
        c_no = _validate_county(county)
        n = _clamp(limit, 10)
        cams = await _all_cameras()
        hits = cams
        if c_no is not None:
            hits = [c for c in hits if c_no in county_list(c.get("CountyNo"))]
        if name_pattern and name_pattern.strip():
            rx = _pattern_to_regex(name_pattern)
            hits = [c for c in hits if any(rx.search(str(c.get(k) or "")) for k in ("Name", "Location", "Description"))]
        if not hits:
            return (f"No cameras match name_pattern={name_pattern!r} county={c_no}. "
                    f"Total cameras available: {len(cams)}. Try a broader pattern or get_cameras_near_location.")
        out = [f"{len(hits)} camera(s) found (showing {min(n, len(hits))}):", ""]
        for c in hits[:n]:
            out.append(_format_camera(c))
            out.append("")
        if len(hits) > n:
            out.append(f"…{len(hits) - n} more. Narrow the search or raise limit (max {MAX_ROWS}).")
        return "\n".join(out).strip()
    except Exception as e:  # noqa: BLE001
        return _err(e)


async def get_cameras_near_location(latitude: float, longitude: float, radius_km: float = 30, county: int | None = None, limit: int = 10) -> str:
    """
    Find traffic cameras within a radius of a WGS84 coordinate, nearest first.

    USE THIS TOOL WHEN:
    - User asks for cameras "near", "around" or "between" places, or along a route,
      and you can supply coordinates (see get_weather_stations_near_location for city examples)

    THEN: show PhotoUrl as a markdown image; call get_camera_image only on explicit request.

    DO NOT USE when the user names a specific camera/road → get_cameras.

    Parameters:
    - latitude, longitude: WGS84 decimal degrees
    - radius_km: default 30
    - county: optional county number (1–25) to pre-filter
    - limit: max cameras, default 10, maximum 50
    """
    try:
        la, lo = _validate_latlon(latitude, longitude)
        c_no = _validate_county(county)
        n = _clamp(limit, 10)
        radius = float(radius_km) if radius_km else 30.0
        cams = await _all_cameras()
        near = []
        for c in cams:
            if c_no is not None and c_no not in county_list(c.get("CountyNo")):
                continue
            p = parse_point(get(c, "Geometry", "WGS84"))
            if not p:
                continue
            d = haversine_km(la, lo, p[0], p[1])
            if d <= radius:
                near.append((d, c))
        near.sort(key=lambda t: t[0])
        if not near:
            return f"No cameras within {radius:.0f} km of ({la}, {lo}). Try a larger radius_km."
        out = [f"{len(near)} camera(s) within {radius:.0f} km of ({la:.3f}, {lo:.3f}), nearest first (showing {min(n, len(near))}):", ""]
        for d, c in near[:n]:
            out.append(_format_camera(c, d))
            out.append("")
        return "\n".join(out).strip()
    except Exception as e:  # noqa: BLE001
        return _err(e)


async def get_camera_image(photo_url: str, full_size: bool = False) -> Image:
    """
    Fetch the actual JPEG from a traffic camera and return it as an image.

    USE THIS TOOL ONLY WHEN the user explicitly asks to see/fetch/analyse the camera picture.
    For simply showing a camera, embed the PhotoUrl from get_cameras as a markdown image instead
    (cheaper, no tool call needed).

    Parameters:
    - photo_url: the PhotoUrl returned by get_cameras / get_cameras_near_location
      (must be on api.trafikinfo.trafikverket.se)
    - full_size: request the full-resolution image (only when HasFullSizePhoto was reported)
    """
    url = (photo_url or "").strip()
    if not url.lower().startswith("https://api.trafikinfo.trafikverket.se/"):
        raise ToolError("photo_url must be a PhotoUrl from get_cameras (https://api.trafikinfo.trafikverket.se/...)")
    if full_size and "type=fullsize" not in url:
        url += ("&" if "?" in url else "?") + "type=fullsize"
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "intric-trafikverket-mcp/1.0"})
    except httpx.HTTPError as e:
        raise ToolError(f"Could not fetch camera image: {e}") from e
    if r.status_code != 200:
        raise ToolError(f"Could not fetch camera image: HTTP {r.status_code}")
    ctype = (r.headers.get("content-type") or "image/jpeg").split(";")[0].strip()
    fmt = ctype.split("/")[-1] if "/" in ctype else "jpeg"
    if fmt == "jpg":
        fmt = "jpeg"
    if not ctype.startswith("image/"):
        raise ToolError(f"URL did not return an image (content-type {ctype})")
    return Image(data=r.content, format=fmt)


# ── Traffic situations (Situation) ───────────────────────────────────


def _deviations(rows: list[dict]) -> list[dict]:
    """Flatten Situation rows into one dict per Deviation, keeping situation id."""
    out = []
    for s in rows:
        devs = s.get("Deviation") or []
        if isinstance(devs, dict):
            devs = [devs]
        for d in devs:
            if isinstance(d, dict):
                if d.get("Suspended") is True:
                    continue  # temporarily suspended deviation (Situation 1.6)
                end = str(d.get("EndTime") or "")
                if end and end[:19] < _now_local_iso():
                    continue  # already ended, not yet purged upstream
                d = dict(d)
                d["_situation_id"] = s.get("Id")
                out.append(d)
    return out


def _sev(d: dict) -> int:
    try:
        return int(d.get("SeverityCode") or 0)
    except (TypeError, ValueError):
        return 0


def _now_local_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _is_planned(d: dict) -> bool:
    st = str(d.get("StartTime") or "")
    return bool(st) and st[:19] > _now_local_iso()


def _headline(d: dict) -> str:
    return str(d.get("Header") or d.get("MessageCode") or d.get("MessageType") or "Traffic situation")


def _format_deviation(d: dict) -> str:
    sev_txt = d.get("SeverityText") or ""
    sev = _sev(d)
    head = _headline(d)
    tag = f"[{d.get('MessageType') or 'Info'}" + (f", severity {sev}/5 {sev_txt}".rstrip() if sev else "") + (", PLANNED" if _is_planned(d) else "") + "]"
    lines = [f"{tag} **{head}**"]
    road = d.get("RoadNumber")
    if d.get("RoadName") and d["RoadName"] != road:
        road = f"{road} ({d['RoadName']})" if road else d["RoadName"]
    loc = d.get("LocationDescriptor")
    where = " – ".join(x for x in [road, loc] if x)
    if where:
        lines.append(f"  Where: {where}")
    cn = county_list(d.get("CountyNo"))
    if cn:
        lines.append("  County: " + ", ".join(county_name(x) for x in cn))
    msg = d.get("Message")
    if msg and msg != head:
        msg = re.sub(r"\s+", " ", str(msg)).strip()
        lines.append(f"  {msg[:400]}{'…' if len(msg) > 400 else ''}")
    if d.get("TrafficRestrictionType"):
        lines.append(f"  Restriction: {d['TrafficRestrictionType']}")
    lines.append(f"  From: {_time(d.get('StartTime'))}  To: {_time(d.get('EndTime')) if d.get('EndTime') else 'until further notice'}")
    p = get(d, "Geometry", "Point", "WGS84")
    if p:
        lines.append(f"  Coordinates: {_coords(p)}")
    return "\n".join(lines)


async def _fetch_situations(county: int | None) -> list[dict]:
    filters = f_eq("Deleted", False)
    if county is not None:
        filters = f_and(filters, f_eq("Deviation.CountyNo", county))
    return await query("Situation", SITUATION_FIELDS, filters, limit=SITUATION_FETCH_CAP)


async def get_traffic_situations(road_number: str | None = None, county: int | None = None, message_type: str | None = None, limit: int = DEFAULT_ROWS) -> str:
    """
    Get CURRENT traffic situations: accidents, road works, closures, obstacles, restrictions.

    USE THIS TOOL WHEN:
    - User asks "is there an accident on E4?", "vägarbeten i Skåne?", "är vägen avstängd?",
      "what's happening on the roads around Stockholm right now?"

    THEN:
    → For an overview / counts ("how many road works in Sweden?") → get_traffic_situation_summary
    → For road surface state (ice, snow) → get_road_conditions
    → For congestion / speeds → get_traffic_flow

    Parameters:
    - road_number: e.g. "E4", "E20", "73", "väg 262". Matched exactly on the road label.
    - county: county number (1–25), see get_cameras for the list. Strongly recommended
      to keep results relevant.
    - message_type: optional filter, one of the Swedish types used by Trafikverket:
      "Olycka" (accident), "Vägarbete" (road work), "Hinder" (obstacle), "Restriktion",
      "Trafikmeddelande", "Färjor" (ferries), "Viktig trafikinformation". Substring match.
    - limit: max situations, default 20, maximum 50. Sorted by severity, then most recent start.
    """
    try:
        c_no = _validate_county(county)
        n = _clamp(limit)
        rows = await _fetch_situations(c_no)
        devs = _deviations(rows)
        total = len(devs)
        if road_number and road_number.strip():
            devs = [d for d in devs if _road_matches(d.get("RoadNumber"), road_number)]
        if message_type and message_type.strip():
            mt = message_type.strip().lower()
            devs = [d for d in devs if mt in str(d.get("MessageType") or "").lower()]
        if not devs:
            return (f"No active traffic situations match road_number={road_number!r}, county={c_no}, "
                    f"message_type={message_type!r} ({total} active situations before filtering). "
                    "Try without road_number, or check the county number.")
        devs.sort(key=lambda d: (-_sev(d), str(d.get("StartTime") or "")), reverse=False)
        devs.sort(key=lambda d: str(d.get("StartTime") or ""), reverse=True)
        devs.sort(key=_sev, reverse=True)
        out = [f"{len(devs)} traffic situation(s) found (showing {min(n, len(devs))}, most severe first):", ""]
        for d in devs[:n]:
            out.append(_format_deviation(d))
            out.append("")
        if len(devs) > n:
            out.append(f"…{len(devs) - n} more. Use get_traffic_situation_summary for an overview, or narrow the filters.")
        return "\n".join(out).strip()
    except Exception as e:  # noqa: BLE001
        return _err(e)


async def get_traffic_situation_summary(county: int | None = None) -> str:
    """
    Aggregated overview of current traffic situations: counts by type, by county and by severity,
    plus the most severe items. Aggregation is done server-side – never count rows yourself.

    USE THIS TOOL WHEN:
    - User asks "how many road works are there in Sweden/Skåne?", "which county has the most
      accidents right now?", "give me an overview of the traffic situation"

    THEN CALL get_traffic_situations with county / message_type for details.

    Parameters:
    - county: optional county number (1–25) to restrict the summary; omit for all of Sweden.
    """
    try:
        c_no = _validate_county(county)
        rows = await _fetch_situations(c_no)
        devs = _deviations(rows)
        if not devs:
            return "No active traffic situations reported" + (f" in {county_name(c_no)}" if c_no else " in Sweden") + "."
        by_type: dict[str, int] = {}
        by_county: dict[int, int] = {}
        by_sev: dict[int, int] = {}
        for d in devs:
            t = str(d.get("MessageType") or "Okänd")
            by_type[t] = by_type.get(t, 0) + 1
            for c in county_list(d.get("CountyNo")):
                by_county[c] = by_county.get(c, 0) + 1
            s = _sev(d)
            by_sev[s] = by_sev.get(s, 0) + 1
        scope = county_name(c_no) if c_no else "Sweden"
        planned = sum(1 for d in devs if _is_planned(d))
        out = [f"Traffic situation overview for {scope}: {len(devs)} active deviation(s) in {len(rows)} situation(s) "
               f"({len(devs) - planned} ongoing, {planned} planned/starting later).", ""]
        out.append("By type:")
        for t, cnt in sorted(by_type.items(), key=lambda kv: -kv[1]):
            out.append(f"  {t}: {cnt}")
        if not c_no:
            out.append("")
            out.append("By county (top 10):")
            for c, cnt in sorted(by_county.items(), key=lambda kv: -kv[1])[:10]:
                out.append(f"  {county_name(c)}: {cnt}")
        out.append("")
        out.append("By severity (5 = most severe):")
        for s in sorted(by_sev.keys(), reverse=True):
            out.append(f"  {s if s else 'unspecified'}: {by_sev[s]}")
        top = sorted(devs, key=_sev, reverse=True)[:5]
        if top and _sev(top[0]) > 0:
            out.append("")
            out.append("Most severe right now:")
            for d in top:
                if _sev(d) == 0:
                    break
                road = d.get("RoadNumber") or ""
                out.append(f"  - [{d.get('MessageType')}, sev {_sev(d)}{', planned' if _is_planned(d) else ''}] {_headline(d)}"
                           + (f" ({road})" if road else "")
                           + (" – " + ", ".join(county_name(x) for x in county_list(d.get("CountyNo"))[:2]) if d.get("CountyNo") else ""))
        return "\n".join(out)
    except Exception as e:  # noqa: BLE001
        return _err(e)


# ── Traffic flow ─────────────────────────────────────────────────────


async def get_traffic_flow(county: int | None = None, latitude: float | None = None, longitude: float | None = None, radius_km: float = 10, limit: int = DEFAULT_ROWS) -> str:
    """
    Real-time traffic flow: vehicles per hour and average speed at Trafikverket measurement sites
    (mainly motorways around Stockholm, Göteborg and Malmö and major national roads).

    USE THIS TOOL WHEN:
    - User asks "how is the traffic / is it congested on E4 in Stockholm right now?",
      "average speed on Essingeleden?", "trafikflöde i Göteborg"

    You MUST give either a county or a coordinate (latitude+longitude); the dataset is
    too large to browse unfiltered. Sites are identified by SiteId and coordinates only –
    there is no road name in this dataset, so use coordinates for "on road X near Y" questions.

    DO NOT USE for incidents → get_traffic_situations, or road surface → get_road_conditions.

    Parameters:
    - county: county number (1 Stockholm, 14 Västra Götaland, 12 Skåne, …)
    - latitude, longitude: WGS84 point to search around (optional, overrides county for distance sort)
    - radius_km: radius when coordinates are given, default 10
    - limit: max rows, default 20, maximum 50. One row per site/lane/vehicle type.
    """
    try:
        c_no = _validate_county(county)
        n = _clamp(limit)
        point = None
        if latitude is not None or longitude is not None:
            point = _validate_latlon(latitude, longitude)
        if c_no is None and point is None:
            return "Error: give a county number (e.g. 1 for Stockholm) or latitude+longitude."
        filters = f_eq("Deleted", False)
        if c_no is not None:
            filters = f_and(filters, f_eq("CountyNo", c_no))
        rows = await query("TrafficFlow", TRAFFICFLOW_FIELDS, filters, limit=TRAFFICFLOW_FETCH_CAP, orderby="MeasurementTime desc")
        if not rows:
            return "No traffic flow measurements available for that filter."
        scored = []
        radius = float(radius_km) if radius_km else 10.0
        for r in rows:
            d = None
            if point:
                p = parse_point(get(r, "Geometry", "WGS84"))
                if not p:
                    continue
                d = haversine_km(point[0], point[1], p[0], p[1])
                if d > radius:
                    continue
            scored.append((d if d is not None else 0.0, r))
        if not scored:
            return f"No traffic flow sites within {radius:.0f} km of the given point. Try a larger radius_km."
        if point:
            scored.sort(key=lambda t: t[0])
        # prefer the aggregate "anyVehicle" rows first when present
        scored.sort(key=lambda t: 0 if str(t[1].get("VehicleType") or "").lower() in ("anyvehicle", "any", "") else 1)
        if point:
            scored.sort(key=lambda t: t[0])
        out = [f"{len(scored)} traffic flow row(s) (showing {min(n, len(scored))}):", ""]
        for d, r in scored[:n]:
            dist = f" – {d:.1f} km away" if point else ""
            cn = county_list(r.get("CountyNo"))
            out.append(
                f"- Site {r.get('SiteId')}{dist}: {_num(r.get('VehicleFlowRate'), 0)} vehicles/h, "
                f"avg speed {_num(r.get('AverageVehicleSpeed'), 0, ' km/h')}, "
                f"type {r.get('VehicleType') or '–'}, lane {r.get('SpecificLane') or '–'}, side {r.get('MeasurementSide') or '–'}, "
                f"measured {_time(r.get('MeasurementTime'))}"
                + (f", quality {r['DataQuality']}" if r.get("DataQuality") else "")
                + (f", {county_name(cn[0])}" if cn else "")
                + f", at {_coords(get(r, 'Geometry', 'WGS84'))}"
            )
        return "\n".join(out)
    except Exception as e:  # noqa: BLE001
        return _err(e)


# ── Road conditions (väglag) ─────────────────────────────────────────


def _listify(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v if x not in (None, "")]
    return [str(v)]


def _format_condition(c: dict) -> str:
    road = c.get("RoadNumber") or (f"väg {c.get('RoadNumberNumeric')}" if c.get("RoadNumberNumeric") else "Unknown road")
    cond = c.get("ConditionText") or f"code {c.get('ConditionCode')}"
    lines = [f"**{road}** – {cond}" + (f" (code {c.get('ConditionCode')})" if c.get("ConditionText") and c.get("ConditionCode") is not None else "")]
    if c.get("LocationText"):
        lines.append(f"  Stretch: {c['LocationText']}")
    cn = county_list(c.get("CountyNo"))
    if cn:
        lines.append("  County: " + ", ".join(county_name(x) for x in cn))
    info = _listify(c.get("ConditionInfo"))
    if info:
        lines.append("  Info: " + "; ".join(info))
    cause = _listify(c.get("Cause"))
    if cause:
        lines.append("  Cause: " + ", ".join(cause))
    warn = _listify(c.get("Warning"))
    if warn:
        lines.append("  Warning: " + ", ".join(warn))
    meas = _listify(c.get("Measurement"))
    if meas:
        lines.append("  Measures: " + ", ".join(meas))
    lines.append(f"  Valid: {_time(c.get('StartTime'))} → {_time(c.get('EndTime')) if c.get('EndTime') else 'until further notice'}")
    return "\n".join(lines)


async def get_road_conditions(road_number: str | None = None, county: int | None = None, limit: int = DEFAULT_ROWS) -> str:
    """
    Current road surface conditions (väglag) reported by Trafikverket: dry, wet, ice, snow,
    slippery, with causes, warnings and measures (salting, ploughing). Mostly active in winter.

    USE THIS TOOL WHEN:
    - User asks "är det halt på E4 idag?", "road conditions in Norrbotten", "is it icy on route 45?"

    THEN:
    → For a measured surface temperature / ice detection at a point → get_weather_station
    → For closures and accidents → get_traffic_situations

    Parameters:
    - road_number: e.g. "E4", "E45", "70", "väg 84" (exact road label match)
    - county: county number (1–25); recommended
    - limit: max rows, default 20, maximum 50
    """
    try:
        c_no = _validate_county(county)
        n = _clamp(limit)
        filters = f_eq("Deleted", False)
        if c_no is not None:
            filters = f_and(filters, f_eq("CountyNo", c_no))
        rows = await query("RoadCondition", ROADCONDITION_FIELDS, filters, limit=ROADCONDITION_FETCH_CAP)
        total = len(rows)
        if road_number and road_number.strip():
            rows = [r for r in rows if _road_matches(r.get("RoadNumber"), road_number)
                    or _norm_road(road_number) == _norm_road(str(r.get("RoadNumberNumeric") or ""))]
        if not rows:
            return (f"No road condition reports match road_number={road_number!r}, county={c_no} "
                    f"({total} reports before road filter). Outside winter the dataset is often empty; "
                    "use get_weather_station for measured surface temperature and ice/snow flags.")
        # worst conditions first (higher ConditionCode = worse), then most recent
        def code(r):
            try:
                return int(r.get("ConditionCode") or 0)
            except (TypeError, ValueError):
                return 0
        rows.sort(key=lambda r: str(r.get("ModifiedTime") or ""), reverse=True)
        rows.sort(key=code, reverse=True)
        out = [f"{len(rows)} road condition report(s) (showing {min(n, len(rows))}, worst first):", ""]
        for r in rows[:n]:
            out.append(_format_condition(r))
            out.append("")
        if len(rows) > n:
            out.append(f"…{len(rows) - n} more. Narrow by road_number or county.")
        return "\n".join(out).strip()
    except Exception as e:  # noqa: BLE001
        return _err(e)


# ── Reference ────────────────────────────────────────────────────────


async def list_counties() -> str:
    """
    List Swedish county numbers (länskoder) used by the county parameter of the other tools.

    USE THIS TOOL only if you are unsure which number a county has. The mapping is also in
    the get_cameras docstring. Never guess a county number – look it up here.
    """
    lines = ["Swedish county numbers (länskod → county):"]
    for k, v in COUNTIES.items():
        lines.append(f"  {k}: {v}")
    lines.append("Note: 2, 11, 15 and 16 are unused historical codes.")
    return "\n".join(lines)


ALL_TOOLS = [
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
]
