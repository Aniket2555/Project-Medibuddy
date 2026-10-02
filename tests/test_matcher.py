"""Deterministic matcher: applicability, conditions, rubric grading, fallbacks,
and the conflict policy (override -> severity -> specificity, top 3 expanded)."""

import shutil
import textwrap

import pytest

from app.facts import TimeWindow, derive_facts
from app.matcher import MAX_PRIMARY, Intent, evaluate, match, render
from app.sop_loader import SOPS_DIR, load_sops
from app.sop_schema import Sop


@pytest.fixture(scope="module")
def lib():
    return load_sops()


@pytest.fixture
def facts_for(fixture):
    def _facts(name, part="now", day="today"):
        return derive_facts(fixture(name), TimeWindow(day, part), "Testville")
    return _facts


def ids(result):
    return [m.id for m in result.primary]


# --- single clear matches ------------------------------------------------------------

def test_high_uv_afternoon_run(lib, facts_for):
    r = match(Intent("running"), facts_for("synthetic_high_uv", "afternoon"), lib)
    assert ids(r) == ["SOP-002"]
    assert r.primary[0].reasons[0].actual >= 8


def test_same_uv_day_but_evening_run_is_all_clear(lib, facts_for):
    """The UV policy is about the user's window, not the day's peak."""
    r = match(Intent("running"), facts_for("synthetic_high_uv", "evening"), lib)
    assert ids(r) == ["SOP-012"]


def test_strong_wind_cycling(lib, facts_for):
    r = match(Intent("cycling"), facts_for("synthetic_strong_wind", "morning"), lib)
    assert ids(r) == ["SOP-004"]
    assert r.primary[0].severity == "high"


def test_fog_morning_drive_vs_afternoon(lib, facts_for):
    assert ids(match(Intent("driving"), facts_for("synthetic_fog_morning", "morning"), lib)) == ["SOP-006"]
    assert ids(match(Intent("driving"), facts_for("synthetic_fog_morning", "afternoon"), lib)) == ["SOP-012"]


def test_light_rain_run_is_low_severity(lib, facts_for):
    r = match(Intent("running"), facts_for("synthetic_light_rain", "morning"), lib)
    assert ids(r) == ["SOP-011"]
    assert r.primary[0].severity == "low"


def test_thunderstorm_only_in_its_window(lib, facts_for):
    assert "SOP-010" in ids(match(Intent("outdoor_sports"), facts_for("synthetic_thunderstorm_afternoon", "afternoon"), lib))
    assert "SOP-010" not in ids(match(Intent("outdoor_sports"), facts_for("synthetic_thunderstorm_afternoon", "morning"), lib))


def test_pet_heat(lib, facts_for):
    r = match(Intent("pet_walk", ("pet",)), facts_for("synthetic_heatwave", "afternoon"), lib)
    assert "SOP-008" in ids(r)
    assert "SOP-007" not in ids(r)  # no child/elderly/pregnant person mentioned


def test_heatwave_run_gets_heat_and_uv(lib, facts_for):
    r = match(Intent("running"), facts_for("synthetic_heatwave", "afternoon"), lib)
    assert ids(r) == ["SOP-002", "SOP-003"]  # high before moderate


# --- audiences -------------------------------------------------------------------------

def test_vulnerable_audience_adds_sop(lib, facts_for):
    facts = facts_for("synthetic_high_uv", "afternoon")
    adult = match(Intent("walking"), facts, lib)
    elderly = match(Intent("walking", ("elderly",)), facts, lib)
    assert "SOP-007" not in ids(adult)
    assert "SOP-007" in ids(elderly)


# --- fuzzy rubric ----------------------------------------------------------------------

def test_picnic_good_day(lib, facts_for):
    r = match(Intent("picnic"), facts_for("synthetic_calm", "afternoon"), lib)
    assert ids(r) == ["SOP-009"]  # all-clear is suppressed: the rubric applies
    m = r.primary[0]
    assert (m.grade, m.severity, m.failed_factors) == ("good", "info", [])


def test_picnic_mixed_day(lib, facts_for):
    m = match(Intent("picnic"), facts_for("synthetic_high_uv", "afternoon"), lib).primary[0]
    assert m.grade == "mixed" and m.severity == "low"
    assert "UV index below 8" in m.failed_factors


def test_picnic_poor_day(lib, facts_for):
    r = match(Intent("picnic"), facts_for("synthetic_thunderstorm_afternoon", "afternoon"), lib)
    rubric = next(m for m in r.matched if m.id == "SOP-009")
    assert rubric.grade == "poor" and rubric.severity == "moderate"
    assert ids(r)[0] == "SOP-010"  # the storm (high) ranks above the poor picnic grade


# --- rain system: override leads, whatever the activity ------------------------------

@pytest.mark.parametrize("activity", ["picnic", "walking", "commute", "outdoor_event", "pet_walk"])
def test_rain_system_leads_every_activity(lib, facts_for, activity):
    r = match(Intent(activity), facts_for("synthetic_rain_system_subtle"), lib)
    assert ids(r)[0] == "SOP-001"


def test_rain_system_leads_even_when_others_are_also_severe(lib, facts_for):
    r = match(Intent("two_wheeler"), facts_for("synthetic_rain_system_extreme", "morning"), lib)
    assert ids(r) == ["SOP-001", "SOP-004", "SOP-005"]


def test_subtle_rain_system_picnic_also_gets_poor_grade(lib, facts_for):
    r = match(Intent("picnic"), facts_for("synthetic_rain_system_subtle", "afternoon"), lib)
    assert ids(r) == ["SOP-001", "SOP-009"]
    assert r.primary[1].grade == "poor"


# --- conflict policy: top-N + also-applies ---------------------------------------------

def test_top_n_and_also_applies(lib, facts_for):
    facts = facts_for("synthetic_rain_system_extreme", "morning")
    facts.update(window_visibility_min_km=0.4, window_has_fog=True, window_has_thunderstorm=True)
    r = match(Intent("two_wheeler"), facts, lib)
    # 001 override; then high: 004 & 006 (activity-specific) before 010 (any); then 005 moderate
    assert ids(r) == ["SOP-001", "SOP-004", "SOP-006"]
    assert [m.id for m in r.also_applies] == ["SOP-010", "SOP-005"]
    assert len(r.primary) == MAX_PRIMARY
    assert r.cited_ids == ["SOP-001", "SOP-004", "SOP-006", "SOP-010", "SOP-005"]


def test_ranking_is_deterministic(lib, facts_for):
    facts = facts_for("synthetic_rain_system_extreme", "morning")
    assert match(Intent("two_wheeler"), facts, lib).cited_ids == match(Intent("two_wheeler"), facts, lib).cited_ids


# --- honest "no guidance" -------------------------------------------------------------------

def test_unknown_activity_matches_nothing(lib, facts_for):
    """Even on a calm day, the all-clear must not bless an activity we have no policy for."""
    r = match(Intent("other"), facts_for("synthetic_calm", "afternoon"), lib)
    assert r.no_match and r.cited_ids == []


def test_all_clear_safety_belt(lib, facts_for):
    """Strong wind while running: no hazard SOP covers it, and the all-clear refuses
    to reassure because gusts are outside its band -> no guidance, not 'go ahead'."""
    r = match(Intent("running"), facts_for("synthetic_strong_wind", "morning"), lib)
    assert r.no_match
    assert any(s.sop_id == "SOP-012" and "window_gusts_max_kmh" in s.reason for s in r.skipped)


def test_all_clear_on_calm_day(lib, facts_for):
    r = match(Intent("walking"), facts_for("synthetic_calm", "afternoon"), lib)
    assert ids(r) == ["SOP-012"] and r.primary[0].severity == "info"


def test_fallback_suppressed_when_something_else_applies(lib, facts_for):
    """Calm picnic: the all-clear's own conditions hold, but the rubric SOP applies,
    so the all-clear is suppressed (not merely failing its bands)."""
    r = match(Intent("picnic"), facts_for("synthetic_calm", "afternoon"), lib)
    assert ids(r) == ["SOP-009"]
    assert any(s.sop_id == "SOP-012" and "suppressed" in s.reason for s in r.skipped)


def test_every_sop_is_accounted_for(lib, facts_for):
    r = match(Intent("cycling"), facts_for("synthetic_calm"), lib)
    assert {m.id for m in r.matched} | {s.sop_id for s in r.skipped} == {s.id for s in lib.sops}


# --- evaluation details ------------------------------------------------------------------------

def test_any_condition_reports_only_the_true_branch(lib):
    sop007 = lib.by_id("SOP-007")
    ok, checks = evaluate(sop007.conditions, {"window_apparent_temp_max_c": 30, "window_uv_max": 9})
    assert ok and [c.fact for c in checks] == ["window_uv_max"]


def _not_uv_sop():
    return Sop.model_validate({
        "id": "SOP-099", "title": "t", "description": "d", "category": "exercise",
        "severity": "low", "guidance": "g {window_uv_max}", "applies_to": {"activities": ["running"]},
        "conditions": {"not": {"fact": "window_uv_max", "op": ">=", "value": 8}},
    })


def test_missing_fact_leaf_is_false():
    ok, checks = evaluate(_not_uv_sop().conditions.not_, {"window_uv_max": None})
    assert ok is False and checks[0].actual is None


def test_sop_needing_missing_data_is_skipped_not_inverted(lib, facts_for):
    """`not (uv >= 8)` would be True for a missing UV value. The matcher must skip
    the SOP instead: we never act on data we don't have."""
    from app.matcher import _evaluate_sop

    result = _evaluate_sop(_not_uv_sop(), {"window_uv_max": None})
    assert isinstance(result, str) and "missing weather data" in result


def test_missing_visibility_blocks_all_clear(lib, facts_for):
    facts = facts_for("synthetic_calm", "afternoon")
    facts["window_visibility_min_km"] = None
    r = match(Intent("walking"), facts, lib)
    assert r.no_match  # can't confirm "all clear" without the data


def test_rubric_factor_with_missing_data_counts_as_failed(lib, facts_for):
    facts = facts_for("synthetic_calm", "afternoon")
    facts["window_uv_max"] = None
    m = match(Intent("picnic"), facts, lib).primary[0]
    assert "UV index below 8" in m.failed_factors and m.grade == "mixed"


def test_render_fills_placeholders_and_marks_missing():
    assert render("Gusts {window_gusts_max_kmh} km/h", {"window_gusts_max_kmh": 58.0}) == "Gusts 58.0 km/h"
    assert render("UV {window_uv_max}", {"window_uv_max": None}) == "UV [unavailable]"


# --- a new policy file is matched with no code change -----------------------------------------

def test_new_sop_file_is_matched_without_code_change(tmp_path, facts_for):
    d = tmp_path / "sops"
    shutil.copytree(SOPS_DIR, d)
    (d / "SOP-013-kite-flying.yaml").write_text(textwrap.dedent("""
        id: SOP-013
        title: Kite flying in strong gusts
        description: Gusts too strong to fly a kite safely.
        category: leisure
        severity: moderate
        applies_to:
          activities: [kite_flying]
        conditions:
          all:
            - { fact: window_gusts_max_kmh, op: ">=", value: 35 }
        guidance: Gusts reach {window_gusts_max_kmh} km/h; the kite line can cut hands. Wait for calmer air.
    """), encoding="utf-8")
    new_lib = load_sops(d)
    r = match(Intent("kite_flying"), facts_for("synthetic_strong_wind", "afternoon"), new_lib)
    assert ids(r) == ["SOP-013"]
    assert "58.0 km/h" in render(r.primary[0].guidance, facts_for("synthetic_strong_wind", "afternoon"))
