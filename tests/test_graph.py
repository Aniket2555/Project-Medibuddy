"""End-to-end graph tests with injected fakes: fixture weather + a scripted LLM.

These check routing, grounding, fallbacks and memory deterministically, with no
API key. The eval suite runs the same paths against the real LLM.
"""

import re
import shutil
import textwrap

import pytest

from app.graph import Deps, build_graph, new_session_id, run_turn
from app.sop_loader import SOPS_DIR
from app.weather import FailingWeatherClient, LocationNotFound, StaticWeatherClient

BASE_INTENT = {"in_scope": True, "activity": None, "activity_text": None, "audiences": [],
               "location": None, "day": None, "part": None}


def good_draft(user_prompt: str) -> str:
    """A well-behaved composer: one sentence per policy, in the given order, quoting
    the required placeholders and citing the policy."""
    out = []
    for block in re.split(r"\n(?=\[SOP-\d{3}\])", user_prompt.split("POLICIES", 1)[1].split("FACTS", 1)[0]):
        m = re.search(r"\[(SOP-\d{3})\]", block)
        if not m:
            continue
        must = re.search(r"must include: (.+)", block)
        phs = must.group(1) if must else ""
        out.append(f"Per our policy, {phs} matters here [{m.group(1)}].")
    return " ".join(out)


class FakeLLM:
    def __init__(self, intents: dict, drafts=None, fail_intent=False, fail_compose=False):
        self.intents, self.drafts = intents, list(drafts or [])
        self.fail_intent, self.fail_compose = fail_intent, fail_compose
        self.compose_prompts: list[str] = []

    def structured(self, system, user, schema):
        if self.fail_intent:
            raise RuntimeError("LLM unavailable")
        msg = user.split("<<<\n", 1)[1].rsplit("\n>>>", 1)[0]
        return schema(**{**BASE_INTENT, **self.intents[msg]})  # validated against the live vocabulary

    def complete(self, system, user):
        self.compose_prompts.append(user)
        if self.fail_compose:
            raise RuntimeError("LLM unavailable")
        if self.drafts:
            d = self.drafts.pop(0)
            return d(user) if callable(d) else d
        return good_draft(user)


class CountingWeather:
    def __init__(self, inner):
        self.inner, self.geocodes, self.forecasts = inner, 0, 0

    def geocode(self, name):
        self.geocodes += 1
        return self.inner.geocode(name)

    def forecast(self, loc):
        self.forecasts += 1
        return self.inner.forecast(loc)


class ExplodingWeather:
    def geocode(self, name):
        raise AssertionError("weather must not be called on this path")

    forecast = geocode


def make(fixture_payload, intents, sops_dir=SOPS_DIR, **llm_kw):
    llm = FakeLLM(intents, **llm_kw)
    weather = fixture_payload if not isinstance(fixture_payload, dict) else CountingWeather(StaticWeatherClient(fixture_payload))
    graph = build_graph(Deps(weather, llm.structured, llm.complete, sops_dir))
    return graph, llm, weather


def ask(graph, text, sid="s1"):
    return run_turn(graph, sid, text)


def test_answer_is_grounded_and_cited(fixture):
    q = "Going for a run at 1pm in Delhi, ok?"
    graph, llm, _ = make(fixture("synthetic_high_uv"), {q: {"activity": "running", "location": "Delhi", "part": "afternoon"}})
    out = ask(graph, q)
    assert out["kind"] == "answered"
    assert "[SOP-002]" in out["answer"] and "Policy basis: SOP-002" in out["answer"]
    assert str(out["facts"]["window_uv_max"]) in out["answer"]  # the real number, rendered by code
    assert "{" not in out["answer"]                               # no placeholder left behind
    assert out["trace"] == ["understand", "resolve_location", "fetch_weather", "derive_facts",
                            "match_sops", "compose", "validate", "finalize"]


def test_draft_with_invented_number_is_retried(fixture):
    q = "Cycling now?"
    bad = "Gusts are 60 km/h, avoid riding [SOP-004]."
    graph, llm, _ = make(fixture("synthetic_strong_wind"), {q: {"activity": "cycling", "location": "Pune"}}, drafts=[bad])
    out = ask(graph, q)
    assert out["kind"] == "answered" and out["attempts"] == 2
    assert "60" not in out["answer"]
    assert "REJECTED" in llm.compose_prompts[1] and "typed numbers ['60']" in llm.compose_prompts[1]


def test_invented_policy_id_is_rejected(fixture):
    q = "Cycling now?"
    fake = lambda u: good_draft(u) + " Also SOP-999 says storms are fine."  # noqa: E731
    graph, llm, _ = make(fixture("synthetic_strong_wind"), {q: {"activity": "cycling", "location": "Pune"}}, drafts=[fake])
    out = ask(graph, q)
    assert "SOP-999" not in out["answer"]
    assert "not among the matched policies" in llm.compose_prompts[1]


def test_three_bad_drafts_fall_back_to_policy_text(fixture):
    q = "Cycling now?"
    graph, _, _ = make(fixture("synthetic_strong_wind"), {q: {"activity": "cycling", "location": "Pune"}},
                       drafts=["Totally fine, go!"] * 3)
    out = ask(graph, q)
    assert out["kind"] == "templated" and out["attempts"] == 3
    assert out["answer"].startswith("[SOP-004] Gusts are forecast to reach 58.0 km/h")
    assert "Totally fine" not in out["answer"]


def test_composer_down_falls_back_to_policy_text(fixture):
    q = "Cycling now?"
    graph, _, _ = make(fixture("synthetic_strong_wind"), {q: {"activity": "cycling", "location": "Pune"}}, fail_compose=True)
    out = ask(graph, q)
    assert out["kind"] == "templated" and "[SOP-004]" in out["answer"]


def test_rain_system_must_lead(fixture):
    q = "Picnic this afternoon?"
    def wrong_order(u):  # cites both, but the picnic grade first
        return "Picnic looks poor, chance {window_precip_prob_max}% [SOP-009]. Rain system: {precip_3day_mm} mm, {precip_max_day_mm} mm [SOP-001]."
    graph, llm, _ = make(fixture("synthetic_rain_system_subtle"),
                         {q: {"activity": "picnic", "location": "Bhopal", "part": "afternoon"}}, drafts=[wrong_order])
    out = ask(graph, q)
    assert "must lead with SOP-001" in llm.compose_prompts[1]
    assert out["answer"].index("[SOP-001]") < out["answer"].index("[SOP-009]")


def test_weather_api_down(fixture):
    q = "Run now in Pune?"
    graph, llm, _ = make(FailingWeatherClient("forecast"), {q: {"activity": "running", "location": "Pune"}})
    out = ask(graph, q)
    assert out["kind"] == "data_unavailable"
    assert not re.search(r"\d", out["answer"])  # no numbers at all: nothing to ground them in
    assert llm.compose_prompts == []             # the LLM never got to write anything


def test_unknown_city_takes_same_fallback(fixture):
    class NoSuchCity(StaticWeatherClient):
        def geocode(self, name):
            raise LocationNotFound("no results")
    q = "Run now in Xyzzyville?"
    graph, _, _ = make(NoSuchCity(fixture("synthetic_calm")), {q: {"activity": "running", "location": "Xyzzyville"}})
    out = ask(graph, q)
    assert out["kind"] == "data_unavailable" and "couldn't find that location" in out["answer"]


def test_intent_llm_down():
    graph, _, _ = make(ExplodingWeather(), {}, fail_intent=True)
    assert ask(graph, "Run now?")["kind"] == "error"


def test_out_of_scope_never_fetches_weather():
    q = "What's the best stock to buy?"
    graph, _, _ = make(ExplodingWeather(), {q: {"in_scope": False}})
    out = ask(graph, q)
    assert out["kind"] == "out_of_scope" and out["trace"] == ["understand", "respond_out_of_scope"]


def test_unknown_activity_no_advice():
    q = "Is it safe to go scuba diving in Goa?"
    graph, llm, _ = make(ExplodingWeather(), {q: {"activity": "other", "activity_text": "scuba diving", "location": "Goa"}})
    out = ask(graph, q)
    assert out["kind"] == "unknown_activity" and "scuba diving" in out["answer"]
    assert llm.compose_prompts == []


def test_no_policy_applies(fixture):
    q = "Going for a run now?"
    graph, llm, _ = make(fixture("synthetic_strong_wind"), {q: {"activity": "running", "location": "Pune"}})
    out = ask(graph, q)
    assert out["kind"] == "no_policy" and "won't make some up" in out["answer"]
    assert "58.0" in out["answer"]  # real data shown for reference, rendered by code
    assert llm.compose_prompts == []


def test_broken_policy_file_fails_loudly(tmp_path, fixture):
    d = tmp_path / "sops"
    shutil.copytree(SOPS_DIR, d)
    (d / "SOP-099-bad.yaml").write_text("id: SOP-099\n", encoding="utf-8")
    q = "Run now in Pune?"
    graph, _, _ = make(fixture("synthetic_calm"), {q: {"activity": "running", "location": "Pune"}}, sops_dir=d)
    out = ask(graph, q)
    assert out["kind"] == "error" and "policy files invalid" in out["error"]


def test_bike_means_two_wheeler_and_says_so(fixture):
    q = "Is it safe to bike to work now in Pune?"
    graph, _, _ = make(fixture("synthetic_strong_wind"), {q: {"activity": "cycling", "location": "Pune"}})
    out = ask(graph, q)
    assert out["intent"]["activity"] == "two_wheeler"
    assert 'taken "bike" to mean a motorbike' in out["answer"]


def test_bicycle_is_not_overridden(fixture):
    q = "Is it safe to ride my bicycle now in Pune?"
    graph, _, _ = make(fixture("synthetic_strong_wind"), {q: {"activity": "cycling", "location": "Pune"}})
    assert ask(graph, q)["intent"]["activity"] == "cycling"


def test_follow_up_reuses_context_and_refetches(fixture):
    q1, q2 = "Safe to ride my scooter in Bhopal this morning?", "What about this evening instead?"
    graph, llm, weather = make(fixture("synthetic_strong_wind"), {
        q1: {"activity": "two_wheeler", "location": "Bhopal", "part": "morning"},
        q2: {"part": "evening", "day": "today"},
    })
    ask(graph, q1)
    out = ask(graph, q2)
    assert out["kind"] == "answered"
    assert out["intent"]["activity"] == "two_wheeler" and out["intent"]["location"] == "Bhopal"
    assert out["facts"]["window_label"] == "this evening"
    assert "resolve_location(cached)" in out["trace"]
    assert weather.geocodes == 1 and weather.forecasts == 2  # fresh data, same place
    assert "Earlier in this conversation" in llm.compose_prompts[-1]


def test_missing_city_is_asked_then_completed(fixture):
    q1, q2 = "Is it okay to go for a run now?", "Pune"
    graph, _, _ = make(fixture("synthetic_calm"), {
        q1: {"activity": "running"},
        q2: {"in_scope": False, "location": "Pune"},  # a bare city name, judged out of scope on its own
    })
    first = ask(graph, q1)
    assert first["kind"] == "clarify" and "Which city" in first["answer"]
    second = ask(graph, q2)
    assert second["kind"] == "answered" and second["intent"]["activity"] == "running"


def test_sessions_are_isolated(fixture):
    q1, q2 = "Run now in Pune?", "What about this evening?"
    graph, _, _ = make(fixture("synthetic_calm"), {
        q1: {"activity": "running", "location": "Pune"}, q2: {"part": "evening", "day": "today"}})
    ask(graph, q1, sid=new_session_id())
    out = ask(graph, q2, sid=new_session_id())  # new session: nothing remembered
    assert out["kind"] == "clarify"


def test_new_sop_file_answers_without_code_change(tmp_path, fixture):
    d = tmp_path / "sops"
    shutil.copytree(SOPS_DIR, d)
    (d / "SOP-013-kite-flying.yaml").write_text(textwrap.dedent("""
        id: SOP-013
        title: Kite flying in strong gusts
        description: Gusts too strong to fly a kite safely.
        category: leisure
        severity: moderate
        applies_to: { activities: [kite_flying] }
        conditions:
          all:
            - { fact: window_gusts_max_kmh, op: ">=", value: 35 }
        guidance: Gusts reach {window_gusts_max_kmh} km/h, so the kite line can cut hands. Wait for calmer air.
        must_mention: [window_gusts_max_kmh]
    """), encoding="utf-8")
    q = "Can the kids fly kites this afternoon in Ahmedabad?"
    graph, _, _ = make(fixture("synthetic_strong_wind"),
                       {q: {"activity": "kite_flying", "location": "Ahmedabad", "part": "afternoon"}}, sops_dir=d)
    out = ask(graph, q)
    assert out["kind"] == "answered" and "[SOP-013]" in out["answer"] and "58.0" in out["answer"]


def test_off_schema_intent_is_retried_once(fixture):
    q = "Cycling now in Pune?"
    graph, llm, _ = make(fixture("synthetic_calm"), {q: {"activity": "cycling", "location": "Pune"}})
    real, calls = llm.structured, []

    def flaky(system, user, schema):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("tool call validation failed: /audiences expected array, got null")
        return real(system, user, schema)

    llm.structured = flaky
    graph = build_graph(Deps(CountingWeather(StaticWeatherClient(fixture("synthetic_calm"))), flaky, llm.complete))
    assert ask(graph, q)["kind"] == "answered" and len(calls) == 2


def test_null_audiences_accepted(fixture):
    q = "Cycling now in Pune?"
    graph, _, _ = make(fixture("synthetic_calm"), {q: {"activity": "cycling", "location": "Pune", "audiences": None}})
    out = ask(graph, q)
    assert out["kind"] == "answered" and out["intent"]["audiences"] == ["general"]


def test_compound_place_name_falls_back_to_city(fixture):
    class CityOnly(StaticWeatherClient):
        def geocode(self, name):
            if name != "Delhi":
                raise LocationNotFound(name)
            return super().geocode(name)
    q = "Picnic in Lodhi Garden this evening?"
    graph, _, _ = make(CityOnly(fixture("synthetic_calm")),
                       {q: {"activity": "picnic", "location": "Lodhi Garden, Delhi", "part": "evening", "day": "today"}})
    assert ask(graph, q)["kind"] == "answered"


def test_day_without_time_means_rest_of_day(fixture):
    q = "Is it safe to cycle in Pune today?"
    graph, _, _ = make(fixture("synthetic_calm"), {q: {"activity": "cycling", "location": "Pune", "day": "today"}})
    out = ask(graph, q)
    assert out["intent"]["part"] == "whole_day" and out["facts"]["window_label"] == "today"
