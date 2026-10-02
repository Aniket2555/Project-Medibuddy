"""Facts derivation: deterministic, catalogued, and honest about missing windows."""

import pytest

from app.facts import FACT_CATALOG, TimeWindow, WindowUnavailable, derive_facts
from app.weather import WeatherDataError

ALL_FIXTURES = [
    "live_bhopal", "live_delhi", "live_jakarta", "live_mumbai",
    "synthetic_calm", "synthetic_high_uv", "synthetic_strong_wind", "synthetic_fog_morning",
    "synthetic_heatwave", "synthetic_thunderstorm_afternoon", "synthetic_light_rain",
    "synthetic_rain_system_extreme", "synthetic_rain_system_subtle",
]


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_facts_match_catalog_exactly(fixture, name):
    """Every produced fact is declared, and every declared fact is produced."""
    facts = derive_facts(fixture(name), TimeWindow("today", "whole_day"), "X")
    assert set(facts) == set(FACT_CATALOG)


def test_deterministic(fixture):
    data = fixture("synthetic_high_uv")
    assert derive_facts(data, TimeWindow("today", "afternoon")) == derive_facts(data, TimeWindow("today", "afternoon"))


def test_now_comes_from_payload_not_server_clock(fixture):
    facts = derive_facts(fixture("synthetic_calm"), TimeWindow("today", "now"))
    assert facts["data_time"] == "2026-09-03T08:00"
    assert facts["window_start"] == "2026-09-03T08:00"
    assert facts["window_end"] == "2026-09-03T10:00"
    assert facts["window_hours"] == 3


# --- time windows ------------------------------------------------------------

def test_uv_depends_on_window(fixture):
    """High midday UV must show up for the afternoon, not for the evening."""
    data = fixture("synthetic_high_uv")
    assert derive_facts(data, TimeWindow("today", "afternoon"))["window_uv_max"] >= 8
    assert derive_facts(data, TimeWindow("today", "evening"))["window_uv_max"] < 3


def test_fog_only_in_morning(fixture):
    data = fixture("synthetic_fog_morning")
    morning = derive_facts(data, TimeWindow("today", "morning"))
    afternoon = derive_facts(data, TimeWindow("today", "afternoon"))
    assert morning["window_has_fog"] and morning["window_visibility_min_km"] < 1
    assert not afternoon["window_has_fog"]


def test_today_window_skips_past_hours(fixture):
    # NOW is 08:00, so "this morning" covers 08-11, not 06-11.
    facts = derive_facts(fixture("synthetic_calm"), TimeWindow("today", "morning"))
    assert facts["window_start"] == "2026-09-03T08:00"


def test_tomorrow_window(fixture):
    facts = derive_facts(fixture("synthetic_calm"), TimeWindow("tomorrow", "evening"))
    assert facts["window_start"] == "2026-09-04T17:00"
    assert facts["window_end"] == "2026-09-04T20:00"


def test_past_window_is_unavailable_not_guessed(fixture):
    data = fixture("synthetic_calm")
    data["current"]["time"] = "2026-09-03T22:30"  # evening already over
    with pytest.raises(WindowUnavailable):
        derive_facts(data, TimeWindow("today", "evening"))


def test_window_unavailable_routes_like_other_data_errors():
    assert issubclass(WindowUnavailable, WeatherDataError)


def test_now_for_tomorrow_rejected(fixture):
    with pytest.raises(WindowUnavailable):
        derive_facts(fixture("synthetic_calm"), TimeWindow("tomorrow", "now"))


# --- rain-system flag ----------------------------------------------------------

@pytest.mark.parametrize("name", ["synthetic_rain_system_extreme", "synthetic_rain_system_subtle"])
def test_rain_system_detected(fixture, name):
    facts = derive_facts(fixture(name), TimeWindow("today", "now"))
    assert facts["heavy_rain_system"] is True


def test_subtle_system_detected_without_any_extreme_number(fixture):
    """The brief's hard case: no single number is extreme, the situation is."""
    facts = derive_facts(fixture("synthetic_rain_system_subtle"), TimeWindow("today", "now"))
    assert facts["window_precip_max_mm_h"] < 2.5          # not even "moderate rain" per hour
    assert facts["precip_max_day_mm"] < 64.5              # no IMD "heavy" day
    assert not facts["heavy_rain_code_3day"]              # no heavy-rain / storm code
    assert "hard_trigger_daily" not in facts["heavy_rain_signals"]
    assert facts["heavy_rain_system"] is True


def test_extreme_system_hits_hard_trigger(fixture):
    facts = derive_facts(fixture("synthetic_rain_system_extreme"), TimeWindow("today", "now"))
    assert "hard_trigger_daily" in facts["heavy_rain_signals"]


def test_isolated_thunderstorm_is_not_a_system(fixture):
    """Calibration regression: one storm afternoon fires several correlated signals."""
    facts = derive_facts(fixture("synthetic_thunderstorm_afternoon"), TimeWindow("today", "afternoon"))
    assert facts["window_has_thunderstorm"] is True
    assert facts["heavy_rain_system"] is False


@pytest.mark.parametrize("name", ["synthetic_calm", "synthetic_strong_wind", "synthetic_light_rain", "live_bhopal"])
def test_no_false_rain_system(fixture, name):
    assert derive_facts(fixture(name), TimeWindow("today", "whole_day"))["heavy_rain_system"] is False


def test_thresholds_come_from_config(fixture):
    """Raising min_signals in config (no code change) switches the flag off."""
    from app.facts import load_config

    cfg = load_config()
    cfg["heavy_rain_system"]["min_signals"] = 5
    facts = derive_facts(fixture("synthetic_rain_system_subtle"), TimeWindow("today", "now"), cfg=cfg)
    assert facts["heavy_rain_system"] is False
