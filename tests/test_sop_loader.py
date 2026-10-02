"""SOP schema + loader: the real rule set meets the brief, bad policies fail loudly,
and new policies are picked up from a file alone."""

import shutil
import textwrap
from pathlib import Path

import pytest

from app.facts import FACT_CATALOG
from app.sop_loader import SOPS_DIR, SopValidationError, load_sops

VALID_SOP = """
id: SOP-099
title: Test policy
description: test
category: exercise
severity: moderate
applies_to:
  activities: [running]
conditions:
  all:
    - { fact: window_gusts_max_kmh, op: ">=", value: 30 }
guidance: Gusts reach {window_gusts_max_kmh} km/h.
"""


@pytest.fixture
def sop_dir(tmp_path: Path) -> Path:
    """A copy of the real sops/ folder that tests can add broken files to."""
    d = tmp_path / "sops"
    shutil.copytree(SOPS_DIR, d)
    return d


def write(d: Path, name: str, body: str) -> None:
    (d / name).write_text(textwrap.dedent(body), encoding="utf-8")


def problems_for(d: Path) -> str:
    with pytest.raises(SopValidationError) as exc:
        load_sops(d)
    return "\n".join(exc.value.problems)


def test_real_sops_load():
    lib = load_sops()
    assert len(lib.sops) >= 10


def test_at_least_three_categories():
    assert len({s.category for s in load_sops().sops}) >= 3


def test_range_of_severities():
    lib = load_sops()
    used = {sev for s in lib.sops for sev in s.severities()}
    assert used == set(lib.severities)  # every level from info to critical is used


def test_has_fuzzy_rubric_sop():
    assert any(s.rubric is not None for s in load_sops().sops)


def test_has_override_rain_system_sop():
    lib = load_sops()
    overrides = [s for s in lib.sops if s.override]
    assert overrides and all(s.applies_to.activities == ["any"] for s in overrides)
    assert any("heavy_rain_system" in s.referenced_facts() for s in overrides)


def test_all_clear_is_fallback_only():
    lib = load_sops()
    fallbacks = [s for s in lib.sops if s.only_if_no_other_match]
    assert len(fallbacks) == 1 and fallbacks[0].severity == "info"


def test_template_file_is_ignored():
    assert "SOP-0NN" not in {s.id for s in load_sops().sops}


def test_every_referenced_fact_exists():
    for s in load_sops().sops:
        assert s.referenced_facts() <= set(FACT_CATALOG), s.id


def test_every_guidance_quotes_live_facts():
    """Advice may contain fixed policy numbers (SPF 30, 7 seconds, 10:00), but each
    guidance must ground itself in at least one live value via a {placeholder}."""
    from app.sop_schema import PLACEHOLDER_RE

    for s in load_sops().sops:
        for text in s.guidance_texts():
            assert PLACEHOLDER_RE.search(text), f"{s.id} guidance quotes no live facts"


def test_new_sop_file_is_picked_up(sop_dir):
    before = len(load_sops(sop_dir).sops)
    write(sop_dir, "SOP-099-test.yaml", VALID_SOP)
    lib = load_sops(sop_dir)
    assert len(lib.sops) == before + 1
    assert lib.by_id("SOP-099").title == "Test policy"
    assert lib.sources["SOP-099"] == "SOP-099-test.yaml"


def test_new_activity_in_sop_extends_vocabulary(sop_dir):
    write(sop_dir, "SOP-099-kites.yaml", VALID_SOP.replace("[running]", "[kite_flying]"))
    lib = load_sops(sop_dir)
    assert "kite_flying" in lib.activities
    assert any("kite_flying" in w for w in lib.warnings)


@pytest.mark.parametrize(
    "mutation, expected",
    [
        (("window_gusts_max_kmh, op", "wind_gust, op"), "unknown fact"),
        (('op: ">="', 'op: "=>"'), "unknown operator"),
        (("severity: moderate", "severity: severe"), "unknown severity"),
        (("category: exercise", "category: sport"), "unknown category"),
        (("id: SOP-099", "id: POLICY-99"), "id"),
        (("{window_gusts_max_kmh} km/h", "{gusts} km/h"), "placeholder {gusts}"),
        (("value: 30", "value: high"), "numeric"),
        (("guidance: Gusts reach {window_gusts_max_kmh} km/h.\n", ""), "severity` and `guidance"),
        (("title: Test policy", "title: Test policy\nunexpected_key: 1"), "unexpected_key"),
    ],
)
def test_invalid_sop_is_rejected_with_filename(sop_dir, mutation, expected):
    old, new = mutation
    assert old in VALID_SOP, f"test bug: {old!r} not in VALID_SOP"
    write(sop_dir, "SOP-099-bad.yaml", VALID_SOP.replace(old, new))
    msg = problems_for(sop_dir)
    assert "SOP-099-bad.yaml" in msg
    assert expected in msg


def test_duplicate_id_rejected(sop_dir):
    write(sop_dir, "SOP-099-copy.yaml", VALID_SOP.replace("SOP-099", "SOP-004"))
    assert "duplicate id SOP-004" in problems_for(sop_dir)


def test_conditions_and_rubric_are_mutually_exclusive(sop_dir):
    body = VALID_SOP + """
rubric:
  factors:
    - { name: a, label: A, condition: { fact: window_has_rain, op: "==", value: false } }
    - { name: b, label: B, condition: { fact: window_uv_max, op: "<", value: 8 } }
  grades:
    - { name: ok, min_passed: 0, severity: info, guidance: fine }
    - { name: great, min_passed: 2, severity: info, guidance: great }
"""
    write(sop_dir, "SOP-099-both.yaml", body)
    assert "exactly one of" in problems_for(sop_dir)


def test_rubric_without_zero_grade_rejected(sop_dir):
    body = """
id: SOP-099
title: Bad rubric
description: test
category: leisure
applies_to: { activities: [picnic] }
rubric:
  factors:
    - { name: a, label: A, condition: { fact: window_has_rain, op: "==", value: false } }
    - { name: b, label: B, condition: { fact: window_uv_max, op: "<", value: 8 } }
  grades:
    - { name: good, min_passed: 2, severity: info, guidance: good }
    - { name: mixed, min_passed: 1, severity: low, guidance: mixed }
"""
    write(sop_dir, "SOP-099-rubric.yaml", body)
    assert "min_passed: 0" in problems_for(sop_dir)


def test_boolean_fact_needs_boolean_value(sop_dir):
    write(sop_dir, "SOP-099-bool.yaml",
          VALID_SOP.replace('{ fact: window_gusts_max_kmh, op: ">=", value: 30 }',
                            '{ fact: window_has_rain, op: "==", value: "yes" }'))
    assert "boolean" in problems_for(sop_dir)


def test_broken_yaml_reported(sop_dir):
    write(sop_dir, "SOP-099-broken.yaml", "id: [unclosed\n")
    assert "SOP-099-broken.yaml: not valid YAML" in problems_for(sop_dir)


def test_one_typo_gives_one_readable_error(sop_dir):
    """A policy author must get one precise line per mistake, not a wall of union noise."""
    write(sop_dir, "SOP-099-typo.yaml", VALID_SOP.replace("window_gusts_max_kmh, op", "wind_gust, op"))
    with pytest.raises(SopValidationError) as exc:
        load_sops(sop_dir)
    assert exc.value.problems == [
        "SOP-099-typo.yaml: conditions.all.0.fact: Value error, unknown fact 'wind_gust' "
        "(see FACT_CATALOG in app/facts.py)"
    ]


def test_all_problems_reported_together(sop_dir):
    write(sop_dir, "SOP-098-a.yaml", VALID_SOP.replace("SOP-099", "SOP-098").replace("category: exercise", "category: x"))
    write(sop_dir, "SOP-099-b.yaml", VALID_SOP.replace("severity: moderate", "severity: y"))
    msg = problems_for(sop_dir)
    assert "SOP-098-a.yaml" in msg and "SOP-099-b.yaml" in msg
