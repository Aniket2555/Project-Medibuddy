"""Weather client: every failure mode must surface as a WeatherDataError subclass."""

import httpx
import pytest

from app.weather import (
    FailingWeatherClient,
    Location,
    LocationNotFound,
    OpenMeteoClient,
    StaticWeatherClient,
    WeatherDataError,
    WeatherUnavailable,
    validate_forecast,
)

BHOPAL = Location("Bhopal", 23.25, 77.40, "India", "Madhya Pradesh")


class FakeResponse:
    def __init__(self, status=200, payload=None, text=None):
        self.status_code = status
        self._payload = payload
        self.text = text if text is not None else str(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def patch_get(monkeypatch, response=None, exc=None):
    def fake_get(url, params=None, timeout=None):
        if exc:
            raise exc
        return response

    monkeypatch.setattr(httpx, "get", fake_get)


# --- geocoding -------------------------------------------------------------

def test_geocode_picks_first_result(monkeypatch):
    patch_get(monkeypatch, FakeResponse(payload={"results": [
        {"name": "Bhopal", "latitude": 23.25, "longitude": 77.4, "country": "India", "admin1": "Madhya Pradesh"},
        {"name": "Bhopāl", "latitude": 24.4, "longitude": 78.1},
    ]}))
    loc = OpenMeteoClient().geocode("Bhopal")
    assert (loc.latitude, loc.longitude) == (23.25, 77.4)
    assert loc.label == "Bhopal, Madhya Pradesh, India"


def test_geocode_unknown_city_is_location_not_found(monkeypatch):
    # The real API returns HTTP 200 with no "results" key for unknown names.
    patch_get(monkeypatch, FakeResponse(payload={"generationtime_ms": 0.1}))
    with pytest.raises(LocationNotFound):
        OpenMeteoClient().geocode("Xyzzyville")


def test_geocode_empty_name_never_calls_api(monkeypatch):
    patch_get(monkeypatch, exc=AssertionError("should not be called"))
    with pytest.raises(LocationNotFound):
        OpenMeteoClient().geocode("   ")


def test_geocode_network_error_is_same_fallback(monkeypatch):
    # The brief: geocoding failure = same honest fallback as weather API down.
    patch_get(monkeypatch, exc=httpx.ConnectTimeout("timed out"))
    with pytest.raises(WeatherDataError):
        OpenMeteoClient().geocode("Bhopal")


# --- forecast --------------------------------------------------------------

def test_forecast_timeout_is_weather_unavailable(monkeypatch):
    patch_get(monkeypatch, exc=httpx.ReadTimeout("timed out"))
    with pytest.raises(WeatherUnavailable):
        OpenMeteoClient().forecast(BHOPAL)


def test_forecast_http_error_is_weather_unavailable(monkeypatch):
    patch_get(monkeypatch, FakeResponse(status=503, payload=None, text="Service Unavailable"))
    with pytest.raises(WeatherUnavailable):
        OpenMeteoClient().forecast(BHOPAL)


def test_forecast_api_error_flag(monkeypatch):
    patch_get(monkeypatch, FakeResponse(status=200, payload={"error": True, "reason": "bad latitude"}))
    with pytest.raises(WeatherUnavailable):
        OpenMeteoClient().forecast(BHOPAL)


def test_forecast_metadata_only_response_is_rejected(monkeypatch):
    # Without field lists, Open-Meteo returns 200 + metadata only. Must not read as "calm".
    patch_get(monkeypatch, FakeResponse(payload={"latitude": 23.2, "longitude": 77.4, "elevation": 511.0}))
    with pytest.raises(WeatherUnavailable):
        OpenMeteoClient().forecast(BHOPAL)


def test_forecast_missing_field_is_rejected(fixture):
    data = fixture("live_bhopal")
    del data["current"]["uv_index"]
    with pytest.raises(WeatherUnavailable, match="uv_index"):
        validate_forecast(data)


def test_forecast_valid_payload_passes(monkeypatch, fixture):
    data = fixture("live_bhopal")
    patch_get(monkeypatch, FakeResponse(payload=data))
    assert OpenMeteoClient().forecast(BHOPAL)["current"]["time"] == data["current"]["time"]


# --- test doubles ------------------------------------------------------------

def test_failing_client_simulates_outage():
    with pytest.raises(WeatherUnavailable):
        FailingWeatherClient("forecast").forecast(BHOPAL)
    with pytest.raises(LocationNotFound):
        FailingWeatherClient("geocode").geocode("Bhopal")


def test_static_client_serves_fixture(fixture):
    client = StaticWeatherClient(fixture("synthetic_calm"))
    loc = client.geocode("pune")
    assert loc.name == "Pune"
    assert "hourly" in client.forecast(loc)
