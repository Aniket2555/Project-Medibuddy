"""Generate synthetic Open-Meteo forecast payloads for deterministic tests/evals.

Live weather changes daily (the September Madhya Pradesh rain system had already
passed by the time this was built), so every "severe" scenario also exists as a
synthetic fixture with the exact Open-Meteo response shape.

    python -m evals.fixtures.build_synthetic      # (re)writes synthetic_*.json
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from app.weather import CURRENT_FIELDS, DAILY_FIELDS, HOURLY_FIELDS

OUT_DIR = Path(__file__).resolve().parent
NOW = "2026-09-03T08:00"  # local time of the "current" observation

BASE_HOUR = {
    "temperature_2m": 26.0,
    "apparent_temperature": 27.0,
    "precipitation": 0.0,
    "precipitation_probability": 5,
    "weather_code": 1,
    "wind_speed_10m": 8.0,
    "wind_gusts_10m": 15.0,
    "uv_index": 0.0,
    "visibility": 20000.0,
}

# Rough diurnal UV curve, scaled by `uv_peak`
UV_SHAPE = {7: 0.1, 8: 0.2, 9: 0.4, 10: 0.6, 11: 0.8, 12: 0.95, 13: 1.0, 14: 0.9, 15: 0.7, 16: 0.45, 17: 0.2, 18: 0.05}


def make_forecast(
    *,
    uv_peak: float = 6.0,
    temp_peak: float = 30.0,
    temp_low: float = 22.0,
    # (from_hour, to_hour) inclusive applies every day; (from_hour, to_hour, day_index) applies to one day
    hour_overrides: dict[tuple, dict] | None = None,
    location: dict | None = None,
) -> dict:
    start = datetime.fromisoformat(NOW).replace(hour=0)
    hourly: dict[str, list] = {"time": []}
    for f in HOURLY_FIELDS:
        hourly[f] = []
    for h in range(72):
        t = start + timedelta(hours=h)
        row = dict(BASE_HOUR)
        frac = max(0.0, 1 - abs(t.hour - 14) / 10)  # temp peaks ~14:00
        row["temperature_2m"] = round(temp_low + (temp_peak - temp_low) * frac, 1)
        row["apparent_temperature"] = round(row["temperature_2m"] + 1.5, 1)
        row["uv_index"] = round(uv_peak * UV_SHAPE.get(t.hour, 0.0), 1)
        for key, ov in (hour_overrides or {}).items():
            a, b, *day = key
            if a <= t.hour <= b and (not day or day[0] == h // 24):
                row.update(ov)
        hourly["time"].append(t.strftime("%Y-%m-%dT%H:%M"))
        for f in HOURLY_FIELDS:
            hourly[f].append(row[f])

    daily: dict[str, list] = {"time": []}
    for f in DAILY_FIELDS:
        daily[f] = []
    for d in range(3):
        sl = slice(d * 24, (d + 1) * 24)
        codes = hourly["weather_code"][sl]
        severe = [c for c in codes if c >= 45] or codes
        daily["time"].append((start + timedelta(days=d)).strftime("%Y-%m-%d"))
        daily["precipitation_sum"].append(round(sum(hourly["precipitation"][sl]), 1))
        daily["precipitation_probability_max"].append(max(hourly["precipitation_probability"][sl]))
        daily["weather_code"].append(max(severe))
        daily["wind_gusts_10m_max"].append(max(hourly["wind_gusts_10m"][sl]))
        daily["temperature_2m_max"].append(max(hourly["temperature_2m"][sl]))
        daily["uv_index_max"].append(max(hourly["uv_index"][sl]))

    i_now = hourly["time"].index(NOW)
    current = {"time": NOW, "interval": 900}
    for f in CURRENT_FIELDS:
        current[f] = hourly[f][i_now]

    payload = {"timezone": "Asia/Kolkata", "current": current, "hourly": hourly, "daily": daily}
    if location:
        payload["_location"] = location
    return payload


BHOPAL = {"name": "Bhopal", "latitude": 23.2547, "longitude": 77.4029,
          "country": "India", "admin1": "Madhya Pradesh", "timezone": "Asia/Kolkata"}

RAINY = {"weather_code": 63, "precipitation": 2.5, "precipitation_probability": 95,
         "wind_gusts_10m": 42.0, "wind_speed_10m": 22.0, "visibility": 4000.0}

SCENARIOS = {
    # IMD-style "heavy to very heavy" event: one day crosses the 64.5 mm hard trigger.
    "synthetic_rain_system_extreme": make_forecast(
        uv_peak=1.5, temp_peak=25, temp_low=22,
        hour_overrides={(0, 23): {**RAINY, "weather_code": 65, "precipitation": 3.6, "wind_gusts_10m": 55.0}},
        location=BHOPAL,
    ),
    # The brief's hard case: no single number is extreme (~1.2 mm/h, no heavy-rain code,
    # no day above 64.5 mm), but a multi-day soaking + squally gusts is a system.
    "synthetic_rain_system_subtle": make_forecast(
        uv_peak=2, temp_peak=26, temp_low=23,
        hour_overrides={(0, 23): {**RAINY, "precipitation": 1.2, "precipitation_probability": 90}},
        location=BHOPAL,
    ),
    # A pleasant day: dry, mild, light wind, moderate UV.
    "synthetic_calm": make_forecast(uv_peak=5, temp_peak=27, temp_low=19),
    # Very high midday UV (index 10), otherwise fine.
    "synthetic_high_uv": make_forecast(uv_peak=10.5, temp_peak=33, temp_low=24),
    # Strong, gusty wind all day, dry.
    "synthetic_strong_wind": make_forecast(
        uv_peak=6, hour_overrides={(0, 23): {"wind_speed_10m": 35.0, "wind_gusts_10m": 58.0}},
    ),
    # Dense morning fog (visibility 400 m) that clears by noon.
    "synthetic_fog_morning": make_forecast(
        uv_peak=4, temp_peak=22, temp_low=12,
        hour_overrides={(4, 10): {"weather_code": 45, "visibility": 400.0}},
    ),
    # Heatwave: feels-like ~44 °C in the afternoon.
    "synthetic_heatwave": make_forecast(uv_peak=9, temp_peak=42.5, temp_low=30),
    # One isolated afternoon thunderstorm today (dry morning, dry following days): not a system.
    "synthetic_thunderstorm_afternoon": make_forecast(
        uv_peak=7, temp_peak=32,
        hour_overrides={(14, 18, 0): {"weather_code": 95, "precipitation": 3.0, "precipitation_probability": 85,
                                   "wind_gusts_10m": 48.0}},
    ),
    # Light drizzle in the morning, nothing severe.
    "synthetic_light_rain": make_forecast(
        uv_peak=3, temp_peak=24, temp_low=18,
        hour_overrides={(6, 11): {"weather_code": 51, "precipitation": 0.4, "precipitation_probability": 70}},
    ),
}


def main() -> None:
    for name, payload in SCENARIOS.items():
        path = OUT_DIR / f"{name}.json"
        path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        print(f"wrote {path.name}")


if __name__ == "__main__":
    main()
