"""Raw Open-Meteo JSON -> a flat, typed dict of "facts".

This is deterministic code with no LLM involved. The facts dict is:
  * what SOP conditions are evaluated against (Phase 3), and
  * the ONLY source of numbers the bot may quote back (Phase 4 grounding).

Every fact is declared in FACT_CATALOG with a unit and a description. The SOP
loader rejects conditions that reference undeclared facts, and the composer
only gets catalogued values.

"Now" is taken from the API's own `current.time` (the location's local time),
not the server clock, so the same payload always produces the same facts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from app.ops import compare
from app.weather import WeatherDataError

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "facts_config.yaml"

DAY_OFFSETS = {"today": 0, "tomorrow": 1, "day_after": 2}

# WMO weather interpretation codes (Open-Meteo `weather_code`).
WMO_DESCRIPTIONS = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "depositing rime fog",
    51: "light drizzle", 53: "moderate drizzle", 55: "dense drizzle",
    56: "light freezing drizzle", 57: "dense freezing drizzle",
    61: "slight rain", 63: "moderate rain", 65: "heavy rain",
    66: "light freezing rain", 67: "heavy freezing rain",
    71: "slight snow", 73: "moderate snow", 75: "heavy snow", 77: "snow grains",
    80: "slight rain showers", 81: "moderate rain showers", 82: "violent rain showers",
    85: "slight snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with slight hail", 99: "thunderstorm with heavy hail",
}
RAIN_CODES = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}
HEAVY_RAIN_CODES = {65, 67, 82, 95, 96, 99}
THUNDERSTORM_CODES = {95, 96, 99}
FOG_CODES = {45, 48}


# name -> (type, unit, description). The single registry of what facts exist.
FACT_CATALOG: dict[str, tuple[str, str, str]] = {
    # context
    "location_name":            ("str",   "",     "Resolved location label"),
    "data_time":                ("str",   "",     "Local time of the 'current' observation"),
    "window_label":             ("str",   "",     "Human label of the time window asked about"),
    "window_start":             ("str",   "",     "First hour of the window (local)"),
    "window_end":               ("str",   "",     "Last hour of the window (local)"),
    "window_hours":             ("int",   "h",    "Number of forecast hours in the window"),
    "weather_description":      ("str",   "",     "Current conditions in words (WMO code)"),
    "window_description":       ("str",   "",     "Most significant conditions in the window, in words"),
    # current snapshot
    "current_temp_c":           ("float", "°C",   "Current air temperature"),
    "current_apparent_temp_c":  ("float", "°C",   "Current feels-like temperature"),
    "current_precip_mm":        ("float", "mm",   "Precipitation in the current interval"),
    "current_precip_prob":      ("int",   "%",    "Current precipitation probability"),
    "current_wind_kmh":         ("float", "km/h", "Current wind speed at 10 m"),
    "current_gusts_kmh":        ("float", "km/h", "Current wind gusts at 10 m"),
    "current_uv":               ("float", "",     "Current UV index"),
    "current_visibility_km":    ("float", "km",   "Current visibility"),
    "current_weather_code":     ("int",   "",     "Current WMO weather code"),
    # aggregated over the requested window (hourly data)
    "window_temp_max_c":        ("float", "°C",   "Max air temperature in the window"),
    "window_temp_min_c":        ("float", "°C",   "Min air temperature in the window"),
    "window_apparent_temp_max_c": ("float", "°C", "Max feels-like temperature in the window"),
    "window_precip_total_mm":   ("float", "mm",   "Total precipitation over the window"),
    "window_precip_max_mm_h":   ("float", "mm/h", "Wettest single hour in the window"),
    "window_precip_prob_max":   ("int",   "%",    "Highest precipitation probability in the window"),
    "window_wind_max_kmh":      ("float", "km/h", "Max wind speed in the window"),
    "window_gusts_max_kmh":     ("float", "km/h", "Max wind gusts in the window"),
    "window_uv_max":            ("float", "",     "Max UV index in the window"),
    "window_visibility_min_km": ("float", "km",   "Lowest visibility in the window"),
    "window_weather_codes":     ("list",  "",     "Distinct WMO codes in the window"),
    "window_has_rain":          ("bool",  "",     "Any rain/drizzle/storm hour in the window"),
    "window_has_heavy_rain":    ("bool",  "",     "Any heavy-rain or thunderstorm code in the window"),
    "window_has_thunderstorm":  ("bool",  "",     "Any thunderstorm code in the window"),
    "window_has_fog":           ("bool",  "",     "Any fog code in the window"),
    # the target day (daily data)
    "day_precip_sum_mm":        ("float", "mm",   "Total precipitation forecast for the target day"),
    "day_precip_prob_max":      ("int",   "%",    "Max precipitation probability for the target day"),
    "day_gusts_max_kmh":        ("float", "km/h", "Max gusts forecast for the target day"),
    "day_temp_max_c":           ("float", "°C",   "Max temperature for the target day"),
    "day_uv_max":               ("float", "",     "Max UV index for the target day"),
    # 3-day outlook (rain-system signals)
    "precip_3day_mm":           ("float", "mm",   "Total precipitation over the 3-day forecast"),
    "precip_max_day_mm":        ("float", "mm",   "Wettest single day in the 3-day forecast"),
    "precip_prob_max_3day":     ("int",   "%",    "Highest daily precipitation probability, 3 days"),
    "high_precip_prob_days_3day": ("int", "days", "Days in the 3-day forecast at/above the high-chance threshold"),
    "gusts_max_3day_kmh":       ("float", "km/h", "Highest daily max gust, 3 days"),
    "heavy_rain_code_3day":     ("bool",  "",     "Any heavy-rain/storm daily code in 3 days"),
    # derived situational flag
    "heavy_rain_system":        ("bool",  "",     "Several rain-system signals agree (see config)"),
    "heavy_rain_signal_count":  ("int",   "",     "How many rain-system signals fired"),
    "heavy_rain_signals":       ("list",  "",     "Names of the rain-system signals that fired"),
}


class WindowUnavailable(WeatherDataError):
    """The requested time is outside the forecast we actually have."""

    user_message = "I don't have forecast data for that time."


@dataclass(frozen=True)
class TimeWindow:
    day: str = "today"   # today | tomorrow | day_after
    part: str = "now"    # now | morning | afternoon | evening | night | whole_day

    @property
    def label(self) -> str:
        if self.part == "now":
            return "right now"
        day = {"today": "today", "tomorrow": "tomorrow", "day_after": "the day after tomorrow"}.get(self.day, self.day)
        if self.part == "whole_day":
            return f"{day} (whole day)"
        if self.day == "today":
            return "tonight" if self.part == "night" else f"this {self.part}"
        return f"{day} {self.part}"


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _r(x: float | None, nd: int = 1) -> float | None:
    return None if x is None else round(float(x), nd)


def _max(values: list) -> Any:
    vals = [v for v in values if v is not None]
    return max(vals) if vals else None


def _min(values: list) -> Any:
    vals = [v for v in values if v is not None]
    return min(vals) if vals else None


def _sum(values: list) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) if vals else None


def _window_indices(hourly_times: list[str], now: datetime, window: TimeWindow, cfg: dict) -> list[int]:
    if window.day not in DAY_OFFSETS:
        raise WindowUnavailable(f"unknown day {window.day!r}")
    spec = cfg["time_windows"].get(window.part)
    if spec is None:
        raise WindowUnavailable(f"unknown window part {window.part!r}")

    now_hour = now.replace(minute=0, second=0, microsecond=0)
    if window.part == "now":
        if window.day != "today":
            raise WindowUnavailable("'now' only makes sense for today")
        start, end = now_hour, now_hour + timedelta(hours=spec["hours_ahead"])
    else:
        target: date = now.date() + timedelta(days=DAY_OFFSETS[window.day])
        start = datetime.combine(target, datetime.min.time()).replace(hour=spec["start"])
        end = datetime.combine(target, datetime.min.time()).replace(hour=spec["end"])
        start = max(start, now_hour)  # never report hours that already passed

    idx = [i for i, t in enumerate(hourly_times) if start <= datetime.fromisoformat(t) <= end]
    if not idx:
        raise WindowUnavailable(f"no forecast hours for {window.label} (already passed or beyond forecast)")
    return idx


def _rain_system(facts: dict[str, Any], cfg: dict) -> tuple[bool, list[str]]:
    rs = cfg["heavy_rain_system"]
    fired = [
        name
        for name, sig in rs["signals"].items()
        if compare(facts.get(sig["fact"]), sig["op"], sig["value"])
    ]
    hard = facts.get("precip_max_day_mm") is not None and facts["precip_max_day_mm"] >= rs["hard_trigger_daily_mm"]
    if hard and "hard_trigger_daily" not in fired:
        fired.append("hard_trigger_daily")
    return (hard or len([f for f in fired if f != "hard_trigger_daily"]) >= rs["min_signals"]), fired


def _most_significant_code(codes: list[int]) -> int | None:
    """Pick the code to describe a window: storms > heavy rain > rain > fog > highest."""
    if not codes:
        return None
    for group in (THUNDERSTORM_CODES, HEAVY_RAIN_CODES, RAIN_CODES, FOG_CODES):
        hits = [c for c in codes if c in group]
        if hits:
            return max(hits)
    return max(codes)


def derive_facts(
    forecast: dict[str, Any],
    window: TimeWindow = TimeWindow(),
    location_label: str | None = None,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the facts dict. Raises WindowUnavailable if the window has no data."""
    cfg = cfg or load_config()
    cur, hourly, daily = forecast["current"], forecast["hourly"], forecast["daily"]
    now = datetime.fromisoformat(cur["time"])

    idx = _window_indices(hourly["time"], now, window, cfg)
    pick = lambda field: [hourly[field][i] for i in idx]  # noqa: E731

    codes_w = sorted({c for c in pick("weather_code") if c is not None})
    vis_w = _min(pick("visibility"))
    win_code = _most_significant_code(codes_w)

    target_day = (now.date() + timedelta(days=DAY_OFFSETS[window.day])).isoformat()
    try:
        d = daily["time"].index(target_day)
    except ValueError as exc:
        raise WindowUnavailable(f"no daily forecast for {target_day}") from exc

    daily_codes = [c for c in daily["weather_code"] if c is not None]

    facts: dict[str, Any] = {
        "location_name": location_label,
        "data_time": cur["time"],
        "window_label": window.label,
        "window_start": hourly["time"][idx[0]],
        "window_end": hourly["time"][idx[-1]],
        "window_hours": len(idx),
        "weather_description": WMO_DESCRIPTIONS.get(cur["weather_code"], f"code {cur['weather_code']}"),
        "window_description": WMO_DESCRIPTIONS.get(win_code, f"code {win_code}") if win_code is not None else None,
        "current_temp_c": _r(cur["temperature_2m"]),
        "current_apparent_temp_c": _r(cur["apparent_temperature"]),
        "current_precip_mm": _r(cur["precipitation"]),
        "current_precip_prob": cur["precipitation_probability"],
        "current_wind_kmh": _r(cur["wind_speed_10m"]),
        "current_gusts_kmh": _r(cur["wind_gusts_10m"]),
        "current_uv": _r(cur["uv_index"]),
        "current_visibility_km": _r(cur["visibility"] / 1000 if cur["visibility"] is not None else None),
        "current_weather_code": cur["weather_code"],
        "window_temp_max_c": _r(_max(pick("temperature_2m"))),
        "window_temp_min_c": _r(_min(pick("temperature_2m"))),
        "window_apparent_temp_max_c": _r(_max(pick("apparent_temperature"))),
        "window_precip_total_mm": _r(_sum(pick("precipitation"))),
        "window_precip_max_mm_h": _r(_max(pick("precipitation"))),
        "window_precip_prob_max": _max(pick("precipitation_probability")),
        "window_wind_max_kmh": _r(_max(pick("wind_speed_10m"))),
        "window_gusts_max_kmh": _r(_max(pick("wind_gusts_10m"))),
        "window_uv_max": _r(_max(pick("uv_index"))),
        "window_visibility_min_km": _r(vis_w / 1000 if vis_w is not None else None),
        "window_weather_codes": codes_w,
        "window_has_rain": any(c in RAIN_CODES for c in codes_w)
        or any((p or 0) >= 0.1 for p in pick("precipitation")),
        "window_has_heavy_rain": any(c in HEAVY_RAIN_CODES for c in codes_w),
        "window_has_thunderstorm": any(c in THUNDERSTORM_CODES for c in codes_w),
        "window_has_fog": any(c in FOG_CODES for c in codes_w),
        "day_precip_sum_mm": _r(daily["precipitation_sum"][d]),
        "day_precip_prob_max": daily["precipitation_probability_max"][d],
        "day_gusts_max_kmh": _r(daily["wind_gusts_10m_max"][d]),
        "day_temp_max_c": _r(daily["temperature_2m_max"][d]),
        "day_uv_max": _r(daily["uv_index_max"][d]),
        "precip_3day_mm": _r(_sum(daily["precipitation_sum"])),
        "precip_max_day_mm": _r(_max(daily["precipitation_sum"])),
        "precip_prob_max_3day": _max(daily["precipitation_probability_max"]),
        "high_precip_prob_days_3day": sum(
            1 for p in daily["precipitation_probability_max"]
            if p is not None and p >= cfg["heavy_rain_system"]["high_prob_day_pct"]
        ),
        "gusts_max_3day_kmh": _r(_max(daily["wind_gusts_10m_max"])),
        "heavy_rain_code_3day": any(c in HEAVY_RAIN_CODES for c in daily_codes),
    }
    flag, fired = _rain_system(facts, cfg)
    facts["heavy_rain_system"] = flag
    facts["heavy_rain_signals"] = fired
    facts["heavy_rain_signal_count"] = len(fired)
    return facts


def format_facts(facts: dict[str, Any]) -> str:
    """Readable dump (CLI / debugging / the frontend's 'why?' panel)."""
    lines = []
    for name, (_, unit, desc) in FACT_CATALOG.items():
        val = facts.get(name)
        shown = f"{val} {unit}".strip() if val is not None else "—"
        lines.append(f"  {name:<28} {shown:<24} {desc}")
    return "\n".join(lines)
