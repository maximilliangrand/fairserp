"""Request contracts: what each adapter actually sends upstream for a given query.

These pin method, URL, parameters and auth headers, and check that params an engine
cannot honor are reported as ignored rather than silently dropped.
"""

import json

import httpx
import pytest
import respx

from fairserp.engines import build_registry
from fairserp.engines.base import EngineContext
from fairserp.http import HttpPool
from fairserp.model import Freshness, SearchQuery
from tests.conftest import load, settings

QUERY = SearchQuery(q="coffee beans", country="de", language="de", safe=True,
                    freshness=Freshness.WEEK, num=10, offset=10)
CREDENTIALS = dict(brave_api_key="brave-secret", mojeek_api_key="mojeek-secret",
                   tavily_api_key="tavily-secret", searxng_url="http://searx.local")


async def capture(engine_name: str, response: object, query: SearchQuery = QUERY):
    s = settings(**CREDENTIALS)
    engine = build_registry(load_plugins=False)[engine_name]
    http = HttpPool(s)
    with respx.mock(assert_all_called=True) as mock:
        route = mock.route().respond(json=response)
        await engine.search(query, EngineContext(s, http))
    await http.close()
    return route.calls, engine


async def test_brave_contract():
    calls, engine = await capture("brave", load("brave_coffee.json"))
    request = calls[0].request
    assert request.method == "GET"
    assert request.url.host == "api.search.brave.com"
    assert dict(request.url.params) == {"q": "coffee beans", "count": "10", "offset": "1",
                                        "country": "de", "search_lang": "de",
                                        "safesearch": "strict", "freshness": "pw"}
    assert request.headers["X-Subscription-Token"] == "brave-secret"
    assert engine.applied(QUERY) == {"country", "language", "safe", "freshness"}
    assert "freshness" not in engine.applied(QUERY.model_copy(update={"freshness": "h"}))


async def test_mojeek_contract():
    calls, engine = await capture("mojeek", load("mojeek_coffee.json"))
    params = dict(calls[0].request.url.params)
    assert params == {"q": "coffee beans", "api_key": "mojeek-secret", "fmt": "json",
                      "s": "11", "t": "10", "rb": "DE", "lb": "DE", "safe": "1"}
    assert engine.applied(QUERY) == {"country", "language", "safe"}


async def test_tavily_contract():
    calls, engine = await capture("tavily", load("tavily_coffee.json"))
    request = calls[0].request
    assert request.method == "POST"
    assert json.loads(request.content) == {"query": "coffee beans", "include_answer": True,
                                           "max_results": 20, "country": "germany",
                                           "time_range": "week"}
    assert request.headers["Authorization"] == "Bearer tavily-secret"
    assert engine.applied(QUERY) == {"country", "freshness"}
    assert "country" not in engine.applied(QUERY.model_copy(update={"country": "zz"}))


async def test_firecrawl_contract_forwards_tbs_not_country():
    calls, engine = await capture("firecrawl", load("firecrawl_coffee.json"))
    body = json.loads(calls[0].request.content)
    assert body == {"query": "coffee beans", "limit": 20, "tbs": "qdr:w"}
    assert "Authorization" not in calls[0].request.headers
    assert engine.applied(QUERY) == {"freshness"}


async def test_marginalia_contract():
    calls, _ = await capture("marginalia", load("marginalia_coffee.json"))
    url = calls[0].request.url
    assert url.raw_path.startswith(b"/public/search/coffee%20beans")
    assert dict(url.params) == {"count": "20"}


async def test_searxng_contract():
    calls, engine = await capture("searxng", load("searxng_coffee.json"))
    url = calls[0].request.url
    assert url.host == "searx.local"
    assert dict(url.params) == {"q": "coffee beans", "format": "json", "pageno": "2",
                                "language": "de", "safesearch": "2", "time_range": "week"}


async def test_wikipedia_contract_validates_language_host():
    calls, _ = await capture("wikipedia", load("wikipedia_search_einstein.json"),
                             SearchQuery(q="einstein", language="de"))
    assert calls[0].request.url.host == "de.wikipedia.org"


async def test_duckduckgo_contract():
    calls, engine = await capture("duckduckgo_ia", load("ddg_ia_coffee.json"))
    assert dict(calls[0].request.url.params) == {"q": "coffee beans", "format": "json",
                                                 "no_html": "1", "skip_disambig": "1"}
    assert engine.applied(QUERY) == set()


async def test_no_engine_follows_redirects():
    s = settings()
    engine = build_registry(load_plugins=False)["wikipedia"]
    http = HttpPool(s)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__startswith="https://en.wikipedia.org/").respond(
            302, headers={"Location": "http://169.254.169.254/latest"})
        internal = mock.get(url__startswith="http://169.254.169.254/").respond(200)
        with pytest.raises(Exception, match="redirected"):
            await engine.search(SearchQuery(q="x"), EngineContext(s, http))
    assert not internal.called
    await http.close()


def test_trust_env_is_off_by_default(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://evil-proxy:3128")
    pool = HttpPool(settings())
    client = pool._client(None)
    assert client._trust_env is False
    assert isinstance(client, httpx.AsyncClient)


def test_proxy_is_sticky_not_rotated():
    pool = HttpPool(settings(proxy="http://egress:3128",
                             engine_proxies={"wikipedia": "none", "brave": "http://b:1"}))
    assert {pool.proxy_for("firecrawl") for _ in range(5)} == {"http://egress:3128"}
    assert pool.proxy_for("wikipedia") is None
    assert pool.proxy_for("brave") == "http://b:1"
