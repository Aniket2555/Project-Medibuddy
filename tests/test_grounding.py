"""The grounding validator in isolation: each rule, accept and reject."""

from app.grounding import render_answer, validate_draft

UV = {"id": "SOP-002", "title": "Very high UV during outdoor exercise", "override": False,
      "must_mention": ["window_uv_max"],
      "guidance": "UV reaches {window_uv_max}. Use SPF 30+ sunscreen; go before 10:00 or after 17:00."}
RAIN = {"id": "SOP-001", "title": "Active heavy-rain system", "override": True,
        "must_mention": ["precip_3day_mm"], "guidance": "About {precip_3day_mm} mm over 3 days."}
FACTS = {"window_uv_max": 10.5, "precip_3day_mm": 86.4, "window_label": "this afternoon", "window_gusts_max_kmh": None}


def test_valid_draft_passes():
    draft = "UV hits {window_uv_max} this afternoon, so use SPF 30 and go before 10:00 [SOP-002]."
    assert validate_draft(draft, [UV], ["SOP-002"], FACTS) == []


def test_policy_constants_allowed_but_new_numbers_rejected():
    errs = validate_draft("UV is {window_uv_max}; wait 20 minutes [SOP-002].", [UV], ["SOP-002"], FACTS)
    assert any("['20']" in e for e in errs)


def test_typed_weather_value_rejected_even_if_correct():
    """Typing the right number is still rejected: only code may put numbers in."""
    errs = validate_draft("UV is 10.5 today [SOP-002].", [UV], ["SOP-002"], FACTS)
    assert any("10.5" in e for e in errs)


def test_unknown_or_missing_placeholder_rejected():
    errs = validate_draft("UV {window_uv_max}, gusts {window_gusts_max_kmh}, {made_up} [SOP-002].", [UV], ["SOP-002"], FACTS)
    assert any("{made_up} is not a known fact" in e for e in errs)
    assert any("{window_gusts_max_kmh} has no value" in e for e in errs)


def test_uncited_primary_rejected():
    errs = validate_draft("UV is {window_uv_max}.", [UV], ["SOP-002"], FACTS)
    assert any("does not cite ['SOP-002']" in e for e in errs)


def test_must_mention_enforced():
    errs = validate_draft("Use sunscreen [SOP-002].", [UV], ["SOP-002"], FACTS)
    assert any("requires mentioning" in e for e in errs)


def test_override_must_lead():
    draft = "UV {window_uv_max} [SOP-002]. Rain {precip_3day_mm} mm [SOP-001]."
    errs = validate_draft(draft, [RAIN, UV], ["SOP-001", "SOP-002"], FACTS)
    assert any("must lead with SOP-001" in e for e in errs)


def test_also_applies_ids_may_be_mentioned():
    draft = "UV {window_uv_max} [SOP-002]; SOP-005 also applies."
    assert validate_draft(draft, [UV], ["SOP-002", "SOP-005"], FACTS) == []


def test_empty_draft():
    assert validate_draft("  ", [UV], ["SOP-002"], FACTS) == ["the draft is empty"]


def test_render_formats_values():
    assert render_answer("UV {window_uv_max}, rain {precip_3day_mm} mm", FACTS) == "UV 10.5, rain 86.4 mm"
    assert render_answer("{x}", {"x": 3}) == "3"


def test_doubled_braces_rejected():
    errs = validate_draft("UV {window_uv_max} during {{window_label}} [SOP-002].", [UV], ["SOP-002"], FACTS)
    assert any("braces" in e for e in errs)


def test_stray_brace_rejected():
    errs = validate_draft("UV {window_uv_max} } [SOP-002].", [UV], ["SOP-002"], FACTS)
    assert any("braces" in e for e in errs)


def test_composer_gets_text_filled_in_and_only_number_placeholders():
    """Text facts are filled in by code; only numeric facts stay as placeholders."""
    from app.compose import build_compose_prompt
    m = {"id": "SOP-002", "title": "UV", "severity": "high", "override": False, "grade": None,
         "guidance": "UV reaches {window_uv_max} during {window_label}.", "must_mention": ["window_uv_max"],
         "facts": ["window_uv_max", "window_label"]}
    _, user = build_compose_prompt("run?", [m], {**FACTS, "location_name": "Delhi"})
    assert "during this afternoon" in user and "{window_label}" not in user
    assert "{window_uv_max}" in user          # numbers stay placeholders
    assert "Location: Delhi | Time window: this afternoon" in user


def test_brace_feedback_quotes_the_fragment():
    errs = validate_draft("UV {window_uv_max} during {this afternoon} [SOP-002].", [UV], ["SOP-002"], FACTS)
    assert any("'{this afternoon}'" in e for e in errs)
