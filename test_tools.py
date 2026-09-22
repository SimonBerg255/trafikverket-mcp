"""
Live verification — calls the real Trafikverket API. Must exit 0 before the server is done.

Run:  python3 test_tools.py
Needs TRAFIKVERKET_API_KEY in .env (or the environment).
"""

import asyncio
import os
import sys
import traceback

from dotenv import load_dotenv

load_dotenv()

if not os.getenv("TRAFIKVERKET_API_KEY") or os.getenv("TRAFIKVERKET_API_KEY") == "REPLACE_WITH_YOUR_KEY":
    print("❌ TRAFIKVERKET_API_KEY is not set in .env – get a free key at https://data.trafikverket.se/")
    sys.exit(2)

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

results = []
_captured = {}


def test(name, coro, *args, allow_empty=False, min_len=20, **kwargs):
    print(f"\n{'=' * 60}\nTEST: {name}\n{'=' * 60}")
    try:
        result = asyncio.run(coro(*args, **kwargs))
        if not isinstance(result, str):
            # image tool
            data = getattr(result, "data", b"") or b""
            print(f"Image: {len(data)} bytes, format={getattr(result, 'format', None)}")
            if len(data) < 1000:
                raise ValueError("image too small – probably not a real photo")
        else:
            print(f"Output ({len(result)} chars):\n{result[:700]}")
            if result.startswith("Error:"):
                raise ValueError(result)
            if len(result) < min_len:
                raise ValueError(f"Response too short: {result!r}")
            if not allow_empty and result.lower().startswith("no "):
                raise ValueError(f"Empty result: {result[:120]!r}")
        _captured[name] = result
        results.append((name, True, None))
        print("✅ PASS")
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        results.append((name, False, str(e)[:200]))
        print(f"❌ FAIL: {e}")


# ── Weather ──────────────────────────────────────────────────────────
test("list_weather_stations (no filter)", list_weather_stations, None, 5)
test("list_weather_stations (pattern 'Uppsala')", list_weather_stations, "Uppsala")
test("get_weather_station ('Uppsala')", get_weather_station, "Uppsala")
test("get_weather_station ('Kiruna')", get_weather_station, "Kiruna")
test("get_weather_stations_near_location (Stockholm 30 km)", get_weather_stations_near_location, 59.33, 18.07, 30, 5)

# derive a real station ID from the near-location output for the history test
import re  # noqa: E402

m = re.search(r"\(ID (\d+)\)", _captured.get("get_weather_stations_near_location (Stockholm 30 km)", "") or "")
station_id = int(m.group(1)) if m else 1005
test(f"get_weather_observations (ID {station_id})", get_weather_observations, station_id, 5)

# ── Cameras ──────────────────────────────────────────────────────────
test("get_cameras (county 1 Stockholm)", get_cameras, None, 1, 5)
test("get_cameras (pattern 'E4')", get_cameras, "E4", None, 5)
test("get_cameras_near_location (Göteborg 20 km)", get_cameras_near_location, 57.71, 11.97, 20, None, 5)
m = re.search(r"Photo: (https://\S+?)(?: \(|\s)", _captured.get("get_cameras (county 1 Stockholm)", "") or "")
if m:
    test("get_camera_image (first Stockholm camera)", get_camera_image, m.group(1))
else:
    results.append(("get_camera_image", False, "no PhotoUrl captured from get_cameras"))

# ── Traffic ──────────────────────────────────────────────────────────
test("get_traffic_situations (county 1 Stockholm)", get_traffic_situations, None, 1, None, 5)
test("get_traffic_situations (road E4, all counties)", get_traffic_situations, "E4", None, None, 5, allow_empty=True)
test("get_traffic_situations (Vägarbete, Skåne)", get_traffic_situations, None, 12, "Vägarbete", 5, allow_empty=True)
test("get_traffic_situation_summary (Sweden)", get_traffic_situation_summary)
test("get_traffic_flow (county 1 Stockholm)", get_traffic_flow, 1, None, None, 10, 5)
test("get_traffic_flow (near Stockholm centre 10 km)", get_traffic_flow, None, 59.33, 18.07, 10, 5)
test("get_road_conditions (county 25 Norrbotten)", get_road_conditions, None, 25, 5, allow_empty=True)
test("get_road_conditions (all, no filter)", get_road_conditions, None, None, 5, allow_empty=True)
test("list_counties", list_counties)

# ── Summary ──────────────────────────────────────────────────────────
print(f"\n{'=' * 60}\nVERIFICATION SUMMARY\n{'=' * 60}")
for name, ok, err in results:
    print(f"  {'✅' if ok else '❌'} {name}" + (f" — {err}" if err else ""))
passed = sum(1 for _, ok, _ in results if ok)
print(f"\n{passed}/{len(results)} passed")
if passed < len(results):
    print("\n❌ NOT DONE — fix failing tools and re-run")
    sys.exit(1)
print("\n🎉 ALL PASSED — server ready for Intric")
sys.exit(0)
