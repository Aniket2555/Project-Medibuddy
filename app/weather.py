"""Weather data access: Open-Meteo geocoding + forecast.

Every failure (network error, timeout, HTTP error, empty geocoding result,
response missing the requested fields) is raised as a subclass of
`WeatherDataError`. The graph routes *all* of them to the same honest
"I can't get the weather right now" answer: we never guess a forecast.

The `WeatherClient` protocol exists so evals and tests can inject recorded or
synthetic data (or a simulated outage) without patching the graph.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Open-Meteo only returns the fields you explicitly ask for. These lists are the
# single source of truth for what the rest of the app can rely on.
CURRENT_FIELDS = [
    "temperature_2m",
    "apparent_temperature",
    "precipitation",
    "precipitation_probability",
    "weather_code",
    "wind_speed_10m",
    "wind_gusts_10m",
    "uv_index",
    "visibility",
]
HOURLY_FIELDS = [
    "temperature_2m",
    "apparent_temperature",
    "precipitation",
    "precipitation_probability",
    "weather_code",
    "wind_speed_10m",
    "wind_gusts_10m",
    "uv_index",
    "visibility",
]
DAILY_FIELDS = [
    "precipitation_sum",
    "precipitation_probability_max",
    "weather_code",
    "wind_gusts_10m_max",
    "temperature_2m_max",
    "uv_index_max",
]
FORECAST_DAYS = 3


class WeatherDataError(Exception):
    """Base class: we could not obtain trustworthy weather data."""

    user_message = "I couldn't get live weather data right now."


class LocationNotFound(WeatherDataError):
    user_message = "I couldn't find that location."


class WeatherUnavailable(WeatherDataError):
    user_message = "The weather service is unavailable right now."


@dataclass(frozen=True)
class Location:
    name: str
    latitude: float
    longitude: float
    country: str | None = None
    admin1: str | None = None  # state / province
    timezone: str | None = None

    @property
    def label(self) -> str:
        parts = [self.name, self.admin1, self.country]
        return ", ".join(p for p in parts if p)


class WeatherClient(Protocol):
    def geocode(self, name: str) -> Location: ...

    def forecast(self, location: Location) -> dict[str, Any]: ...


class OpenMeteoClient:
    """Real client. Picks the first geocoding result (documented default)."""

    def __init__(self, timeout: float | None = None):
        self.timeout = timeout or float(os.getenv("HTTP_TIMEOUT", "10"))

    def _get(self, url: str, params: dict[str, Any], error_cls: type[WeatherDataError]) -> dict:
        try:
            resp = httpx.get(url, params=params, timeout=self.timeout)
        except httpx.HTTPError as exc:  # timeouts, DNS, connection refused, ...
            raise error_cls(f"request failed: {exc!r}") from exc
        if resp.status_code != 200:
            raise error_cls(f"HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise error_cls("response was not JSON") from exc
        if isinstance(data, dict) and data.get("error"):
            raise error_cls(f"API error: {data.get('reason')}")
        return data

    def geocode(self, name: str) -> Location:
        name = (name or "").strip()
        if not name:
            raise LocationNotFound("empty location name")
        # Any failure here, including the API being down, is a LocationNotFound:
        # the brief treats "can't resolve a location" the same as the weather API failing.
        data = self._get(GEOCODING_URL, {"name": name, "count": 1}, LocationNotFound)
        results = data.get("results") or []  # unknown names return 200 with no "results"
        if not results:
            raise LocationNotFound(f"no geocoding results for {name!r}")
        top = results[0]
        return Location(
            name=top["name"],
            latitude=top["latitude"],
            longitude=top["longitude"],
            country=top.get("country"),
            admin1=top.get("admin1"),
            timezone=top.get("timezone"),
        )

    def forecast(self, location: Location) -> dict[str, Any]:
        params = {
            "latitude": location.latitude,
            "longitude": location.longitude,
            "timezone": "auto",  # times come back in the location's local time
            "forecast_days": FORECAST_DAYS,
            "current": ",".join(CURRENT_FIELDS),
            "hourly": ",".join(HOURLY_FIELDS),
            "daily": ",".join(DAILY_FIELDS),
        }
        data = self._get(FORECAST_URL, params, WeatherUnavailable)
        validate_forecast(data)
        return data


def validate_forecast(data: dict[str, Any]) -> None:
    """Reject responses that don't carry the values we asked for.

    Without field lists, Open-Meteo returns 200 with metadata only. That must
    be treated as "no data", not as "calm weather".
    """
    for block, fields in (("current", CURRENT_FIELDS), ("hourly", HOURLY_FIELDS), ("daily", DAILY_FIELDS)):
        section = data.get(block)
        if not isinstance(section, dict):
            raise WeatherUnavailable(f"forecast response missing '{block}' block")
        missing = [f for f in fields if f not in section]
        if missing:
            raise WeatherUnavailable(f"'{block}' block missing fields: {missing}")
        if block != "current" and not section.get("time"):
            raise WeatherUnavailable(f"'{block}' block has no time steps")


# --- Test / eval doubles ---------------------------------------------------


class StaticWeatherClient:
    """Serves a fixed forecast payload (recorded or synthetic) for any location."""

    def __init__(self, forecast: dict[str, Any], location: Location | None = None):
        validate_forecast(forecast)
        self._forecast = forecast
        self._location = location

    def geocode(self, name: str) -> Location:
        if self._location:
            return self._location
        return Location(name=name.strip().title(), latitude=0.0, longitude=0.0)

    def forecast(self, location: Location) -> dict[str, Any]:
        return self._forecast


class FailingWeatherClient:
    """Simulates an outage. `fail_on` is 'geocode' or 'forecast'."""

    def __init__(self, fail_on: str = "forecast"):
        self.fail_on = fail_on

    def geocode(self, name: str) -> Location:
        if self.fail_on == "geocode":
            raise LocationNotFound("simulated geocoding outage")
        return Location(name=name.strip().title(), latitude=0.0, longitude=0.0)

    def forecast(self, location: Location) -> dict[str, Any]:
        raise WeatherUnavailable("simulated forecast API outage")
