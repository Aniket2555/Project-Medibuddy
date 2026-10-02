"""Pydantic schema for SOP files (sops/*.yaml).

An SOP is either:
  * a **threshold rule**: `conditions` (a small boolean DSL over facts) + one
    `severity` + one `guidance` text, or
  * a **rubric rule** (for fuzzy questions like "good day for a picnic?"):
    a list of soft `factors`, each a condition; code counts how many pass and
    picks a `grade`, and each grade carries its own severity and guidance.

Everything is validated against the fact catalog and the operators, so a
typo in a policy file fails loudly at load time instead of silently never
matching. Vocabulary checks (activities, audiences, categories, severities)
happen in the loader, because they depend on config/vocabulary.yaml.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Union

from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag, field_validator, model_validator

from app.facts import FACT_CATALOG
from app.ops import OPERATORS

PLACEHOLDER_RE = re.compile(r"\{([a-z0-9_]+)\}")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# --- condition DSL -------------------------------------------------------------

class Leaf(_Strict):
    fact: str
    op: str
    value: Any

    @field_validator("fact")
    @classmethod
    def _known_fact(cls, v: str) -> str:
        if v not in FACT_CATALOG:
            raise ValueError(f"unknown fact {v!r} (see FACT_CATALOG in app/facts.py)")
        return v

    @field_validator("op")
    @classmethod
    def _known_op(cls, v: str) -> str:
        if v not in OPERATORS:
            raise ValueError(f"unknown operator {v!r}; allowed: {sorted(OPERATORS)}")
        return v

    @model_validator(mode="after")
    def _value_shape(self) -> "Leaf":
        ftype = FACT_CATALOG[self.fact][0]
        if self.op == "between" and not (isinstance(self.value, list) and len(self.value) == 2):
            raise ValueError(f"'between' needs [low, high], got {self.value!r}")
        if ftype == "bool" and not isinstance(self.value, bool):
            raise ValueError(f"fact {self.fact!r} is boolean; value must be true/false, got {self.value!r}")
        if ftype in ("int", "float") and self.op in (">", ">=", "<", "<=") and (
            isinstance(self.value, bool) or not isinstance(self.value, (int, float))
        ):
            raise ValueError(f"fact {self.fact!r} is numeric; value must be a number, got {self.value!r}")
        return self


class AllOf(_Strict):
    all: list["Condition"] = Field(min_length=1)


class AnyOf(_Strict):
    any: list["Condition"] = Field(min_length=1)


class NotOf(_Strict):
    not_: "Condition" = Field(alias="not")


def _condition_kind(v: Any) -> str:
    """Pick the branch by its key, so a typo produces ONE precise error instead
    of Pydantic reporting why every alternative failed."""
    if isinstance(v, dict):
        for key in ("all", "any", "not"):
            if key in v:
                return key
        return "leaf"
    return {Leaf: "leaf", AllOf: "all", AnyOf: "any", NotOf: "not"}.get(type(v), "leaf")


Condition = Annotated[
    Union[
        Annotated[Leaf, Tag("leaf")],
        Annotated[AllOf, Tag("all")],
        Annotated[AnyOf, Tag("any")],
        Annotated[NotOf, Tag("not")],
    ],
    Discriminator(_condition_kind),
]
for _m in (AllOf, AnyOf, NotOf):
    _m.model_rebuild()


def condition_facts(cond: Condition) -> set[str]:
    """All fact names referenced by a condition tree."""
    if isinstance(cond, Leaf):
        return {cond.fact}
    if isinstance(cond, AllOf):
        return set().union(*(condition_facts(c) for c in cond.all))
    if isinstance(cond, AnyOf):
        return set().union(*(condition_facts(c) for c in cond.any))
    return condition_facts(cond.not_)


# --- rubric (fuzzy SOPs) ---------------------------------------------------------

class RubricFactor(_Strict):
    name: str
    label: str               # human wording, e.g. "No rain expected"
    condition: Condition


class RubricGrade(_Strict):
    name: str                 # e.g. good / mixed / poor
    min_passed: int = Field(ge=0)
    severity: str
    guidance: str


class Rubric(_Strict):
    factors: list[RubricFactor] = Field(min_length=2)
    grades: list[RubricGrade] = Field(min_length=2)

    @model_validator(mode="after")
    def _grades_cover_all_scores(self) -> "Rubric":
        mins = sorted(g.min_passed for g in self.grades)
        if mins[0] != 0:
            raise ValueError("rubric needs a grade with min_passed: 0 so every score gets a grade")
        if len(set(mins)) != len(mins):
            raise ValueError("rubric grades must have distinct min_passed values")
        if mins[-1] > len(self.factors):
            raise ValueError(f"a grade needs {mins[-1]} passes but there are only {len(self.factors)} factors")
        names = [f.name for f in self.factors]
        if len(set(names)) != len(names):
            raise ValueError("rubric factor names must be unique")
        return self


# --- the SOP ---------------------------------------------------------------------

class AppliesTo(_Strict):
    # Activity ids from config/vocabulary.yaml, or ["any"] = any known activity
    # (never `other`, i.e. never an activity we have no vocabulary entry for).
    activities: list[str] = Field(min_length=1)
    # Empty = applies whoever is going. Otherwise at least one must be present.
    audiences: list[str] = Field(default_factory=list)


class Sop(_Strict):
    id: str = Field(pattern=r"^SOP-\d{3}$")
    title: str
    description: str                  # one line: what this policy is for
    category: str
    version: int = 1
    owner: str = "safety-policy-team"
    applies_to: AppliesTo

    # threshold rule
    conditions: Condition | None = None
    severity: str | None = None
    guidance: str | None = None
    # rubric rule
    rubric: Rubric | None = None

    # Conflict-resolution hints (used by the matcher, Phase 3)
    override: bool = False                # leads the answer, shown before everything else
    only_if_no_other_match: bool = False  # e.g. the all-clear: suppressed if anything else applies

    # Facts the answer must quote (checked by the grounding validator, Phase 4)
    must_mention: list[str] = Field(default_factory=list)
    # Example questions this SOP is meant for. Documentation and eval material only:
    # NOT used for matching (matching is facts + vocabulary, never string lookup).
    examples: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _shape(self) -> "Sop":
        if (self.conditions is None) == (self.rubric is None):
            raise ValueError("an SOP needs exactly one of `conditions` (threshold rule) or `rubric` (fuzzy rule)")
        if self.conditions is not None and (not self.severity or not self.guidance):
            raise ValueError("a threshold SOP needs `severity` and `guidance`")
        if self.rubric is not None and (self.severity or self.guidance):
            raise ValueError("a rubric SOP sets severity/guidance per grade, not at the top level")
        if self.override and self.only_if_no_other_match:
            raise ValueError("`override` and `only_if_no_other_match` contradict each other")
        for name in self.must_mention:
            if name not in FACT_CATALOG:
                raise ValueError(f"must_mention: unknown fact {name!r}")
        for text in self.guidance_texts():
            for ph in PLACEHOLDER_RE.findall(text):
                if ph not in FACT_CATALOG:
                    raise ValueError(f"guidance placeholder {{{ph}}} is not a known fact")
        return self

    def guidance_texts(self) -> list[str]:
        if self.rubric:
            return [g.guidance for g in self.rubric.grades]
        return [self.guidance or ""]

    def severities(self) -> list[str]:
        return [g.severity for g in self.rubric.grades] if self.rubric else [self.severity or ""]

    def referenced_facts(self) -> set[str]:
        facts = set(self.must_mention)
        if self.conditions is not None:
            facts |= condition_facts(self.conditions)
        if self.rubric:
            for f in self.rubric.factors:
                facts |= condition_facts(f.condition)
        for text in self.guidance_texts():
            facts |= set(PLACEHOLDER_RE.findall(text))
        return facts
