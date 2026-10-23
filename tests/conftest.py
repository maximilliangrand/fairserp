import json
from pathlib import Path

import pytest

from fairserp.engines.base import SearchRequest

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def coffee() -> SearchRequest:
    return SearchRequest(q="coffee", engine="google")
