"""HTTP-level behavior: auth, quotas, caching, archive, async, compatibility details.

Upstreams are mocked with respx and answer from the saved fixtures.
"""

import re

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from fairserp.app import create_app
from fairserp.config import Settings
from tests.conftest import load

PUBLIC = "https://serp.example.org"


def make_client(**overrides) -> TestClient:
    values = {"api_keys": "good:3,other", "public_url": PUBLIC, **overrides}
    settings = Settings(_env_file=None, **values)
    return TestClient(create_app(settings))


@pytest.fixture
def upstreams():
    with respx.mock(assert_all_called=False) as mock:
        mock.post("https://api.firecrawl.dev/v2/search", name="firecrawl").respond(
            json=load("firecrawl_coffee.json")
        )
        mock.get("https://api.duckduckgo.com/").respond(json=load("ddg_ia_coffee.json"))
        mock.get("https://en.wikipedia.org/w/api.php").respond(
            json={"query": {"searchinfo": {"totalhits": 0}, "search": []}}
        )
        mock.get(
            url__regex=r"https://nominatim\.openstreetmap\.org/search.*", name="nominatim"
        ).respond(json=load("nominatim_cafe_austin.json"))
        yield mock


def test_invalid_key_is_checked_before_params(upstreams):
    with make_client() as client:
        response = client.get("/search.json", params={"api_key": "nope"})
    assert response.status_code == 401
    assert response.json()["error"].startswith("Invalid API key")


def test_missing_query(upstreams):
    with make_client() as client:
        response = client.get("/search", params={"api_key": "good"})
    assert response.status_code == 400
    assert response.json() == {"error": "Missing query `q` parameter."}


def test_google_search_is_serpapi_shaped(upstreams):
    with make_client() as client:
        response = client.get(
            "/search",
            params={"engine": "google", "q": "coffee", "api_key": "good", "source": "python",
                    "output": "json"},
        )
    assert response.status_code == 200
    data = response.json()
    meta = data["search_metadata"]
    assert re.fullmatch(r"[0-9a-f]{24}", meta["id"])
    assert meta["status"] == "Success"
    assert meta["json_endpoint"] == f"{PUBLIC}/searches/{meta['id']}.json"
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d UTC", meta["created_at"])
    assert [s["engine"] for s in meta["fairserp_sources"]] == [
        "firecrawl", "duckduckgo_ia", "wikipedia",
    ]
    assert data["search_parameters"] == {"engine": "google", "q": "coffee"}
    assert data["search_information"]["organic_results_state"] == "Results for exact spelling"
    assert len(data["organic_results"]) == 10
    assert data["knowledge_graph"]["title"] == "Coffee"
    assert data["knowledge_graph"]["serpapi_knowledge_graph_search_link"].startswith(PUBLIC)
    assert data["serpapi_pagination"]["next"] == (
        f"{PUBLIC}/search.json?engine=google&q=coffee&start=10"
    )
    assert data["pagination"]["current"] == 1
    assert "error" not in data


def test_second_page_skips_enrichment(upstreams):
    with make_client() as client:
        data = client.get("/search.json", params={"q": "coffee", "start": "5", "num": "5",
                                                  "api_key": "good"}).json()
    assert [s["engine"] for s in data["search_metadata"]["fairserp_sources"]] == ["firecrawl"]
    assert data["search_information"]["page_number"] == 2
    assert data["search_parameters"]["start"] == 5
    assert data["serpapi_pagination"]["previous"].endswith("start=0")
    assert "knowledge_graph" not in data


def test_local_results_from_location(upstreams):
    with make_client() as client:
        data = client.get("/search.json", params={"q": "coffee", "location": "Austin, Texas",
                                                  "api_key": "good"}).json()
    assert data["local_results"]["places"][0]["title"] == "Yummi Joy"
    assert data["search_parameters"]["location_used"] == "Austin, Texas"


def test_google_maps_returns_place_list(upstreams):
    with make_client() as client:
        data = client.get("/search.json", params={"engine": "google_maps", "q": "coffee",
                                                  "location": "Austin", "api_key": "good"}).json()
    assert isinstance(data["local_results"], list)
    assert data["local_results"][0]["position"] == 1


def test_cache_hit_is_free_and_identical(upstreams):
    with make_client() as client:
        params = {"q": "coffee", "api_key": "good"}
        first = client.get("/search.json", params=params).json()
        second = client.get("/search.json", params={**params, "source": "nodejs"}).json()
        account = client.get("/account.json", params={"api_key": "good"}).json()
    assert first["search_metadata"]["id"] == second["search_metadata"]["id"]
    assert upstreams["firecrawl"].call_count == 1
    assert account["this_hour_searches"] == 1


def test_no_cache_bypasses_cache(upstreams):
    with make_client() as client:
        params = {"q": "coffee", "api_key": "good"}
        client.get("/search.json", params=params)
        client.get("/search.json", params={**params, "no_cache": "true"})
    assert upstreams["firecrawl"].call_count == 2


def test_hourly_limit(upstreams):
    with make_client() as client:
        statuses = [
            client.get("/search.json", params={"q": f"coffee {i}", "api_key": "good"}).status_code
            for i in range(4)
        ]
        other = client.get("/search.json", params={"q": "coffee", "api_key": "other"})
    assert statuses == [200, 200, 200, 429]
    assert other.status_code == 200


def test_archive_is_private_to_the_owner(upstreams):
    with make_client() as client:
        search_id = client.get("/search.json", params={"q": "coffee", "api_key": "good"}).json()[
            "search_metadata"]["id"]
        mine = client.get(f"/searches/{search_id}.json", params={"api_key": "good"})
        no_ext = client.get(f"/searches/{search_id}", params={"api_key": "good"})
        theirs = client.get(f"/searches/{search_id}.json", params={"api_key": "other"})
        markdown = client.get(f"/searches/{search_id}.md", params={"api_key": "good"})
    assert mine.status_code == 200 and no_ext.status_code == 200
    assert mine.json()["search_metadata"]["id"] == search_id
    assert theirs.status_code == 404
    assert markdown.text.startswith("# coffee")


def test_async_search_completes(upstreams):
    client = make_client()
    with client:
        submitted = client.get("/search.json", params={"q": "coffee", "async": "true",
                                                       "api_key": "good"}).json()
        search_id = submitted["search_metadata"]["id"]
        services = client.app.state.services
        client.portal.call(_wait_for_background, services)
        final = client.get(f"/searches/{search_id}.json", params={"api_key": "good"}).json()
    assert final["search_metadata"]["status"] == "Success"
    assert len(final["organic_results"]) == 10


async def _wait_for_background(services) -> None:
    import asyncio

    await asyncio.gather(*services.background)


def test_async_and_no_cache_conflict(upstreams):
    with make_client() as client:
        response = client.get("/search.json", params={"q": "c", "async": "true",
                                                      "no_cache": "true", "api_key": "good"})
    assert response.status_code == 400


def test_json_restrictor(upstreams):
    with make_client() as client:
        data = client.get("/search.json", params={
            "q": "coffee", "api_key": "good",
            "json_restrictor": "organic_results[0:2].{title, link}",
        }).json()
    assert data == {"organic_results": [
        {"title": "Coffee - Wikipedia", "link": "https://en.wikipedia.org/wiki/Coffee"},
        {"title": "Coffee - The Nutrition Source",
         "link": "https://nutritionsource.hsph.harvard.edu/food-features/coffee/"},
    ]}


def test_fallback_to_next_organic_engine(upstreams):
    upstreams["firecrawl"].respond(429)
    upstreams.get(url__regex=r"https://api\.marginalia\.nu/.*").respond(
        json=load("marginalia_coffee.json")
    )
    with make_client() as client:
        data = client.get("/search.json", params={"q": "coffee", "api_key": "good"}).json()
    sources = data["search_metadata"]["fairserp_sources"]
    assert sources[0] == {"engine": "firecrawl", "status": "error",
                          "error": "upstream rate limit reached"}
    assert sources[1] == {"engine": "marginalia", "status": "ok"}
    assert data["organic_results"][0]["title"] == "Coffee"


def test_all_engines_failing_is_an_error(upstreams):
    upstreams["firecrawl"].respond(503)
    upstreams.get(url__regex=r"https://api\.marginalia\.nu/.*").respond(503)
    with make_client(organic_priority="firecrawl,marginalia") as client:
        response = client.get("/search.json", params={"q": "coffee", "api_key": "good"})
        account = client.get("/account.json", params={"api_key": "good"}).json()
    assert response.status_code == 502
    assert "no organic engine succeeded" in response.json()["error"]
    assert account["this_hour_searches"] == 0


def test_brave_is_preferred_and_not_cached(upstreams):
    brave = upstreams.get("https://api.search.brave.com/res/v1/web/search").respond(
        json=load("brave_coffee.json")
    )
    with make_client(brave_api_key="k") as client:
        params = {"q": "coffee", "api_key": "good"}
        data = client.get("/search.json", params=params).json()
        client.get("/search.json", params=params)
    assert data["related_questions"][0]["question"] == "Is coffee good for you?"
    assert data["knowledge_graph"]["type"] == "Beverage"  # provider's own graph wins
    assert brave.call_count == 2
    assert brave.calls[0].request.headers["X-Subscription-Token"] == "k"


def test_unconfigured_engine(upstreams):
    with make_client() as client:
        response = client.get("/search.json", params={"engine": "brave", "q": "x",
                                                      "api_key": "good"})
    assert response.status_code == 503
    assert "FAIRSERP_BRAVE_API_KEY" in response.json()["error"]


def test_unknown_engine(upstreams):
    with make_client() as client:
        response = client.get("/search.json", params={"engine": "yahoo", "q": "x",
                                                      "api_key": "good"})
    assert response.status_code == 400


def test_empty_results_return_200_with_error(upstreams):
    upstreams["firecrawl"].respond(
        json={"success": True, "data": {"web": []}}
    )
    with make_client() as client:
        response = client.get("/search.json", params={"q": "zzqx", "api_key": "good"})
    assert response.status_code == 200
    assert response.json()["search_information"]["organic_results_state"] == "Fully empty"
    assert "hasn't returned any results" in response.json()["error"]


def test_bearer_and_legacy_key_names(upstreams):
    with make_client() as client:
        bearer = client.get("/account", headers={"Authorization": "Bearer good"})
        legacy = client.get("/account", params={"serp_api_key": "good"})
    assert bearer.status_code == 200 and legacy.status_code == 200


def test_auth_disabled_mode(upstreams):
    with make_client(api_keys="", auth_disabled=True) as client:
        assert client.get("/search.json", params={"q": "coffee"}).status_code == 200


def test_no_keys_configured_rejects_everything(upstreams):
    with make_client(api_keys="") as client:
        assert client.get("/search.json", params={"q": "coffee"}).status_code == 401


def test_locations_api(upstreams):
    upstreams["nominatim"].respond(
        json=load("nominatim_austin.json")
    )
    with make_client() as client:
        data = client.get("/locations.json", params={"q": "Austin", "limit": 1}).json()
    assert data[0]["canonical_name"] == "Austin,Travis County,Texas,United States"
    assert data[0]["gps"] == [-97.7474086, 30.2649261]
    assert data[0]["target_type"] == "City"


def test_engines_listing(upstreams):
    with make_client() as client:
        listing = client.get("/engines").json()
    by_name = {e["name"]: e for e in listing["engines"]}
    assert by_name["firecrawl"]["configured"] is True
    assert by_name["brave"]["configured"] is False
    assert {e["compliance"] for e in listing["engines"]} <= {
        "official-api", "open-data-api", "user-operated",
    }


def test_proxy_is_used_for_engine(upstreams):
    settings = Settings(_env_file=None, proxies="http://proxy-a:3128,http://proxy-b:3128",
                        engine_proxies={"wikipedia": "none"})
    from fairserp.http import HttpPool

    pool = HttpPool(settings)
    assert [pool._proxy_for("firecrawl") for _ in range(3)] == [
        "http://proxy-a:3128", "http://proxy-b:3128", "http://proxy-a:3128",
    ]
    assert pool._proxy_for("wikipedia") is None
    assert isinstance(pool._client(None), httpx.AsyncClient)
