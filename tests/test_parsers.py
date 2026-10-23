"""Every adapter's parser against saved upstream payloads."""

import re

from fairserp.engines.base import SearchRequest
from fairserp.engines.brave import BraveEngine
from fairserp.engines.duckduckgo_ia import DuckDuckGoInstantAnswerEngine
from fairserp.engines.firecrawl import FirecrawlEngine
from fairserp.engines.marginalia import MarginaliaEngine
from fairserp.engines.mojeek import MojeekEngine
from fairserp.engines.openstreetmap import OpenStreetMapEngine, local_query
from fairserp.engines.searxng import SearxngEngine
from fairserp.engines.tavily import TavilyEngine
from fairserp.engines.wikipedia import WikipediaEngine
from tests.conftest import load

ORGANIC_KEYS = {"position", "title", "link", "displayed_link", "snippet", "source"}


def assert_organic(results, expected_count):
    assert len(results) == expected_count
    assert [r["position"] for r in results] == list(range(1, expected_count + 1))
    for result in results:
        assert ORGANIC_KEYS <= result.keys()
        assert result["link"].startswith("http")
        assert not re.search(r"</?[a-z]+>", result["snippet"])
        assert "](" not in result["snippet"]


def test_firecrawl(coffee):
    fragments = FirecrawlEngine().parse(load("firecrawl_coffee.json"), coffee)
    results = fragments["organic_results"]
    assert_organic(results, 10)
    first = results[0]
    assert first["link"] == "https://en.wikipedia.org/wiki/Coffee"
    assert first["displayed_link"] == "https://en.wikipedia.org › wiki › Coffee"
    assert first["snippet"].startswith("For other uses, see Coffee (disambiguation).")
    assert "Coffee" in first["snippet_highlighted_words"]


def test_firecrawl_second_page_slices_results():
    request = SearchRequest(q="coffee", engine="google", num=4, start=4)
    results = FirecrawlEngine().parse(load("firecrawl_coffee.json"), request)["organic_results"]
    everything = load("firecrawl_coffee.json")["data"]["web"]
    assert [r["link"] for r in results] == [r["url"] for r in everything[4:8]]
    assert results[0]["position"] == 1


def test_marginalia(coffee):
    results = MarginaliaEngine().parse(load("marginalia_coffee.json"), coffee)["organic_results"]
    assert_organic(results, 10)
    assert results[0]["title"] == "Coffee"


def test_brave(coffee):
    fragments = BraveEngine().parse(load("brave_coffee.json"), coffee)
    results = fragments["organic_results"]
    assert_organic(results, 2)
    assert results[0]["source"] == "Wikipedia"
    assert results[0]["snippet"] == (
        "Coffee is a beverage brewed from roasted, ground coffee beans."
    )
    assert results[0]["sitelinks"]["inline"][0]["title"] == "Coffee bean"
    assert results[0]["date"] == "3 days ago"
    assert results[1]["thumbnail"].endswith("thumb-coffee.jpg")
    assert fragments["related_questions"][0]["question"] == "Is coffee good for you?"
    graph = fragments["knowledge_graph"]
    assert graph["title"] == "Coffee" and graph["type"] == "Beverage"
    assert graph["country_of_origin"] == "Yemen"
    place = fragments["local_results"]["places"][0]
    assert place["gps_coordinates"] == {"latitude": 30.2650127, "longitude": -97.7460957}
    assert place["rating"] == 4.5 and place["reviews"] == 812


def test_mojeek(coffee):
    fragments = MojeekEngine().parse(load("mojeek_coffee.json"), coffee)
    assert_organic(fragments["organic_results"], 2)
    assert fragments["search_information"]["total_results"] == 3121948


def test_tavily(coffee):
    fragments = TavilyEngine().parse(load("tavily_coffee.json"), coffee)
    assert_organic(fragments["organic_results"], 2)
    assert fragments["organic_results"][1]["date"] == "2025-11-02"
    assert fragments["answer_box"]["type"] == "organic_result"


def test_searxng(coffee):
    fragments = SearxngEngine().parse(load("searxng_coffee.json"), coffee)
    assert_organic(fragments["organic_results"], 2)
    assert fragments["knowledge_graph"]["inception"] == "15th century"
    assert [r["query"] for r in fragments["related_searches"]] == ["coffee near me", "coffee beans"]
    assert "search_information" not in fragments


def test_duckduckgo_instant_answer_entity():
    request = SearchRequest(q="Albert Einstein", engine="google")
    fragments = DuckDuckGoInstantAnswerEngine().parse(load("ddg_ia_einstein.json"), request)
    graph = fragments["knowledge_graph"]
    assert graph["title"] == "Albert Einstein"
    assert graph["type"] == "Scientist"
    assert graph["website"] == "https://alberteinstein.com"
    assert graph["born"].startswith("March 14, 1879")
    assert graph["source"]["link"] == "https://en.wikipedia.org/wiki/Albert_Einstein"
    assert graph["header_images"][0]["image"].startswith("https://duckduckgo.com/i/")
    queries = [r["query"] for r in fragments["related_searches"]]
    assert "Bern Historical Museum" in queries
    assert all(not q.startswith("c/") for q in queries)


def test_duckduckgo_instant_answer_no_organic(coffee):
    fragments = DuckDuckGoInstantAnswerEngine().parse(load("ddg_ia_coffee.json"), coffee)
    assert "organic_results" not in fragments
    assert fragments["knowledge_graph"]["title"] == "Coffee"


def test_wikipedia():
    request = SearchRequest(q="Albert Einstein", engine="wikipedia")
    raw = {
        "base": "https://en.wikipedia.org",
        "search": load("wikipedia_search_einstein.json"),
        "summary": load("wikipedia_summary_einstein.json"),
    }
    fragments = WikipediaEngine().parse(raw, request)
    assert_organic(fragments["organic_results"], 10)
    assert fragments["organic_results"][0]["link"] == "https://en.wikipedia.org/wiki/Albert_Einstein"
    assert fragments["organic_results"][0]["source"] == "Wikipedia"
    graph = fragments["knowledge_graph"]
    assert graph["wikidata_id"] == "Q937"
    assert graph["type"] == "German-born theoretical physicist"
    assert fragments["search_information"]["total_results"] > 0


def test_openstreetmap_places():
    request = SearchRequest(q="coffee", engine="google", location="Austin, Texas")
    fragments = OpenStreetMapEngine().parse(load("nominatim_cafe_austin.json"), request)
    places = fragments["local_results"]["places"]
    assert len(places) == 10
    first = places[0]
    assert first["title"] == "Yummi Joy"
    assert first["type"] == "Cafe"
    assert first["address"] == "400 West Cesar Chavez Street, Austin, Texas 78701"
    assert first["gps_coordinates"]["latitude"] == 30.2652973
    assert first["place_id"] == "osm:node/8090124769"


def test_local_intent_detection():
    near = SearchRequest(q="coffee near Austin, TX", engine="g")
    assert local_query(near) == "cafe in Austin, TX"
    assert local_query(SearchRequest(q="pizza", engine="g", location="Brooklyn")) == (
        "restaurant in Brooklyn"
    )
    assert local_query(SearchRequest(q="coffee", engine="g")) is None
    assert local_query(SearchRequest(q="python asyncio", engine="g", location="Austin")) is None
