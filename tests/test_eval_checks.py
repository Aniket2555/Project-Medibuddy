"""The eval checker must be able to FAIL. A suite that can't fail proves nothing,
so each check is fed a deliberately broken state and must flag it."""

from evals.run_evals import check, stray_numbers

FACTS = {"window_gusts_max_kmh": 58.0, "window_uv_max": 6.0, "heavy_rain_system": False, "window_precip_prob_max": 5}
SOP4 = {"id": "SOP-004", "guidance": "Gusts reach {window_gusts_max_kmh} km/h.", "title": "Strong gusts"}
GOOD = {
    "kind": "answered", "facts": FACTS, "primary": [SOP4], "also": [],
    "intent": {"activity": "cycling", "location": "Pune", "audiences": ["general"]},
    "answer": "Gusts reach 58.0 km/h, avoid riding [SOP-004].\n---\nPolicy basis: SOP-004 Strong gusts (high, v1)",
}


def bad(**changes):
    return {**GOOD, **changes}


def test_good_state_passes():
    assert check({"kind": "answered", "primary": ["SOP-004"], "answer_contains": ["58.0"], "live_number": True}, GOOD) == []


def test_invented_number_caught():
    s = bad(answer="Gusts reach 60 km/h [SOP-004].")
    assert stray_numbers(s) == ["60"]
    assert any("not traceable" in f for f in check({}, s))


def test_wrong_kind_caught():
    assert check({"kind": "no_policy"}, GOOD)


def test_wrong_primary_caught():
    assert check({"primary": ["SOP-002"]}, GOOD)


def test_missing_inline_citation_caught():
    assert any("not cited inline" in f for f in check({}, bad(answer="Gusts reach 58.0 km/h.")))


def test_wrong_leader_caught():
    s = bad(answer="Wind [SOP-004] then rain [SOP-001].")
    assert check({"first_cited": "SOP-001"}, s)


def test_rain_system_must_lead_when_live_flag_set():
    s = bad(facts={**FACTS, "heavy_rain_system": True}, answer="Gusts 58.0 km/h [SOP-004].")
    assert any("rain system" in f for f in check({"rain_system_leads": True}, s))


def test_digits_on_failure_path_caught():
    s = {"kind": "data_unavailable", "answer": "Service down, but it's probably 30 °C.", "facts": {}}
    assert any("digits" in f for f in check({"no_digits": True}, s))


def test_forbidden_text_caught():
    assert check({"answer_not_contains": ["avoid riding"]}, GOOD)


def test_intent_mismatch_caught():
    assert check({"intent": {"activity": "two_wheeler"}}, GOOD)


def test_no_live_number_caught():
    s = bad(answer="It is windy, avoid riding [SOP-004].")
    assert any("quotes no weather value" in f for f in check({"live_number": True}, s))


def test_leftover_braces_caught():
    s = bad(answer="Gusts reach 58.0 km/h during {this morning} [SOP-004].")
    assert any("braces" in f for f in check({}, s))


def test_footer_numbers_are_not_checked():
    """The footer (data time, versions) is code-built, so only the body is checked."""
    s = bad(answer=GOOD["answer"] + "\nWeather data: forecast issued 2026-09-03T08:00 local time.")
    assert check({}, s) == []


def test_provider_quota_is_infra_not_failure():
    from evals.run_evals import is_infra_error
    assert is_infra_error("intent extraction failed: RateLimitError('Error code: 429 - rate_limit_exceeded')")
    assert not is_infra_error("intent extraction failed: ValidationError('activity')")
