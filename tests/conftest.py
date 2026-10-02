import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent.parent / "evals" / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture
def fixture():
    return load_fixture
