"""Every adapter's parser against saved upstream payloads, plus malformed ones."""

import re

import pytest

from fairserp.engines import build_registry
from fairserp.engines.base import EngineError, PayloadError
from fairserp.engines.brave import BraveEngine
from fairserp.engines.duckduckgo_ia import DuckDuckGoInstantAnswerEngine
from fairserp.engines.firecrawl import FirecrawlEngine
from fairserp.engines.marginalia import MarginaliaEngine
from fairserp.engines.mojeek import MojeekEngine
from fairserp.engines.openstreetmap import OpenStreetMapEngine, local_request
from fairserp.engines.searxng import SearxngEngine
from fairserp.engines.tavily import TavilyEngine
from fairserp.engines.wikidata import WikidataEngine, pick_candidate
from fairserp.engines.wikipedia import WikipediaEngine
from fairserp.model import SearchQuery
from tests.conftest import FIXTURES, load


def assert_web(results, count):
    assert len(results) == count
    for result in results:
        assert result.url.startswith("http")
        assert result.title
        assert not re.search(r"</?[a-z]+>", result.snippet)
        assert "](" not in result.snippet


def test_firecrawl(coffee):
    out = FirecrawlEngine().parse(load("firecrawl_coffee.json"), coffee)
    assert_web(out.web, 10)
    assert out.web[0].url == "https://en.wikipedia.org/wiki/Coffee"
    assert out.has_more is True


def test_firecrawl_strips_google_click_tracking(coffee):
    out = FirecrawlEngine().parse(load("firecrawl_coffee.json"), coffee)
    assert not any("srsltid" in r.url for r in out.web)


def test_firecrawl_failure_envelope_is_an_error(coffee):
    with pytest.raises(PayloadError):
        FirecrawlEngine().parse({"success": False, "error": "quota"}, coffee)


def test_firecrawl_second_page_slices():
    query = SearchQuery(q="coffee", num=4, offset=4)
    out = FirecrawlEngine().parse(load("firecrawl_coffee.json"), query)
    everything = load("firecrawl_coffee.json")["data"]["web"]
    assert [r.url.split("?")[0] for r in out.web] == [
        r["url"].split("?")[0] for r in everything[4:8]]


def test_query_biased_snippet_skips_boilerplate(coffee):
    out = FirecrawlEngine().parse(load("firecrawl_coffee.json"), coffee)
    assert len(out.web[0].snippet) <= 304
    assert "coffee" in out.web[0].snippet.lower()


def test_marginalia(coffee):
    out = MarginaliaEngine().parse(load("marginalia_coffee.json"), coffee)
    assert_web(out.web, 10)
    assert out.web[0].title == "Coffee"


def test_brave(coffee):
    out = BraveEngine().parse(load("brave_coffee.json"), coffee)
    assert_web(out.web, 2)
    first = out.web[0]
    assert first.source_name == "Wikipedia"
    assert first.snippet == "Coffee is a beverage brewed from roasted, ground coffee beans."
    assert first.sitelinks[0].title == "Coffee bean"
    assert out.questions[0].question == "Is coffee good for you?"
    assert out.entity.type == "Beverage"
    assert out.entity.attributes["country_of_origin"] == "Yemen"
    assert out.places[0].rating == 4.5 and out.places[0].reviews == 812


def test_mojeek(coffee):
    out = MojeekEngine().parse(load("mojeek_coffee.json"), coffee)
    assert_web(out.web, 2)
    assert out.total_results == 3121948
    assert out.web[0].published_at == "2025-10-09"


def test_tavily_answer_is_labeled_generated(coffee):
    out = TavilyEngine().parse(load("tavily_coffee.json"), coffee)
    assert_web(out.web, 2)
    assert out.answer.generated is True
    assert out.answer.citations == [r.url for r in out.web]


def test_searxng(coffee):
    out = SearxngEngine().parse(load("searxng_coffee.json"), coffee)
    assert_web(out.web, 2)
    assert out.entity.attributes["inception"] == "15th century"
    assert out.related == ["coffee near me", "coffee beans"]


def test_searxng_unresponsive_engines_are_a_failure_not_emptiness(coffee):
    payload = {"results": [], "unresponsive_engines": [["brave", "timeout"]]}
    with pytest.raises(EngineError, match="unresponsive"):
        SearxngEngine().parse(payload, coffee)


def test_wikipedia_dates_are_updates_not_publication():
    query = SearchQuery(q="Albert Einstein")
    raw = {"base": "https://en.wikipedia.org", "search": load("wikipedia_search_einstein.json")}
    out = WikipediaEngine().parse(raw, query)
    assert_web(out.web, 10)
    assert out.web[0].url == "https://en.wikipedia.org/wiki/Albert_Einstein"
    assert out.web[0].published_at is None and out.web[0].updated_at


def test_wikidata_fuzzy_entity():
    out = WikidataEngine().parse(load("wikidata_einstein.json"), SearchQuery(q="einstein"))
    entity = out.entity
    assert entity.title == "Albert Einstein" and entity.wikidata_id == "Q937"
    assert entity.attributes["born"] == "1879-03-14"
    assert entity.attributes["instance_of"] == "human"
    assert len(entity.description) <= 604


def test_wikidata_skips_name_items_and_long_queries():
    search = load("wikidata_einstein.json")["search"]
    assert pick_candidate(search, "einstein") == "Q937"
    assert pick_candidate(search, "best laptops 2026 reviews for students") is None
    long = load("wikidata_best_laptops_2026_reviews_for_students.json")
    out = WikidataEngine().parse(long, SearchQuery(q="best laptops 2026 reviews for students"))
    assert out.entity is None


def test_duckduckgo_instant_answer():
    out = DuckDuckGoInstantAnswerEngine().parse(load("ddg_ia_einstein.json"),
                                                SearchQuery(q="Albert Einstein"))
    assert out.entity.title == "Albert Einstein"
    assert out.entity.attributes["born"].startswith("March 14, 1879")
    assert "Bern Historical Museum" in out.related
    assert not out.web


def test_openstreetmap_pizza_uses_cuisine_and_distance():
    query = SearchQuery(q="pizza", location="Brooklyn, New York", vertical="local")
    out = OpenStreetMapEngine().parse(load("openstreetmap_pizza.json"), query)
    assert out.places
    assert all(p.category == "Pizza restaurant" for p in out.places)
    distances = [p.distance_m for p in out.places]
    assert distances == sorted(distances)
    assert all(p.rating is None for p in out.places)


def test_openstreetmap_cafes():
    query = SearchQuery(q="coffee", location="Austin, Texas", vertical="local")
    out = OpenStreetMapEngine().parse(load("openstreetmap_coffee.json"), query)
    assert out.places and all(p.category == "Cafe" for p in out.places)


def test_local_intent():
    assert local_request(SearchQuery(q="coffee near Austin, TX"))[1] == "Austin, TX"
    assert local_request(SearchQuery(q="pizza", location="Brooklyn"))[0].cuisine == "pizza"
    assert local_request(SearchQuery(q="coffee")) is None
    assert local_request(SearchQuery(q="python asyncio", location="Austin")) is None


NEGATIVE = {
    "brave": [{}, {"type": "search", "web": {"results": [{"title": "no url"}]}}],
    "mojeek": [{}, {"response": {"status": "ERROR"}}],
    "tavily": [{}, {"results": "nope"}],
    "firecrawl": [{"success": True}, {"success": True, "data": {"web": [{"title": "x"}]}}],
    "marginalia": [{}, {"results": [{}]}],
    "searxng": [{}],
    "duckduckgo_ia": [[], {"nope": 1}],
    "wikipedia": [{"base": "https://en.wikipedia.org", "search": {}}],
    "wikidata": [{}],
    "openstreetmap": [[], {"category": "cafe", "center": {"lat": 1, "lon": 1}, "pois": None}],
}


@pytest.mark.parametrize("engine_name", sorted(NEGATIVE))
def test_malformed_payloads_raise_engine_errors(engine_name):
    import asyncio

    engine = build_registry(load_plugins=False)[engine_name]

    class Raw(type(engine)):
        async def fetch(self, query, ctx):
            return self.payload

    adapter = Raw()
    query = SearchQuery(q="pizza", location="Austin", vertical="local")
    for payload in NEGATIVE[engine_name]:
        adapter.payload = payload
        with pytest.raises(EngineError, match="unexpected upstream payload|unresponsive"):
            asyncio.run(adapter.search(query, _ctx()))


def _ctx():
    from fairserp.engines.base import EngineContext
    from fairserp.http import HttpPool
    from tests.conftest import settings

    s = settings(searxng_url="http://searx", brave_api_key="k", mojeek_api_key="k",
                 tavily_api_key="k")
    return EngineContext(s, HttpPool(s))


def test_fixture_manifest_matches_engine_claims():
    manifest = load("manifest.json")
    assert {p.name for p in FIXTURES.glob("*.json")} - {"manifest.json"} == set(manifest)
    registry = build_registry(load_plugins=False)
    for name, engine in registry.items():
        sources = {m["source"] for m in manifest.values() if m["engine"] == name}
        if engine.fixture_verified:
            assert "recorded" in sources, f"{name} claims verified fixtures but has none"
        elif sources:
            assert sources == {"handwritten"}, f"{name} has recorded fixtures; mark verified"


def test_capability_matrix_is_current():
    import subprocess
    import sys

    result = subprocess.run([sys.executable, "scripts/capabilities.py", "--check"],
                            capture_output=True, text=True,
                            cwd=FIXTURES.parent.parent)
    assert result.returncode == 0, result.stdout + result.stderr


def _area_payload(pois):
    square = {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]}
    area = {"name": "Square", "lat": 0.5, "lon": 0.5, "box": [0, 1, 0, 1], "polygon": square}
    return {"category": "cafe", "center": area, "pois": pois}


def _cafe(name, lat, lon, **address):
    return {"name": name, "lat": str(lat), "lon": str(lon), "category": "amenity",
            "type": "cafe", "osm_type": "node", "osm_id": 1, "address": address}


def test_local_results_respect_the_area_boundary_and_report_widening():
    query = SearchQuery(q="coffee", location="Square", vertical="local")
    inside = [_cafe(f"in{i}", 0.5, 0.5 + i / 100) for i in range(3)]
    out = OpenStreetMapEngine().parse(_area_payload(inside + [_cafe("out", 1.2, 0.5)]), query)
    assert [p.name for p in out.places] == ["in0", "in1", "in2"] and not out.notes
    assert out.area.has_boundary and out.area.name == "Square"

    sparse = OpenStreetMapEngine().parse(_area_payload(
        [_cafe("in0", 0.5, 0.5), _cafe("out", 1.2, 0.5)]), query)
    assert [(p.name, p.in_area) for p in sparse.places] == [("in0", True), ("out", False)]
    assert "widened" in sparse.notes[0]


def test_addresses_name_the_borough_without_repeating_the_city():
    raw = _area_payload([_cafe("Cafe", 0.5, 0.5, house_number="30", road="Nevins Street",
                               neighbourhood="Downtown Brooklyn", suburb="Brooklyn",
                               city="New York", state="New York", postcode="11217")])
    out = OpenStreetMapEngine().parse(raw, SearchQuery(q="coffee", location="Square",
                                                       vertical="local"))
    assert out.places[0].address == (
        "30 Nevins Street, Downtown Brooklyn, Brooklyn, New York 11217")


def test_brooklyn_fixture_has_no_manhattan_or_queens_places():
    query = SearchQuery(q="pizza", location="Brooklyn, New York", vertical="local")
    out = OpenStreetMapEngine().parse(load("openstreetmap_pizza.json"), query)
    assert out.places and all("Brooklyn" in p.address for p in out.places)
    assert all(p.in_area for p in out.places)
