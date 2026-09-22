"""
Offline unit tests (no network, no API key). Run: pytest -q
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TRAFIKVERKET_API_KEY", "unit-test-key")

import trafikverket_client as tc  # noqa: E402
import tools_trafikverket as tt  # noqa: E402


# ── client: query builder ────────────────────────────────────────────


def test_build_request_contains_login_query_and_filters():
    xml = tc.build_request(
        "Camera",
        ["Id", "Name"],
        tc.f_and(tc.f_eq("Deleted", False), tc.f_eq("CountyNo", 1)),
        limit=5,
        orderby="Name",
        api_key="ABC",
    )
    assert xml.startswith('<REQUEST><LOGIN authenticationkey="ABC" />')
    assert 'objecttype="Camera" schemaversion="1.1"' in xml
    assert 'limit="5"' in xml and 'orderby="Name"' in xml
    assert '<AND><EQ name="Deleted" value="false" /><EQ name="CountyNo" value="1" /></AND>' in xml
    assert "<INCLUDE>Id</INCLUDE><INCLUDE>Name</INCLUDE>" in xml


def test_filter_helpers_escape_xml_and_collapse_single():
    assert tc.f_and(tc.f_eq("Name", "A&B")) == '<EQ name="Name" value="A&amp;B" />'
    assert tc.f_or() == ""
    assert tc.f_in("CountyNo", [1, 12]) == '<IN name="CountyNo" value="1,12" />'


def test_extract_error_envelope():
    payload = {"RESPONSE": {"RESULT": [{"ERROR": {"SOURCE": "Security", "MESSAGE": "Invalid authentication"}}]}}
    assert tc._extract_error(payload) == "Security: Invalid authentication"
    assert tc._extract_error({"RESPONSE": {"RESULT": [{"Camera": []}]}}) is None


def test_query_raises_on_api_error(monkeypatch):
    class FakeResp:
        status_code = 200
        text = ""

        def json(self):
            return {"RESPONSE": {"RESULT": [{"ERROR": {"SOURCE": "Security", "MESSAGE": "Invalid authentication"}}]}}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return FakeResp()

    monkeypatch.setattr(tc.httpx, "AsyncClient", FakeClient)
    with pytest.raises(tc.TrafikverketError, match="Invalid authentication"):
        asyncio.run(tc.query("Camera"))


def test_query_unwraps_result(monkeypatch):
    class FakeResp:
        status_code = 200
        text = ""

        def json(self):
            return {"RESPONSE": {"RESULT": [{"Camera": [{"Id": "1"}, {"Id": "2"}], "INFO": {}}]}}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return FakeResp()

    monkeypatch.setattr(tc.httpx, "AsyncClient", FakeClient)
    assert asyncio.run(tc.query("Camera")) == [{"Id": "1"}, {"Id": "2"}]


# ── client: helpers ──────────────────────────────────────────────────


def test_parse_point_and_haversine():
    assert tc.parse_point("POINT (18.0686 59.3293)") == (59.3293, 18.0686)
    assert tc.parse_point(None) is None
    d = tc.haversine_km(59.3293, 18.0686, 57.7089, 11.9746)  # Stockholm → Göteborg
    assert 395 < d < 400


def test_county_helpers():
    assert tc.county_name(1) == "Stockholm (1)"
    assert tc.county_name([12]) == "Skåne (12)"
    assert tc.county_list([1, "3"]) == [1, 3]
    assert tc.county_list(None) == []


def test_cache_roundtrip():
    tc.clear_cache()
    calls = []

    async def fetch():
        calls.append(1)
        return ["x"]

    async def run():
        a = await tc.cached("k", 60, fetch)
        b = await tc.cached("k", 60, fetch)
        return a, b

    a, b = asyncio.run(run())
    assert a == b == ["x"] and len(calls) == 1
    tc.clear_cache()


# ── tools: pure helpers ──────────────────────────────────────────────


def test_road_normalisation():
    assert tt._road_matches("E4", "e 4")
    assert tt._road_matches("Väg 262", "262")
    assert tt._road_matches("Väg 262", "väg 262")
    assert not tt._road_matches("E4", "E45")
    assert not tt._road_matches(None, "E4")


def test_clamp_limits():
    assert tt._clamp(None) == tt.DEFAULT_ROWS
    assert tt._clamp(500) == tt.MAX_ROWS
    assert tt._clamp(0) == 1
    assert tt._clamp("abc") == tt.DEFAULT_ROWS


def test_validate_county_and_latlon():
    assert tt._validate_county(None) is None
    assert tt._validate_county("12") == 12
    with pytest.raises(tc.TrafikverketError):
        tt._validate_county(2)
    assert tt._validate_latlon(59.33, 18.07) == (59.33, 18.07)
    with pytest.raises(tc.TrafikverketError, match="outside Sweden"):
        tt._validate_latlon(18.07, 59.33)  # swapped


def test_station_matching_prefers_exact_then_prefix():
    stations = [{"Name": "1302 Kullavik Fjärryta"}, {"Name": "Uppsala"}, {"Name": "1005 Uppsala N"}]
    hits = tt._station_matches(stations, "uppsala")
    assert [s["Name"] for s in hits] == ["Uppsala", "1005 Uppsala N"]
    assert tt._station_matches(stations, "%Fjärryta") == [stations[0]]
    assert tt._station_matches(stations, "nothing") == []


SAMPLE_STATION = {
    "Id": "1005",
    "Name": "1005 Galtsjön Fjärryta",
    "Geometry": {"WGS84": "POINT (15.1 59.2)"},
    "Observation": {
        "Sample": "2026-09-22T18:10:00.000+02:00",
        "Air": {"Temperature": {"Value": 8.2}, "RelativeHumidity": {"Value": 81.4}, "Dewpoint": {"Value": 5.1}},
        "Surface": {"Temperature": {"Value": 7.0}, "Ice": False, "Snow": False, "Water": True, "WaterDepth": {"Value": 0.3}},
        "Wind": [{"Speed": {"Value": 3.1}, "Direction": {"Value": 315}, "Height": 6}],
        "Weather": {"Precipitation": "rain"},
        "Aggregated30minutes": {"Wind": {"SpeedMax": {"Value": 6.0}}, "Precipitation": {"TotalWaterEquivalent": {"Value": 1.2}}},
    },
}


def test_format_station_is_readable_text():
    text = tt._format_station(SAMPLE_STATION, 4.2)
    assert "**1005 Galtsjön Fjärryta**" in text and "4.2 km away" in text
    assert "Air: 8.2°C" in text and "Road surface: 7.0°C" in text
    assert "from NW (315°)" in text and "gusts 6.0 m/s" in text
    assert "Precipitation: rain (1.2 mm" in text
    assert "water yes (0.3 mm)" in text
    assert "Observed: 2026-09-22 18:10 +02:00" in text
    assert "{" not in text  # never raw JSON


def test_get_weather_station_uses_cache_and_formats(monkeypatch):
    tc.clear_cache()

    async def fake_all():
        return [SAMPLE_STATION, {"Id": "2", "Name": "Other", "Geometry": {}, "Observation": {}}]

    monkeypatch.setattr(tt, "_all_stations", fake_all)
    out = asyncio.run(tt.get_weather_station("galtsjön"))
    assert "Found 1 station(s)" in out and "Air: 8.2°C" in out
    out2 = asyncio.run(tt.get_weather_station("zzz"))
    assert out2.startswith("No weather station matches")
    near = asyncio.run(tt.get_weather_stations_near_location(59.2, 15.1, 5, 10))
    assert "1 station(s) within 5 km" in near and "0.0 km" in near


def test_tools_return_error_string_instead_of_raising(monkeypatch):
    async def boom():
        raise tc.TrafikverketError("Security: Invalid authentication")

    monkeypatch.setattr(tt, "_all_stations", boom)
    out = asyncio.run(tt.list_weather_stations("x"))
    assert out == "Error: Security: Invalid authentication"


def test_deviation_flatten_sort_and_format():
    rows = [
        {"Id": "S1", "Deviation": [
            {"Header": "Olycka E4", "MessageType": "Olycka", "SeverityCode": 4, "SeverityText": "Stor påverkan",
             "RoadNumber": "E4", "CountyNo": [1], "StartTime": "2026-09-22T10:00:00.000+02:00", "Message": "Bil i diket."},
            {"Header": "Vägarbete", "MessageType": "Vägarbete", "SeverityCode": 2, "RoadNumber": "Väg 73", "CountyNo": [1]},
        ]},
    ]
    devs = tt._deviations(rows)
    assert len(devs) == 2 and devs[0]["_situation_id"] == "S1"
    text = tt._format_deviation(devs[0])
    assert "[Olycka, severity 4/5 Stor påverkan] **Olycka E4**" in text
    assert "Where: E4" in text and "Stockholm (1)" in text and "Bil i diket." in text


def test_get_traffic_situations_filters_and_summary(monkeypatch):
    rows = [
        {"Id": "S1", "Deviation": [
            {"Header": "Olycka E4", "MessageType": "Olycka", "SeverityCode": 4, "RoadNumber": "E4", "CountyNo": [1]},
            {"Header": "Vägarbete 73", "MessageType": "Vägarbete", "SeverityCode": 2, "RoadNumber": "Väg 73", "CountyNo": [1]},
        ]},
        {"Id": "S2", "Deviation": [
            {"Header": "Vägarbete E6", "MessageType": "Vägarbete", "SeverityCode": 1, "RoadNumber": "E6", "CountyNo": [12]},
        ]},
    ]

    async def fake_fetch(county):
        return rows if county is None else [r for r in rows if any(county in (d.get("CountyNo") or []) for d in r["Deviation"])]

    monkeypatch.setattr(tt, "_fetch_situations", fake_fetch)
    out = asyncio.run(tt.get_traffic_situations(road_number="e 4"))
    assert "1 traffic situation(s)" in out and "Olycka E4" in out
    out = asyncio.run(tt.get_traffic_situations(message_type="vägarbete", county=12))
    assert "Vägarbete E6" in out and "Vägarbete 73" not in out
    summary = asyncio.run(tt.get_traffic_situation_summary())
    assert "3 active deviation(s) in 2 situation(s)" in summary
    assert "Vägarbete: 2" in summary and "Olycka: 1" in summary
    assert "Stockholm (1): 2" in summary
    out = asyncio.run(tt.get_traffic_situations(county=99))
    assert out.startswith("Error: Unknown county number 99")


def test_camera_image_rejects_foreign_url():
    from fastmcp.exceptions import ToolError
    with pytest.raises(ToolError):
        asyncio.run(tt.get_camera_image("https://evil.example/x.jpg"))


def test_server_registers_all_tools_without_permission_prompt():
    os.environ.setdefault("MCP_SERVER_JWT_SECRET", "x" * 64)
    import server  # noqa: F401

    async def names():
        tools = await server.mcp.list_tools()
        return {t.name: t for t in tools}

    tools = asyncio.run(names())
    expected = {f.__name__ for f in tt.ALL_TOOLS}
    assert set(tools) == expected
    for t in tools.values():
        assert (t.meta or {}).get("requires_permission") is False, t.name


def test_camera_image_through_mcp_layer(monkeypatch):
    """A JPEG fetched by get_camera_image must reach the client as an MCP image content block."""
    os.environ.setdefault("MCP_SERVER_JWT_SECRET", "x" * 64)
    import server

    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 2000 + b"\xff\xd9"

    class FakeResp:
        status_code = 200
        headers = {"content-type": "image/jpeg"}
        content = jpeg

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            return FakeResp()

    monkeypatch.setattr(tt.httpx, "AsyncClient", FakeClient)

    async def call():
        from fastmcp import Client
        async with Client(server.mcp) as client:
            return await client.call_tool("get_camera_image", {"photo_url": "https://api.trafikinfo.trafikverket.se/v2/Images/x.jpg", "full_size": True})

    res = asyncio.run(call())
    block = res.content[0]
    assert block.type == "image" and block.mimeType == "image/jpeg"
    import base64
    assert base64.b64decode(block.data) == jpeg
