"""Deterministic SOP matcher: (intent, facts) -> which policies apply, ranked.

No LLM here. Given the user's activity/audiences and the
weather facts, every SOP is checked in plain code:

1. Applicability: does the SOP cover this activity and audience?
   `[any]` means any activity in the vocabulary, never `other`.
2. Conditions: the boolean DSL is evaluated against the facts; rubric SOPs are
   graded by counting passed factors.
3. Fallbacks: `only_if_no_other_match` SOPs (the all-clear) are used only if
   nothing else applies.
4. Conflict policy (documented in the README):
     sort by  override first -> severity (high to low) -> specificity -> id
     the top MAX_PRIMARY get full guidance, the rest are "also applies"
     (still cited, just not expanded).

Every match carries the exact fact comparisons that made it true, so the bot can
always answer "why did it say that?" with a policy and the numbers behind it.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.facts import FACT_CATALOG, TimeWindow, derive_facts
from app.ops import compare
from app.sop_loader import ANY, OTHER, SopLibrary, load_sops
from app.sop_schema import PLACEHOLDER_RE, AllOf, AnyOf, Condition, Leaf, NotOf, Sop, condition_facts

MAX_PRIMARY = 3  # SOPs that get full guidance in one answer


@dataclass(frozen=True)
class Intent:
    activity: str                          # vocabulary id, or "other"
    audiences: tuple[str, ...] = ("general",)


@dataclass(frozen=True)
class Check:
    """One evaluated comparison, e.g. window_gusts_max_kmh (58.0) >= 40 -> True."""
    fact: str
    op: str
    expected: Any
    actual: Any
    passed: bool

    def __str__(self) -> str:
        unit = FACT_CATALOG.get(self.fact, ("", "", ""))[1]
        actual = f"{self.actual} {unit}".strip() if self.actual is not None else "missing"
        return f"{self.fact} = {actual} ({self.op} {self.expected}: {'yes' if self.passed else 'no'})"


@dataclass
class SopMatch:
    sop: Sop
    severity: str
    guidance: str                            # template with {fact} placeholders
    reasons: list[Check]                     # comparisons that made the SOP apply
    grade: str | None = None                 # rubric SOPs only
    passed_factors: list[str] = field(default_factory=list)
    failed_factors: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.sop.id

    @property
    def specificity(self) -> int:
        a = self.sop.applies_to
        return (0 if a.activities == [ANY] else 1) + (1 if a.audiences else 0)


@dataclass
class Skipped:
    sop_id: str
    reason: str


@dataclass
class MatchResult:
    intent: Intent
    primary: list[SopMatch]                  # full guidance, in ranked order
    also_applies: list[SopMatch]             # cited, not expanded
    skipped: list[Skipped]                   # every SOP not used, and why (for the "why?" panel)

    @property
    def matched(self) -> list[SopMatch]:
        return self.primary + self.also_applies

    @property
    def cited_ids(self) -> list[str]:
        return [m.id for m in self.matched]

    @property
    def no_match(self) -> bool:
        return not self.primary


def evaluate(cond: Condition, facts: dict[str, Any]) -> tuple[bool, list[Check]]:
    """Returns (result, checks that explain the result)."""
    if isinstance(cond, Leaf):
        actual = facts.get(cond.fact)
        ok = compare(actual, cond.op, cond.value)
        return ok, [Check(cond.fact, cond.op, cond.value, actual, ok)]
    if isinstance(cond, AllOf):
        results = [evaluate(c, facts) for c in cond.all]
        ok = all(r for r, _ in results)
        # true: every child is a reason; false: show the children that failed
        return ok, [chk for r, checks in results for chk in checks if ok or not r]
    if isinstance(cond, AnyOf):
        results = [evaluate(c, facts) for c in cond.any]
        ok = any(r for r, _ in results)
        return ok, [chk for r, checks in results for chk in checks if r or not ok]
    if isinstance(cond, NotOf):
        ok, checks = evaluate(cond.not_, facts)
        return (not ok), checks
    raise TypeError(f"unknown condition type {type(cond).__name__}")


def _applies_to(sop: Sop, intent: Intent, lib: SopLibrary) -> str | None:
    """None if the SOP covers this intent, otherwise the reason it doesn't."""
    acts = sop.applies_to.activities
    if acts == [ANY]:
        if intent.activity == OTHER or intent.activity not in lib.activities:
            return f"activity {intent.activity!r} is outside our vocabulary"
    elif intent.activity not in acts:
        return f"covers {acts}, not {intent.activity!r}"
    auds = sop.applies_to.audiences
    if auds and not set(auds) & set(intent.audiences):
        return f"only for {auds}"
    return None


def _evaluate_sop(sop: Sop, facts: dict[str, Any]) -> SopMatch | str:
    """A SopMatch if its conditions hold, otherwise a short reason string.

    Missing data is never treated as a value: a threshold SOP whose condition
    needs a missing fact is skipped (otherwise `not (x >= 8)` would be true for
    a missing x), and a rubric factor with missing data counts as failed.
    """
    if sop.rubric is not None:
        passed, failed, reasons = [], [], []
        for factor in sop.rubric.factors:
            ok, checks = evaluate(factor.condition, facts)
            ok = ok and not [f for f in condition_facts(factor.condition) if facts.get(f) is None]
            (passed if ok else failed).append(factor.label)
            reasons.extend(checks)
        n = len(passed)
        grade = max((g for g in sop.rubric.grades if g.min_passed <= n), key=lambda g: g.min_passed)
        return SopMatch(sop, grade.severity, grade.guidance, reasons, grade.name, passed, failed)

    missing = sorted(f for f in condition_facts(sop.conditions) if facts.get(f) is None)
    if missing:
        return f"missing weather data: {missing}"
    ok, checks = evaluate(sop.conditions, facts)
    if not ok:
        return "conditions not met: " + "; ".join(str(c) for c in checks)
    return SopMatch(sop, sop.severity, sop.guidance, checks)


def rank_key(m: SopMatch, lib: SopLibrary) -> tuple:
    return (not m.sop.override, -lib.severity_rank(m.severity), -m.specificity, m.id)


def match(intent: Intent, facts: dict[str, Any], lib: SopLibrary | None = None) -> MatchResult:
    lib = lib or load_sops()
    matched: list[SopMatch] = []
    fallbacks: list[SopMatch] = []
    skipped: list[Skipped] = []

    for sop in lib.sops:
        why_not = _applies_to(sop, intent, lib)
        if why_not:
            skipped.append(Skipped(sop.id, why_not))
            continue
        result = _evaluate_sop(sop, facts)
        if isinstance(result, str):
            skipped.append(Skipped(sop.id, result))
        elif sop.only_if_no_other_match:
            fallbacks.append(result)
        else:
            matched.append(result)

    if matched:
        skipped += [Skipped(f.id, "fallback suppressed: another SOP applies") for f in fallbacks]
    else:
        matched = fallbacks

    matched.sort(key=lambda m: rank_key(m, lib))
    return MatchResult(intent, matched[:MAX_PRIMARY], matched[MAX_PRIMARY:], skipped)


def render(template: str, facts: dict[str, Any]) -> str:
    """Fill {fact} placeholders with the live values. Unknown/missing -> visible marker."""
    def sub(m):
        val = facts.get(m.group(1))
        return "[unavailable]" if val is None else str(val)

    return " ".join(PLACEHOLDER_RE.sub(sub, template).split())


def explain(result: MatchResult, facts: dict[str, Any]) -> str:
    """Plain-text explanation: what applies, why, and what was skipped."""
    out = [f"Intent: activity={result.intent.activity}, audiences={list(result.intent.audiences)}"]
    if result.no_match:
        out.append("\nNo SOP applies -> the bot must say it has no guidance.")
    for label, group in (("PRIMARY", result.primary), ("ALSO APPLIES", result.also_applies)):
        for m in group:
            grade = f", grade={m.grade}" if m.grade else ""
            flag = " [override]" if m.sop.override else ""
            out.append(f"\n[{label}] {m.id} {m.sop.title} (severity={m.severity}{grade}){flag}")
            if m.grade:
                out.append(f"    passed: {m.passed_factors}")
                out.append(f"    failed: {m.failed_factors}")
            else:
                out += [f"    because {c}" for c in m.reasons]
            if label == "PRIMARY":
                out.append(f"    guidance: {render(m.guidance, facts)}")
    out.append("\nSkipped:")
    out += [f"    {s.sop_id}: {s.reason}" for s in result.skipped]
    return "\n".join(out)


def main() -> int:
    """python -m app.matcher --fixture synthetic_high_uv --activity running --part afternoon
    python -m app.matcher --city Bhopal --activity cycling"""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=main.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--fixture", help="name of a JSON file in evals/fixtures (without .json)")
    src.add_argument("--city", help="fetch live weather for this city")
    p.add_argument("--activity", required=True)
    p.add_argument("--audience", action="append", help="repeatable; default: general")
    p.add_argument("--day", default="today")
    p.add_argument("--part", default="now")
    args = p.parse_args()

    if args.fixture:
        path = Path(__file__).resolve().parent.parent / "evals" / "fixtures" / f"{args.fixture}.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        label = raw.get("_location", {}).get("name", args.fixture)
    else:
        from app.weather import OpenMeteoClient
        client = OpenMeteoClient()
        loc = client.geocode(args.city)
        raw, label = client.forecast(loc), loc.label

    facts = derive_facts(raw, TimeWindow(args.day, args.part), label)
    intent = Intent(args.activity, tuple(args.audience or ["general"]))
    print(explain(match(intent, facts), facts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
