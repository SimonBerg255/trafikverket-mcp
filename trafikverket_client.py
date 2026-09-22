"""
trafikverket_client.py – thin async wrapper around the Trafikverket Open API.

The API (https://api.trafikinfo.trafikverket.se/v2/) takes an XML query via
POST and returns JSON (data.json) or XML (data.xml). We use the JSON endpoint.

Response envelope (verified with a live probe):
    {"RESPONSE": {"RESULT": [{"<ObjectType>": [ ... ], "INFO": {...}}]}}
Error envelope (HTTP 200 *or* 4xx – always check RESULT[0].ERROR):
    {"RESPONSE": {"RESULT": [{"ERROR": {"SOURCE": "Security", "MESSAGE": "Invalid authentication"}}]}}

Trafikverket key resolution (per request):
    1. "Authorization: Bearer <key>" header on the MCP request – this is what Intric sends when the
       server is registered with auth mode "API key" and the Trafikverket key is pasted in that field.
    2. TRAFIKVERKET_API_KEY environment variable (shared fallback for all callers).
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import re
import time
from typing import Any

import httpx

BASE_URL = "https://api.trafikinfo.trafikverket.se/v2/data.json"
USER_AGENT = "intric-trafikverket-mcp/1.0"
TIMEOUT_SECONDS = 30.0

# Schema versions – one place to change if Trafikverket bumps a dataset.
SCHEMA = {
    "WeatherMeasurepoint": "2.1",
    "WeatherObservation": "2.1",
    "Camera": "1.1",
    "Situation": "1.6",   # 1.5 removed from catalogue 2026; 1.6 adds Deviation.Suspended
    "TrafficFlow": "1.5",  # adds DataQuality (good/degraded/bad)
    "RoadCondition": "1.2", # 1.3 renames Measurement→Measure
}

# QUERY namespace attribute. Verified live 2026-09-22: without it the data endpoint answers
# "ObjectType 'Situation' does not exists" and Camera only resolves to the old 1.0 schema.
NAMESPACE: dict[str, str] = {
    "Situation": "Road.TrafficInfo",
    "Camera": "Road.Infrastructure",
}

# Swedish county numbers (länskod) as used by Trafikverket's CountyNo fields.
COUNTIES: dict[int, str] = {
    1: "Stockholm",
    3: "Uppsala",
    4: "Södermanland",
    5: "Östergötland",
    6: "Jönköping",
    7: "Kronoberg",
    8: "Kalmar",
    9: "Gotland",
    10: "Blekinge",
    12: "Skåne",
    13: "Halland",
    14: "Västra Götaland",
    17: "Värmland",
    18: "Örebro",
    19: "Västmanland",
    20: "Dalarna",
    21: "Gävleborg",
    22: "Västernorrland",
    23: "Jämtland",
    24: "Västerbotten",
    25: "Norrbotten",
}


class TrafikverketError(Exception):
    """Raised for API-level errors (auth, bad query, network, timeout)."""


NO_KEY_MESSAGE = (
    "No Trafikverket API key available. In Intric, set this MCP server's auth mode to 'API key' and "
    "paste your Trafikverket key (free at https://data.trafikverket.se/) – or set TRAFIKVERKET_API_KEY "
    "on the server."
)


def _bearer_from_request() -> str | None:
    """Read the API key Intric forwards on the current MCP request, if any.

    Accepts "Authorization: Bearer <key>" (what Intric's api_key mode sends), a raw
    Authorization value, or an X-API-Key / Api-Key header."""
    try:
        from fastmcp.server.dependencies import get_http_headers

        headers = get_http_headers(include={"authorization", "x-api-key", "api-key"})
    except Exception:  # noqa: BLE001 – no request context (tests, CLI)
        return None
    value = (headers.get("authorization") or "").strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    if not value:
        value = (headers.get("x-api-key") or headers.get("api-key") or "").strip()
    return value or None


def resolve_api_key() -> str:
    key = _bearer_from_request() or os.environ.get("TRAFIKVERKET_API_KEY", "").strip()
    if not key or key == "REPLACE_WITH_YOUR_KEY":
        raise TrafikverketError(NO_KEY_MESSAGE)
    return key


# ── XML query builder ────────────────────────────────────────────────


def _esc(value: Any) -> str:
    s = str(value)
    if isinstance(value, bool):
        s = "true" if value else "false"
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def f_eq(name: str, value: Any) -> str:
    return f'<EQ name="{_esc(name)}" value="{_esc(value)}" />'


def f_ne(name: str, value: Any) -> str:
    return f'<NE name="{_esc(name)}" value="{_esc(value)}" />'


def f_gt(name: str, value: Any) -> str:
    return f'<GT name="{_esc(name)}" value="{_esc(value)}" />'


def f_gte(name: str, value: Any) -> str:
    return f'<GTE name="{_esc(name)}" value="{_esc(value)}" />'


def f_lt(name: str, value: Any) -> str:
    return f'<LT name="{_esc(name)}" value="{_esc(value)}" />'


def f_like(name: str, regex: str) -> str:
    """LIKE takes a regular expression (case-insensitive by default in the API)."""
    return f'<LIKE name="{_esc(name)}" value="{_esc(regex)}" />'


def f_in(name: str, values: list[Any]) -> str:
    return f'<IN name="{_esc(name)}" value="{_esc(",".join(str(v) for v in values))}" />'


def f_exists(name: str, value: bool = True) -> str:
    return f'<EXISTS name="{_esc(name)}" value="{_esc(value)}" />'


def f_and(*filters: str) -> str:
    inner = [f for f in filters if f]
    if not inner:
        return ""
    if len(inner) == 1:
        return inner[0]
    return "<AND>" + "".join(inner) + "</AND>"


def f_or(*filters: str) -> str:
    inner = [f for f in filters if f]
    if not inner:
        return ""
    if len(inner) == 1:
        return inner[0]
    return "<OR>" + "".join(inner) + "</OR>"


def build_request(
    objecttype: str,
    includes: list[str] | None = None,
    filters: str = "",
    limit: int | None = None,
    orderby: str | None = None,
    skip: int | None = None,
    schemaversion: str | None = None,
    api_key: str | None = None,
    namespace: str | None = None,
) -> str:
    key = api_key if api_key is not None else resolve_api_key()
    version = schemaversion or SCHEMA[objecttype]
    ns = namespace if namespace is not None else NAMESPACE.get(objecttype)
    attrs = f'objecttype="{objecttype}" schemaversion="{version}"'
    if ns:
        attrs += f' namespace="{_esc(ns)}"'
    if limit is not None:
        attrs += f' limit="{int(limit)}"'
    if skip is not None:
        attrs += f' skip="{int(skip)}"'
    if orderby:
        attrs += f' orderby="{_esc(orderby)}"'
    parts = [f"<REQUEST><LOGIN authenticationkey=\"{_esc(key)}\" /><QUERY {attrs}>"]
    if filters:
        parts.append(f"<FILTER>{filters}</FILTER>")
    for inc in includes or []:
        parts.append(f"<INCLUDE>{_esc(inc)}</INCLUDE>")
    parts.append("</QUERY></REQUEST>")
    return "".join(parts)


# ── HTTP ─────────────────────────────────────────────────────────────


def _extract_error(payload: Any) -> str | None:
    try:
        results = payload["RESPONSE"]["RESULT"]
    except (KeyError, TypeError):
        return None
    for item in results if isinstance(results, list) else [results]:
        if isinstance(item, dict) and "ERROR" in item:
            err = item["ERROR"] or {}
            return f"{err.get('SOURCE', 'API')}: {err.get('MESSAGE', 'Unknown error')}"
    return None


async def query(
    objecttype: str,
    includes: list[str] | None = None,
    filters: str = "",
    limit: int | None = None,
    orderby: str | None = None,
    skip: int | None = None,
    schemaversion: str | None = None,
    namespace: str | None = None,
) -> list[dict]:
    """POST one query and return the list of objects (empty list if none)."""
    body = build_request(objecttype, includes, filters, limit, orderby, skip, schemaversion, namespace=namespace)
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            r = await client.post(
                BASE_URL,
                content=body.encode("utf-8"),
                headers={
                    "Content-Type": "text/xml; charset=utf-8",
                    "Accept": "application/json",
                    "User-Agent": USER_AGENT,
                },
            )
    except httpx.TimeoutException as e:
        raise TrafikverketError(f"Trafikverket API timed out after {TIMEOUT_SECONDS:.0f}s") from e
    except httpx.HTTPError as e:
        raise TrafikverketError(f"Network error talking to Trafikverket: {e}") from e

    try:
        payload = r.json()
    except ValueError:
        payload = None

    err = _extract_error(payload)
    if err:
        if "Invalid authentication" in err:
            err += (" – the Trafikverket API key was rejected. Check the key pasted in Intric's "
                    "API key field (or TRAFIKVERKET_API_KEY on the server).")
        raise TrafikverketError(err)
    if r.status_code not in (200, 206):  # 206 = partial result (too large), still valid JSON
        snippet = (r.text or "")[:300].replace("\n", " ")
        raise TrafikverketError(f"HTTP {r.status_code} from Trafikverket: {snippet}")
    if not isinstance(payload, dict):
        raise TrafikverketError("Unexpected non-JSON response from Trafikverket")

    results = payload.get("RESPONSE", {}).get("RESULT", [])
    if not results:
        return []
    first = results[0] if isinstance(results, list) else results
    items = first.get(objecttype, []) if isinstance(first, dict) else []
    if isinstance(items, dict):
        items = [items]
    return items


# ── Simple TTL cache (module-level, shared across tool calls) ────────

_cache: dict[str, tuple[float, Any]] = {}
_cache_locks: dict[str, asyncio.Lock] = {}


async def cached(key: str, ttl_seconds: float, fetch):
    """TTL cache scoped per Trafikverket API key, so a caller with an invalid key never gets
    data that a valid caller fetched earlier (the key is the credential)."""
    key = hashlib.sha256(resolve_api_key().encode()).hexdigest()[:12] + ":" + key
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl_seconds:
        return hit[1]
    lock = _cache_locks.setdefault(key, asyncio.Lock())
    async with lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < ttl_seconds:
            return hit[1]
        value = await fetch()
        _cache[key] = (time.monotonic(), value)
        return value


def clear_cache() -> None:
    _cache.clear()


# ── Geo helpers ──────────────────────────────────────────────────────

_POINT_RE = re.compile(r"POINT\s*\(\s*([-\d.]+)\s+([-\d.]+)", re.IGNORECASE)


def parse_point(wkt: str | None) -> tuple[float, float] | None:
    """Parse 'POINT (lon lat)' WKT → (lat, lon). Returns None when absent."""
    if not wkt:
        return None
    m = _POINT_RE.search(wkt)
    if not m:
        return None
    lon, lat = float(m.group(1)), float(m.group(2))
    return lat, lon


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def get(d: Any, *path: str, default: Any = None) -> Any:
    """Safe nested lookup: get(obj, 'Air', 'Temperature', 'Value')."""
    cur = d
    for p in path:
        if isinstance(cur, dict):
            cur = cur.get(p)
        elif isinstance(cur, list) and cur and p == "0":
            cur = cur[0]
        else:
            return default
        if cur is None:
            return default
    return cur


def first(value: Any) -> Any:
    """Return first element when the API gives a list, else the value itself."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def county_name(no: Any) -> str:
    try:
        n = int(first(no))
    except (TypeError, ValueError):
        return "okänt län"
    if n == 0:
        return "hela Sverige (0)"
    if n == 2:  # historic code for Stockholm city, still emitted alongside 1 in Situation data
        return "Stockholm (2)"
    return f"{COUNTIES.get(n, 'okänt län')} ({n})"


def county_list(value: Any) -> list[int]:
    """Normalise a CountyNo value (int or list) to a de-duplicated list of ints.
    Historic code 2 (Stockholm city) is folded into 1 (Stockholm county)."""
    if value is None:
        return []
    vals = value if isinstance(value, list) else [value]
    out: list[int] = []
    for v in vals:
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if n == 2:
            n = 1
        if n not in out:
            out.append(n)
    return out
