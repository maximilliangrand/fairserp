"""Every built-in engine passes the same conformance suite offered to plugin authors."""

import pytest

from fairserp.engines import build_registry
from fairserp.engines.exa import ExaEngine
from fairserp.engines.jina import JinaEngine
from fairserp.engines.parallel import ParallelEngine
from fairserp.model import SearchQuery
from fairserp.testing.conformance import ConformanceError, check_engine, check_metadata
from tests.conftest import load

WIKIPEDIA_RAW = {"base": "https://en.wikipedia.org",
                 "search": load("wikipedia_search_einstein.json")}
SAMPLES = {
    "brave": ("brave_coffee.json", None), "mojeek": ("mojeek_coffee.json", None),
    "marginalia": ("marginalia_coffee.json", None), "tavily": ("tavily_coffee.json", None),
    "exa": ("exa_coffee.json", None), "parallel": ("parallel_coffee.json", None),
    "jina": ("jina_coffee.json", None), "firecrawl": ("firecrawl_coffee.json", None),
    "searxng": ("searxng_coffee.json", None), "wikipedia": (None, WIKIPEDIA_RAW),
    "wikidata": ("wikidata_einstein.json", None),
    "duckduckgo_ia": ("ddg_ia_einstein.json", None),
    "openstreetmap": ("openstreetmap_pizza.json", None),
}
QUERIES = {
    "wikidata": SearchQuery(q="einstein"),
    "openstreetmap": SearchQuery(q="pizza", location="Brooklyn, New York", vertical="local"),
}


@pytest.mark.parametrize("name", sorted(build_registry(load_plugins=False)))
def test_builtin_engines_conform(name):
    engine = build_registry(load_plugins=False)[name]
    fixture, raw = SAMPLES[name]
    check_engine(engine, raw if raw is not None else load(fixture),
                 query=QUERIES.get(name))


def test_new_adapters_parse_their_documented_shapes():
    query = SearchQuery(q="coffee")
    exa = ExaEngine().parse(load("exa_coffee.json"), query)
    assert exa.web[1].url == "https://www.ncausa.org/About-Coffee"  # utm stripped
    assert exa.web[0].published_at == "2001-09-12"
    parallel = ParallelEngine().parse(load("parallel_coffee.json"), query)
    assert "brewed" in parallel.web[0].snippet
    jina = JinaEngine().parse(load("jina_coffee.json"), query)
    assert jina.web[0].published_at == "2025-10-01" and jina.has_more is False


def test_conformance_catches_bad_metadata():
    engine = ExaEngine()
    engine.supports = frozenset({"telepathy"})
    with pytest.raises(ConformanceError, match="supports"):
        check_metadata(engine)
