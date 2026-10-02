"""Load and validate every SOP in sops/*.yaml.

* Re-reads the folder on every call (a few small files, a few ms), so a new or
  edited SOP takes effect on the next question, with no restart and no code change.
* Files starting with `_` (e.g. `_TEMPLATE.yaml`) are ignored.
* All problems across all files are collected and reported together, each with
  its file name, so a broken policy fails loudly instead of silently not matching.

    python -m app.sop_loader          # validate + print the policy table
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import ValidationError

from app.sop_schema import Sop

ROOT = Path(__file__).resolve().parent.parent
SOPS_DIR = ROOT / "sops"
VOCAB_PATH = ROOT / "config" / "vocabulary.yaml"

log = logging.getLogger(__name__)

ANY = "any"
OTHER = "other"  # intent label for activities outside the vocabulary


class SopValidationError(Exception):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("Invalid SOP set:\n  - " + "\n  - ".join(problems))


@dataclass
class SopLibrary:
    sops: list[Sop]
    severities: list[str]                      # ordered low -> high
    categories: dict[str, str]
    activities: dict[str, str]                 # id -> description (config + SOP-introduced)
    audiences: dict[str, str]
    sources: dict[str, str] = field(default_factory=dict)  # SOP id -> file name
    warnings: list[str] = field(default_factory=list)

    def by_id(self, sop_id: str) -> Sop | None:
        return next((s for s in self.sops if s.id == sop_id), None)

    def severity_rank(self, severity: str) -> int:
        return self.severities.index(severity)


def _loc(parts: tuple) -> str:
    """Readable error location, e.g. `conditions.all.0.fact`.

    Pydantic adds internal labels (validator names, the condition branch tag), and
    the tag for all/any/not duplicates the field name that follows it.
    """
    out: list[str] = []
    for p in map(str, parts):
        if p.startswith("function-") or p == "leaf" or (out and out[-1] == p):
            continue
        out.append(p)
    return ".".join(out) or "(top level)"


def load_vocabulary(path: Path = VOCAB_PATH) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_sops(sops_dir: Path = SOPS_DIR, vocab_path: Path = VOCAB_PATH) -> SopLibrary:
    vocab = load_vocabulary(vocab_path)
    severities: list[str] = vocab["severities"]
    categories: dict[str, str] = vocab["categories"]
    activities: dict[str, str] = dict(vocab["activities"])
    audiences: dict[str, str] = dict(vocab["audiences"])

    problems: list[str] = []
    warnings: list[str] = []
    sops: list[Sop] = []
    sources: dict[str, str] = {}

    files = sorted(p for p in Path(sops_dir).glob("*.y*ml") if not p.name.startswith("_"))
    for path in files:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            problems.append(f"{path.name}: not valid YAML ({exc})")
            continue
        if not isinstance(raw, dict):
            problems.append(f"{path.name}: expected a mapping at the top level")
            continue
        try:
            sop = Sop.model_validate(raw)
        except ValidationError as exc:
            for err in exc.errors():
                problems.append(f"{path.name}: {_loc(err['loc'])}: {err['msg']}")
            # Still check vocabulary fields, so every typo is reported in one run.
            if raw.get("category") not in categories:
                problems.append(f"{path.name}: unknown category {raw.get('category')!r}; allowed: {list(categories)}")
            if "severity" in raw and raw["severity"] not in severities:
                problems.append(f"{path.name}: unknown severity {raw['severity']!r}; allowed: {severities}")
            continue

        # vocabulary checks
        if sop.id in sources:
            problems.append(f"{path.name}: duplicate id {sop.id} (also in {sources[sop.id]})")
            continue
        if sop.category not in categories:
            problems.append(f"{path.name}: unknown category {sop.category!r}; allowed: {list(categories)}")
        for sev in sop.severities():
            if sev not in severities:
                problems.append(f"{path.name}: unknown severity {sev!r}; allowed: {severities}")
        for aud in sop.applies_to.audiences:
            if aud not in audiences:
                problems.append(f"{path.name}: unknown audience {aud!r}; allowed: {list(audiences)}")
        acts = sop.applies_to.activities
        if ANY in acts and len(acts) > 1:
            problems.append(f"{path.name}: use either [any] or a list of activities, not both")
        for act in acts:
            if act in (ANY, OTHER) or act in activities:
                continue
            # New activity introduced by an SOP: accept it, so one new file is enough.
            activities[act] = act.replace("_", " ")
            warnings.append(f"{path.name}: activity {act!r} is not in config/vocabulary.yaml; added it automatically")

        sops.append(sop)
        sources[sop.id] = path.name

    if problems:
        raise SopValidationError(problems)
    for w in warnings:
        log.warning(w)
    return SopLibrary(sops, severities, categories, activities, audiences, sources, warnings)


def format_table(lib: SopLibrary) -> str:
    rows = [f"{'ID':<8} {'CATEGORY':<18} {'SEVERITY':<22} {'ACTIVITIES':<34} TITLE"]
    for s in lib.sops:
        sev = "/".join(s.severities())
        acts = ",".join(s.applies_to.activities)
        if s.applies_to.audiences:
            acts += " | " + ",".join(s.applies_to.audiences)
        flags = (" [override]" if s.override else "") + (" [fallback]" if s.only_if_no_other_match else "")
        rows.append(f"{s.id:<8} {s.category:<18} {sev:<22} {acts[:34]:<34} {s.title}{flags}")
    return "\n".join(rows)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        lib = load_sops()
    except SopValidationError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(format_table(lib))
    cats = {s.category for s in lib.sops}
    sevs = {sev for s in lib.sops for sev in s.severities()}
    print(f"\n{len(lib.sops)} SOPs | {len(cats)} categories | severities used: "
          f"{[x for x in lib.severities if x in sevs]}")
    for w in lib.warnings:
        print(f"warning: {w}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
