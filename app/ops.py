"""Comparison operators shared by the facts config and the SOP condition DSL."""

from __future__ import annotations

from typing import Any, Callable


def _between(a: Any, b: Any) -> bool:
    lo, hi = b
    return lo <= a <= hi


OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    "in": lambda a, b: a in b,
    "not_in": lambda a, b: a not in b,
    "between": _between,  # value: [low, high], inclusive
    "contains_any": lambda a, b: bool(set(a) & set(b)),  # list fact vs list value
}


def compare(actual: Any, op: str, expected: Any) -> bool:
    """Evaluate `actual <op> expected`. A missing fact (None) never matches."""
    if op not in OPERATORS:
        raise ValueError(f"unknown operator {op!r}; allowed: {sorted(OPERATORS)}")
    if actual is None:
        return False
    try:
        return OPERATORS[op](actual, expected)
    except TypeError:
        return False
