import json
from pathlib import Path

import pytest

from fairserp.config import Settings
from fairserp.model import SearchQuery

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


def settings(**overrides) -> Settings:
    values = {"api_keys": "good:3,other", "public_url": "https://serp.example.org",
              **overrides}
    return Settings(_env_file=None, **values)


@pytest.fixture
def coffee() -> SearchQuery:
    return SearchQuery(q="coffee")
