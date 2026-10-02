"""Eval suite runner: real LLM, programmatic checks, pass rates, an LLM faithfulness judge.

    python -m evals.run_evals                      # all cases, 3 runs each, with judge
    python -m evals.run_evals --only L01,A02 --repeats 1
    python -m evals.run_evals --no-judge

Writes evals/RESULTS.md (summary + per-case detail + evals/NOTES.md appended)
and evals/results/latest.json (raw runs).

Pass/fail is decided ONLY by programmatic checks (reply type, cited SOPs and
their order, extracted intent, and every number in the answer traced to this
request's weather facts or the cited policy text). The judge (a different model)
is reported in its own column: it catches advice added in words, which the
grounding validator can't see, but it is a model and can be wrong.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from app.facts import FACT_CATALOG, TimeWindow, derive_facts
from app.graph import Deps, build_graph, groq_deps, new_session_id, run_turn
from app.grounding import NUMBER_RE, SOP_ID_RE, format_value, policy_constants
from app.matcher import Intent, match, render
from app.sop_loader import SOPS_DIR, load_sops
from app.weather import FailingWeatherClient, Location, OpenMeteoClient, StaticWeatherClient, WeatherDataError

EVALS = Path(__file__).resolve().parent
FIXTURES = EVALS / "fixtures"
JUDGE_MODEL = "openai/gpt-oss-20b"

# Cities scanned for the "most severe live weather right now" case (L02):
# monsoon/cyclone-prone, tropical-storm-prone and windy places.
SCAN_CITIES = [
    "Bhopal", "Mumbai", "Chennai", "Kolkata", "Guwahati", "Bhubaneswar", "Thiruvananthapuram", "Visakhapatnam",
    "Dhaka", "Yangon", "Manila", "Taipei", "Hong Kong", "Ho Chi Minh City", "Jakarta", "Kuala Lumpur",
    "Bangkok", "Okinawa", "Miami", "Houston", "Reykjavik", "Wellington",
]


def fixture_client(name: str) -> StaticWeatherClient:
    payload = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    loc = payload.get("_location")
    return StaticWeatherClient(payload, Location(**loc) if loc else None)


def scan_most_severe() -> dict | None:
    """Score each city by the policies that apply to a two-wheeler ride today (live data)."""
    client, lib, best = OpenMeteoClient(), load_sops(), None
    for city in SCAN_CITIES:
        try:
            loc = client.geocode(city)
            facts = derive_facts(client.forecast(loc), TimeWindow("today", "whole_day"), loc.label)
        except WeatherDataError:
            continue
        result = match(Intent("two_wheeler"), facts, lib)
        hazards = [m for m in result.matched if not m.sop.only_if_no_other_match]
        score = sum((lib.severity_rank(m.severity) + 1) ** 2 for m in hazards)
        if hazards and (best is None or score > best["score"]):
            best = {"city": city, "score": score, "sops": [m.id for m in hazards],
                    "facts": {k: facts[k] for k in ("precip_3day_mm", "precip_max_day_mm", "window_gusts_max_kmh",
                                                    "window_precip_prob_max", "window_has_thunderstorm",
                                                    "heavy_rain_system", "window_uv_max")}}
        time.sleep(0.2)
    return best


def body_of(answer: str) -> str:
    return answer.split("\n---\n")[0]


def fact_number_strings(facts: dict) -> set[str]:
    out = set()
    for k, v in facts.items():
        if FACT_CATALOG.get(k, ("",))[0] in ("int", "float") and v is not None:
            out |= {format_value(k, v), str(v)}
            if isinstance(v, float) and v.is_integer():
                out.add(str(int(v)))
    return out


def stray_numbers(state: dict) -> list[str]:
    body = SOP_ID_RE.sub(" ", body_of(state.get("answer", "")))
    allowed = fact_number_strings(state.get("facts") or {}) | policy_constants(state.get("primary", []) + state.get("also", []))
    return sorted({n for n in NUMBER_RE.findall(body) if n not in allowed})


def check(expect: dict, state: dict) -> list[str]:
    f: list[str] = []
    answer, body = state.get("answer", ""), body_of(state.get("answer", "")).lower()
    kind = state.get("kind")
    primary = [m["id"] for m in state.get("primary", [])]
    cited = primary + [m["id"] for m in state.get("also", [])]
    intent = state.get("intent") or {}

    kinds = expect.get("kind")
    if kinds and kind not in (kinds if isinstance(kinds, list) else [kinds]):
        f.append(f"kind {kind!r}, expected {kinds}")
    if "primary" in expect and primary != expect["primary"]:
        f.append(f"primary {primary}, expected {expect['primary']}")
    mentioned = SOP_ID_RE.findall(answer)
    if expect.get("first_cited") and (not mentioned or mentioned[0] != expect["first_cited"]):
        f.append(f"answer leads with {mentioned[:1]}, expected {expect['first_cited']}")
    for sid in expect.get("cited_includes", []):
        if sid not in cited:
            f.append(f"{sid} not cited (cited {cited})")
    for sid in expect.get("cited_excludes", []):
        if sid in cited:
            f.append(f"{sid} cited but should not be")
    for k, v in (expect.get("intent") or {}).items():
        got = intent.get(k)
        ok = (str(got).lower() == str(v).lower()) if k != "location" else (str(v).lower() in str(got).lower())
        if not ok:
            f.append(f"intent.{k} = {got!r}, expected {v!r}")
    for aud in expect.get("audiences_include", []):
        if aud not in (intent.get("audiences") or []):
            f.append(f"audiences {intent.get('audiences')} missing {aud!r}")
    if "grade" in expect:
        grades = [m.get("grade") for m in state.get("primary", []) if m.get("grade")]
        if expect["grade"] not in grades:
            f.append(f"rubric grade {grades}, expected {expect['grade']}")
    for s in expect.get("answer_contains", []):
        if s.lower() not in body:
            f.append(f"answer lacks {s!r}")
    for s in expect.get("answer_not_contains", []):
        if s.lower() in body:
            f.append(f"answer contains forbidden {s!r}")
    if expect.get("no_digits") and re.search(r"\d", answer):
        f.append("reply contains digits although no weather data was available")
    if expect.get("live_number"):
        nums = set(NUMBER_RE.findall(SOP_ID_RE.sub(" ", body_of(answer))))
        if not nums & fact_number_strings(state.get("facts") or {}):
            f.append("answer quotes no weather value from this request")
    if expect.get("rain_system_leads") and (state.get("facts") or {}).get("heavy_rain_system"):
        if not mentioned or mentioned[0] != "SOP-001":
            f.append("live facts show a rain system but SOP-001 does not lead")

    # always: traceability + grounding
    if kind in ("answered", "templated"):
        missing = [p for p in primary if f"[{p}]" not in answer]
        if missing:
            f.append(f"matched policies not cited inline: {missing}")
    stray = stray_numbers(state)
    if stray:
        f.append(f"numbers not traceable to this request's data or policy text: {stray}")
    if re.search(r"[{}]", body_of(answer)):
        f.append("answer contains leftover braces (unrendered or doubled placeholder)")
    return f


class Verdict(BaseModel):
    faithful: bool = Field(description="true if the answer contains no advice beyond the policy text")
    unsupported_claims: list[str] = Field(default_factory=list,
                                          description="advice/claims in the answer that the policy text does not support")


JUDGE_SYSTEM = """You audit a weather-safety assistant. It may ONLY give advice that is in the POLICY TEXT.
List every piece of advice, recommendation, reassurance or safety claim in the ANSWER that is NOT stated or
directly implied by the POLICY TEXT. Ignore: rephrasing, shortening, leaving things out, numbers, citations
like [SOP-001], and restating what the user plans to do. faithful = true when the list is empty."""


def judge(judge_llm, state: dict) -> dict | None:
    if state.get("kind") != "answered":
        return None  # fixed-text replies and templated policy text need no judge
    policy = "\n\n".join(f"[{m['id']}] {render(m['guidance'], state['facts'])}" for m in state["primary"])
    user = f"POLICY TEXT:\n{policy}\n\nANSWER:\n{body_of(state['answer'])}"
    try:
        v = judge_llm.with_structured_output(Verdict).invoke([("system", JUDGE_SYSTEM), ("user", user)])
        return {"faithful": v.faithful, "unsupported": v.unsupported_claims}
    except Exception as exc:
        return {"faithful": None, "unsupported": [f"judge error: {exc!r}"[:200]]}


def judge_control(judge_llm) -> dict:
    """Negative control: a doctored answer that adds advice no policy contains.
    If the judge doesn't flag this, its 'faithful' verdicts mean nothing."""
    facts = derive_facts(json.loads((FIXTURES / "synthetic_strong_wind.json").read_text(encoding="utf-8")),
                         TimeWindow("today", "morning"), "Pune")
    sop = load_sops().by_id("SOP-004")
    state = {
        "kind": "answered", "facts": facts,
        "primary": [{"id": "SOP-004", "guidance": sop.guidance}],
        "answer": (f"Gusts reach {facts['window_gusts_max_kmh']} km/h this morning, so avoid riding [SOP-004]. "
                   "Wear a helmet and a reflective jacket, and drink an electrolyte drink before you leave."),
    }
    return judge(judge_llm, state)


def is_infra_error(text: str) -> bool:
    """Provider-side failures (rate limits, quota, outages) are not bot failures:
    they're reported as INFRA and excluded from pass rates."""
    t = text or ""
    return any(k in t for k in ("RateLimitError", "rate_limit_exceeded", "Error code: 429", "Error code: 503",
                                "APIConnectionError", "InternalServerError"))


def run_case(case: dict, base: Deps, judge_llm, scan: dict | None) -> dict:
    w = case["weather"]
    if "fixture" in w:
        weather = fixture_client(w["fixture"])
    elif "failing" in w:
        weather = FailingWeatherClient(w["failing"])
    else:
        weather = OpenMeteoClient()

    sops_dir, tmp = SOPS_DIR, None
    if case.get("extra_sop"):
        tmp = Path(tempfile.mkdtemp())
        sops_dir = tmp / "sops"
        shutil.copytree(SOPS_DIR, sops_dir)
        (sops_dir / f"{case['id']}-extra.yaml").write_text(case["extra_sop"], encoding="utf-8")

    graph = build_graph(Deps(weather, base.structured, base.complete, sops_dir))
    sid, failures, state = new_session_id(), [], {}
    try:
        for i, turn in enumerate(case["turns"], 1):
            text = turn["say"].replace("{city}", scan["city"]) if scan else turn["say"]
            state = run_turn(graph, sid, text)
            turn_failures = check(turn.get("expect", {}), state)
            if turn_failures and state.get("error"):
                turn_failures.append(f"error: {state['error'][:200]}")  # keep the reason for every turn
            failures += [f"turn {i}: {x}" for x in turn_failures]
    except Exception as exc:
        failures.append(f"exception: {exc!r}"[:300])
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)

    infra = is_infra_error(state.get("error", "")) or any(is_infra_error(f) for f in failures)
    return {
        "passed": not failures and not infra, "infra": infra, "error": state.get("error", ""),
        "failures": failures, "kind": state.get("kind"),
        "cited": [m["id"] for m in state.get("primary", []) + state.get("also", [])],
        "intent": state.get("intent"), "attempts": state.get("attempts"), "answer": state.get("answer", ""),
        "judge": judge(judge_llm, state) if (judge_llm and state) else None,
    }


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser()
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--only", help="comma-separated case ids")
    p.add_argument("--no-judge", action="store_true")
    p.add_argument("--pause", type=float, default=1.0, help="seconds between runs (Groq rate limits)")
    p.add_argument("--resume", action="store_true", help="keep completed runs from results/latest.json, run the rest")
    args = p.parse_args()

    cases = yaml.safe_load((EVALS / "cases.yaml").read_text(encoding="utf-8"))["cases"]
    if args.only:
        wanted = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in wanted]

    from app.config import get_llm
    base = groq_deps()
    judge_llm = None if args.no_judge else get_llm(JUDGE_MODEL)

    controls = []
    if judge_llm:
        controls = [judge_control(judge_llm) for _ in range(args.repeats)]
        caught = sum(1 for c in controls if c and c["faithful"] is False)
        print(f"judge control (doctored answer must be flagged): {caught}/{len(controls)} flagged")

    scan = None
    if any(c["weather"].get("scan") for c in cases):
        print("scanning cities for the most severe live weather...")
        scan = scan_most_severe()
        print(f"  -> {scan['city'] if scan else 'nothing severe anywhere in the scan list'}: {scan}")

    previous = {}
    if args.resume and (EVALS / "results" / "latest.json").exists():
        old = json.loads((EVALS / "results" / "latest.json").read_text(encoding="utf-8"))
        previous = {r["id"]: r for r in old["results"]}
        controls = controls or old["meta"].get("judge_control") or []

    results, started, quota_hit = [], time.time(), False
    for case in cases:
        if case["weather"].get("scan") and not scan:
            results.append({**case, "runs": [], "skipped": "no city in the scan list has hazardous live weather today"})
            continue
        runs = [r for r in previous.get(case["id"], {}).get("runs", []) if not r.get("infra")]
        case_scan = scan if case["weather"].get("scan") else None
        if runs and previous[case["id"]].get("scan"):
            case_scan = previous[case["id"]]["scan"]  # keep the city those runs used
        while len(runs) < args.repeats and not quota_hit:
            run = run_case(case, base, judge_llm, case_scan)
            if run["infra"]:
                quota_hit = True
                print(f"{case['id']}: provider error, stopping ({run['error'][:160]})")
                print("   re-run later with --resume to continue where this stopped")
                break
            runs.append(run)
            status = "PASS" if run["passed"] else "FAIL"
            print(f"{case['id']} run {len(runs)}/{args.repeats}: {status} kind={run['kind']} cited={run['cited']}"
                  + ("" if run["passed"] else f"  {run['failures']}"))
            time.sleep(args.pause)
        results.append({**case, "runs": runs, "scan": case_scan,
                        **({"incomplete": f"{len(runs)}/{args.repeats} runs (provider quota)"} if len(runs) < args.repeats else {})})

    meta = {"date": datetime.now().strftime("%Y-%m-%d %H:%M"), "model": base_model(), "judge": None if args.no_judge else JUDGE_MODEL,
            "repeats": args.repeats, "seconds": round(time.time() - started), "judge_control": controls}
    out_dir = EVALS / "results"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "latest.json").write_text(json.dumps({"meta": meta, "results": results}, indent=1, default=str), encoding="utf-8")
    if not args.only:
        (EVALS / "RESULTS.md").write_text(render_report(meta, results), encoding="utf-8")
        print("\nwrote evals/RESULTS.md and evals/results/latest.json")
    else:
        print("\n(--only run: wrote evals/results/latest.json; RESULTS.md is only rewritten by a full run)")
    return 0


def base_model() -> str:
    import os
    return os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")


def judge_control_line(meta: dict) -> str:
    ctrl = meta.get("judge_control") or []
    if not ctrl:
        return "**Judge control:** not run."
    caught = sum(1 for c in ctrl if c and c["faithful"] is False)
    flagged = sorted({u for c in ctrl if c for u in c["unsupported"]})
    return (f"**Judge control:** a doctored answer that adds a helmet/reflective-jacket/electrolyte tip (in no policy) "
            f"was flagged in {caught}/{len(ctrl)} runs. Flagged: {flagged}")


def render_report(meta: dict, results: list[dict]) -> str:
    def rate(r):
        if r.get("skipped"):
            return "SKIP"
        n, ok = len(r["runs"]), sum(x["passed"] for x in r["runs"])
        if n == 0:
            return "NOT RUN (provider quota)"
        mark = "✅" if ok == n else ("⚠️" if ok else "❌")
        return f"{mark} {ok}/{n}" + (" (incomplete: provider quota)" if r.get("incomplete") else "")

    def judge_rate(r):
        js = [x["judge"] for x in r.get("runs", []) if x.get("judge")]
        if not js:
            return "n/a"
        ok = sum(1 for j in js if j["faithful"])
        return f"{ok}/{len(js)} faithful"

    total_runs = sum(len(r.get("runs", [])) for r in results)
    passed_runs = sum(x["passed"] for r in results for x in r.get("runs", []))
    full = sum(1 for r in results if r.get("runs") and all(x["passed"] for x in r["runs"]) and not r.get("incomplete"))
    lines = [
        "# Eval results", "",
        f"Run {meta['date']} · model `{meta['model']}` · judge `{meta['judge']}` · {meta['repeats']} runs per case · {meta['seconds']}s",
        "",
        f"**{full}/{len(results)} cases passed every run · {passed_runs}/{total_runs} runs passed overall.**",
        "",
        "Pass/fail = programmatic checks only. The judge column is a separate, model-based check for advice "
        "added in words (it can be wrong); see evals/run_evals.py.",
        "",
        "Regenerate: `python -m evals.run_evals` (needs GROQ_API_KEY; live cases call Open-Meteo).",
        "",
        judge_control_line(meta),
        "",
        "## Summary", "",
        "| ID | Brief requirement | What we check | Result | Judge |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(f"| {r['id']} | {r['requirement']} | {r['checking']} | {rate(r)} | {judge_rate(r)} |")
    lines += ["", "## Case details", ""]
    for r in results:
        lines += [f"### {r['id']} — {r['requirement']}", "",
                  f"- **Checking:** {r['checking']}",
                  f"- **Pass looks like:** {r['pass_criteria']}",
                  f"- **Weather:** `{json.dumps(r['weather'])}`",
                  f"- **Result:** {rate(r)}"]
        if r.get("skipped"):
            lines += [f"- **Skipped:** {r['skipped']}", ""]
            continue
        if r.get("scan"):
            lines.append(f"- **Live city picked by scan:** {r['scan']['city']} (policies {r['scan']['sops']}, facts {r['scan']['facts']})")
        fails = sorted({f for x in r["runs"] for f in x["failures"]})
        if fails:
            lines.append("- **Failures seen:**")
            lines += [f"  - {f}" for f in fails]
        unsupported = sorted({c for x in r["runs"] if x.get("judge") for c in x["judge"]["unsupported"]})
        if unsupported:
            lines.append("- **Judge flagged:**")
            lines += [f"  - {c}" for c in unsupported]
        if not r["runs"]:
            lines.append("")
            continue
        sample = r["runs"][0]
        lines += [f"- **Turns:** " + " → ".join(f"“{t['say']}”" for t in r["turns"]),
                  f"- **Sample answer (run 1, kind `{sample['kind']}`, cited {sample['cited']}):**", "",
                  "  > " + body_of(sample["answer"]).strip().replace("\n", "\n  > "), ""]
    notes = EVALS / "NOTES.md"
    if notes.exists():
        lines += ["---", "", notes.read_text(encoding="utf-8")]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
