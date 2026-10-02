"""Grounding: where "the bot only composes language" is enforced in code.

The composer LLM writes text with {fact} placeholders instead of numbers, and
cites policies inline as [SOP-xxx]. `validate_draft` rejects a draft that:

  1. types a number itself, unless that number is a fixed constant in the
     policy text it was given (e.g. "SPF 30", "7 seconds", "10:00");
  2. uses a placeholder that isn't a real, non-missing fact for this request,
     or leaves stray/doubled braces (which would leak into the rendered text);
  3. cites a policy that wasn't matched (blocks invented or injected SOP ids);
  4. fails to cite a matched primary policy (every answer must be traceable);
  5. leaves out a fact the policy requires (`must_mention`);
  6. doesn't lead with an override policy (the rain system must come first).

Only a passing draft is rendered: `render_answer` substitutes the real values,
so every weather number the user sees comes from this request's API response.
"""

from __future__ import annotations

import re
from typing import Any

from app.facts import FACT_CATALOG
from app.sop_schema import PLACEHOLDER_RE

SOP_ID_RE = re.compile(r"\bSOP-\d{3}\b")
CITATION_RE = re.compile(r"\[(SOP-\d{3})\]")
NUMBER_RE = re.compile(r"\d+(?:[.,:]\d+)*")


def policy_constants(primary: list[dict]) -> set[str]:
    """Numbers that appear in the policy texts themselves (not live data)."""
    allowed: set[str] = set()
    for m in primary:
        for text in (m["guidance"], m["title"], *m.get("passed_factors", []), *m.get("failed_factors", [])):
            allowed |= set(NUMBER_RE.findall(PLACEHOLDER_RE.sub(" ", text)))
    return allowed


def validate_draft(draft: str, primary: list[dict], cited_ids: list[str], facts: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not draft or not draft.strip():
        return ["the draft is empty"]

    # 1. raw numbers
    stripped = SOP_ID_RE.sub(" ", PLACEHOLDER_RE.sub(" ", draft))
    allowed = policy_constants(primary)
    stray = sorted({n for n in NUMBER_RE.findall(stripped) if n not in allowed})
    if stray:
        errors.append(f"typed numbers {stray} directly; weather values must be written as placeholders like "
                      "{window_gusts_max_kmh}, and no other numbers may be added")

    # 2. placeholders must be real, present facts, written with single braces
    #    (found in evals: the model sometimes writes {{window_label}}, which rendered as "{this morning}")
    if re.search(r"[{}]", PLACEHOLDER_RE.sub("", draft)):
        errors.append("contains stray or doubled braces; write placeholders exactly like {window_label}")
    for ph in sorted(set(PLACEHOLDER_RE.findall(draft))):
        if ph not in FACT_CATALOG:
            errors.append(f"placeholder {{{ph}}} is not a known fact")
        elif facts.get(ph) is None:
            errors.append(f"placeholder {{{ph}}} has no value for this request")

    # 3 + 4. citations
    mentioned = SOP_ID_RE.findall(draft)
    unknown = sorted(set(mentioned) - set(cited_ids))
    if unknown:
        errors.append(f"mentions {unknown}, which are not among the matched policies {cited_ids}")
    cited_inline = set(CITATION_RE.findall(draft))
    missing = [m["id"] for m in primary if m["id"] not in cited_inline]
    if missing:
        errors.append(f"does not cite {missing} inline as [SOP-xxx]")

    # 5. required facts
    used = set(PLACEHOLDER_RE.findall(draft))
    for m in primary:
        lacking = [f for f in m.get("must_mention", []) if f not in used]
        if lacking:
            errors.append(f"{m['id']} requires mentioning {['{' + f + '}' for f in lacking]}")

    # 6. override policy leads
    leaders = [m["id"] for m in primary if m.get("override")]
    if leaders and mentioned:
        first = mentioned[0]
        if first not in leaders:
            errors.append(f"must lead with {leaders[0]} (it overrides the others), but {first} comes first")
    return errors


def format_value(name: str, value: Any) -> str:
    if isinstance(value, float) and value.is_integer() and FACT_CATALOG.get(name, ("",))[0] == "float":
        return f"{value:.1f}"
    return str(value)


def render_answer(draft: str, facts: dict[str, Any]) -> str:
    """Substitute placeholders with this request's values. Only called on validated drafts."""
    return PLACEHOLDER_RE.sub(lambda m: format_value(m.group(1), facts[m.group(1)]), draft).strip()
