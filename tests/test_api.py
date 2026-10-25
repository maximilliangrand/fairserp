"""HTTP behavior of the native and SerpApi endpoints, on deterministic stub engines."""

import logging

import pytest
import respx
from fastapi.testclient import TestClient

from fairserp.app import create_app
from fairserp.engines.base import ErrorKind, Role
from fairserp.logs import RedactSecrets, redact_url
from fairserp.model import Answer, EngineOutput
from tests import stubs
from tests.conftest import load, settings

PUBLIC = "https://serp.example.org"
AUTH = {"Authorization": "Bearer good"}


def registry(*engines):
    engines_by_name = {e.name: e for e in engines}
    engines_by_name.setdefault("wikidata", stubs.entity_engine())
    engines_by_name.setdefault("duckduckgo_ia", stubs.answer_engine())
    engines_by_name.setdefault("openstreetmap", stubs.local_engine())
    return engines_by_name


def client(*engines, **overrides) -> TestClient:
    organic = [e.name for e in engines if e.role == Role.ORGANIC] or ["a"]
    values = {"organic_priority": ",".join(organic), "upstream_budgets": "",
              "serpapi_engines": "google:auto,google_maps:openstreetmap", **overrides}
    return TestClient(create_app(settings(**values), registry(*engines)))


def usage(c: TestClient) -> int:
    return c.get("/account.json", headers=AUTH).json()["this_hour_searches"]


# ------------------------------------------------------------------ auth and validation


def test_invalid_key_is_checked_before_params():
    with client(stubs.organic("a")) as c:
        response = c.get("/search.json", params={"api_key": "nope"})
    assert response.status_code == 401 and "Bearer" in response.json()["error"]


def test_key_sources():
    with client(stubs.organic("a")) as c:
        for kwargs in ({"headers": AUTH}, {"params": {"api_key": "good"}},
                       {"params": {"serp_api_key": "good"}}):
            assert c.get("/account", **kwargs).status_code == 200


def test_no_keys_configured_rejects_everything():
    with client(stubs.organic("a"), api_keys="") as c:
        assert c.get("/search.json", params={"q": "x"}).status_code == 401


def test_serpapi_engine_names_need_an_explicit_migration_mapping():
    with client(stubs.organic("a"), serpapi_engines="") as c:
        response = c.get("/search.json", params={"engine": "google", "q": "x"}, headers=AUTH)
    assert response.status_code == 400
    assert "FAIRSERP_SERPAPI_ENGINES=google:auto" in response.json()["error"]


def test_invalid_json_restrictor_is_rejected_before_searching_or_billing():
    engine = stubs.organic("a")
    with client(engine) as c:
        response = c.get("/search.json", params={"q": "x", "json_restrictor": "a[x]"},
                         headers=AUTH)
        assert response.status_code == 400
        assert usage(c) == 0
    assert engine.calls == []


@pytest.mark.parametrize("hl", ["169.254.169.254/latest#", "en.evil.com", "e"])
def test_language_cannot_name_a_host(hl):
    with client(stubs.organic("a")) as c:
        response = c.get("/search.json", params={"q": "x", "hl": hl}, headers=AUTH)
    assert response.status_code == 400


# ------------------------------------------------------------------ SerpApi rendering


def test_serpapi_response_reports_provenance_and_params():
    engine = stubs.organic("a", rights_=stubs.rights(attribution="Results from A"))
    with client(engine) as c:
        data = c.get("/search.json", params={"engine": "google", "q": "coffee", "gl": "de",
                                             "device": "mobile", "source": "python"},
                     headers=AUTH).json()
    meta = data["search_metadata"]
    assert meta["status"] == "Success"
    assert meta["json_endpoint"] == f"{PUBLIC}/searches/{meta['id']}.json"
    fair = meta["fairserp"]
    assert fair["requested_engine"] == "google" and fair["resolved_engine"] == "auto"
    assert fair["params"] == {"applied": [], "ignored": ["device", "gl"]}
    assert "Results from A" in fair["attribution"]
    assert data["search_parameters"]["engine"] == "google"
    first = data["organic_results"][0]
    assert first["position"] == 1 and first["link"] == "https://a.example/0"
    assert first["fairserp_sources"][0]["engine"] == "a"
    assert data["knowledge_graph"]["wikidata_id"] == "Q8486"
    assert data["answer_box"]["answer"] == "42"


def test_generated_answers_are_never_an_answer_box():
    gen = stubs.Stub("a", output=EngineOutput(web=stubs.web("a", 3), answer=Answer(
        text="A model wrote this.", generated=True, citations=["https://a.example/0"])))
    with client(gen) as c:
        data = c.get("/search.json", params={"q": "x"}, headers=AUTH).json()
    assert "answer_box" not in data
    assert data["ai_overview"]["fairserp_generated"] is True


def test_google_maps_alias_returns_a_place_list():
    with client(stubs.organic("a")) as c:
        data = c.get("/search.json", params={"engine": "google_maps", "q": "coffee",
                                             "location": "Austin"}, headers=AUTH).json()
    assert isinstance(data["local_results"], list) and "error" not in data


def test_pagination_links_are_unique_and_honest():
    with client(stubs.organic("a")) as c:
        page2 = c.get("/search.json", params={"engine": "bing", "q": "x", "first": "11",
                                              "count": "10"}, headers=AUTH)
    assert page2.status_code == 400  # bing is not mapped in this deployment
    with client(stubs.organic("a"), serpapi_engines="bing:auto") as c:
        data = c.get("/search.json", params={"engine": "bing", "q": "x", "first": "11",
                                             "count": "10"}, headers=AUTH).json()
    last = stubs.Stub("a", output=EngineOutput(web=stubs.web("a", 3), has_more=False))
    pages = data["serpapi_pagination"]
    links = list(pages["other_pages"].values())
    assert len(links) == len(set(links))
    assert "first=" not in pages["next"] and "start=20" in pages["next"]
    assert pages["previous"].endswith("start=0")
    with client(last) as c:
        data = c.get("/search.json", params={"q": "x"}, headers=AUTH).json()
    assert "next" not in data["serpapi_pagination"]


def test_startup_warns_when_auto_has_no_web_provider(caplog):
    with caplog.at_level(logging.WARNING, logger="fairserp"), client(stubs.organic("a")):
        pass
    assert "no web search provider" not in caplog.text
    with caplog.at_level(logging.WARNING, logger="fairserp"), \
            client(stubs.organic("a"), organic_priority="missing"):
        pass
    assert "no web search provider is enabled" in caplog.text


def test_self_links_use_the_request_host_when_public_url_is_unset():
    with client(stubs.organic("a"), public_url=None) as c:
        data = c.get("/search.json", params={"q": "x"}, headers=AUTH).json()
    assert data["serpapi_pagination"]["next"].startswith("http://testserver/search.json")


# ------------------------------------------------------------------ honest failures


def test_empty_is_200_with_error_and_billed():
    with client(stubs.Stub("a")) as c:
        response = c.get("/search.json", params={"q": "zzqx"}, headers=AUTH)
        assert usage(c) == 1
    assert response.status_code == 200
    assert response.json()["search_information"]["organic_results_state"] == "Fully empty"
    assert "error" in response.json()


def test_upstream_failure_is_502_and_free():
    with client(stubs.failing("a")) as c:
        response = c.get("/search.json", params={"q": "x"}, headers=AUTH)
        assert usage(c) == 0
    assert response.status_code == 502
    body = response.json()
    assert "a broke" in body["error"]
    assert body["search_metadata"]["fairserp"]["status"] == "failed"


def test_upstream_rate_limit_is_429_with_retry_after():
    with client(stubs.failing("a", ErrorKind.RATE_LIMITED, 42)) as c:
        response = c.get("/search.json", params={"q": "x"}, headers=AUTH)
    assert response.status_code == 429 and response.headers["Retry-After"] == "42"


def test_hourly_quota():
    with client(stubs.organic("a")) as c:
        statuses = [c.get("/search.json", params={"q": f"q{i}"}, headers=AUTH).status_code
                    for i in range(4)]
        limited = c.get("/search.json", params={"q": "q9"}, headers=AUTH)
    assert statuses == [200, 200, 200, 429]
    assert int(limited.headers["Retry-After"]) > 0


def test_cache_hits_are_free():
    engine = stubs.organic("a")
    with client(engine) as c:
        c.get("/search.json", params={"q": "x"}, headers=AUTH)
        c.get("/search.json", params={"q": "x", "source": "nodejs"}, headers=AUTH)
        assert usage(c) == 1
    assert len(engine.calls) == 1


def test_native_error_envelope():
    with client(stubs.organic("a")) as c:
        bad = c.get("/v1/search", params={"q": "x", "num": "500"}, headers=AUTH)
        missing = c.get("/v1/nope", headers=AUTH)
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "invalid_parameter"
    assert missing.status_code == 404 and "error" in missing.json()


def test_native_search():
    with client(stubs.organic("a")) as c:
        data = c.get("/v1/search", params={"q": "coffee", "freshness": "d"},
                     headers=AUTH).json()
        posted = c.post("/v1/search", json={"q": "coffee", "mode": "private"},
                        headers=AUTH).json()
    assert data["status"] == "success" and data["params"]["ignored"] == ["freshness"]
    assert data["results"][0]["sources"][0] == {"engine": "a", "rank": 1,
                                                "provenance": "own-index"}
    assert [p["engine"] for p in posted["providers"]] == ["a"]


# ------------------------------------------------------------------ archive and retention


def test_archive_is_owner_only():
    with client(stubs.organic("a")) as c:
        search_id = c.get("/search.json", params={"q": "x"}, headers=AUTH).json()[
            "search_metadata"]["id"]
        mine = c.get(f"/searches/{search_id}.json", headers=AUTH)
        native = c.get(f"/v1/searches/{search_id}", headers=AUTH)
        theirs = c.get(f"/searches/{search_id}.json", params={"api_key": "other"})
    assert mine.status_code == 200 and native.status_code == 200
    assert theirs.status_code == 404


def test_results_that_may_not_be_stored_are_not_archived():
    brave = stubs.organic("brave", rights_=stubs.rights(storage=0))
    with client(brave) as c:
        data = c.get("/search.json", params={"q": "x"}, headers=AUTH).json()
        archived = c.get(f"/searches/{data['search_metadata']['id']}.json", headers=AUTH)
        async_ = c.get("/search.json", params={"q": "y", "async": "true"}, headers=AUTH)
    assert data["search_metadata"]["fairserp"]["archived"] is False
    assert archived.status_code == 410
    assert async_.status_code == 400 and "brave" in async_.json()["error"]


def test_async_search_completes_and_lands_in_archive():
    with client(stubs.organic("a", delay=0.05)) as c:
        stub = c.get("/search.json", params={"q": "x", "async": "true"}, headers=AUTH).json()
        assert stub["search_metadata"]["status"] == "Processing"
        search_id = stub["search_metadata"]["id"]
        services = c.app.state.services
        c.portal.call(_drain, services)
        final = c.get(f"/searches/{search_id}.json", headers=AUTH).json()
    assert final["search_metadata"]["status"] == "Success"


async def _drain(services):
    import asyncio

    await asyncio.gather(*services.background)


def test_interrupted_async_job_reads_as_failed():
    with client(stubs.organic("a")) as c:
        services = c.app.state.services

        async def orphan():
            from fairserp.limits import owner_id
            await services.archive.start_job(owner_id("good"), "deadbeef" * 3, {}, -1)

        c.portal.call(orphan)
        response = c.get("/searches/" + "deadbeef" * 3 + ".json", headers=AUTH).json()
    assert response["search_metadata"]["status"] == "Error"
    assert "interrupted" in response["error"]


def test_zero_trace_is_not_archived_cached_or_async():
    engine = stubs.organic("a")
    with client(engine) as c:
        data = c.get("/search.json", params={"q": "x", "zero_trace": "true"},
                     headers=AUTH).json()
        c.get("/search.json", params={"q": "x"}, headers=AUTH)
        archived = c.get(f"/searches/{data['search_metadata']['id']}.json", headers=AUTH)
        both = c.get("/search.json", params={"q": "x", "zero_trace": "true", "async": "true"},
                     headers=AUTH)
    assert archived.status_code == 404 and both.status_code == 400
    assert len(engine.calls) == 2


# ------------------------------------------------------------------ security


def test_html_output_only_links_http_and_sets_csp():
    evil = stubs.Stub("a", output=EngineOutput(web=[
        stubs.web("ok", 1)[0],
        stubs.web("x", 1)[0].model_copy(update={"url": "javascript:alert(1)"})]))
    with client(evil) as c:
        response = c.get("/search.html", params={"q": "x"}, headers=AUTH)
    assert "default-src 'none'" in response.headers["Content-Security-Policy"]
    assert 'href="javascript' not in response.text
    assert 'href="https://ok.example/0"' in response.text


@pytest.mark.parametrize("path", [
    "/search.json?q=x&api_key=SECRET",
    "/search.json?q=x&api%5fkey=SECRET",
    "/search.json?q=x&API_KEY=SECRET",
    "/search.json?q=x&serp_api_key=SECRET",
])
def test_access_logs_redact_keys_including_encoded_names(path):
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s "%s %s"',
                               ("1.2.3.4", "GET", path), None)
    RedactSecrets().filter(record)
    assert "SECRET" not in record.getMessage()


def test_zero_trace_queries_are_redacted_from_logs():
    assert "sensitive" not in redact_url("/search?q=sensitive&zero_trace=true")
    assert "sensitive" in redact_url("/search?q=sensitive")


def test_outbound_urls_are_redacted_in_httpx_logs():
    record = logging.LogRecord("httpx", logging.INFO, "", 0, "HTTP Request: %s %s",
                               ("GET", "https://api.mojeek.com/search?q=x&api_key=SECRET"), None)
    RedactSecrets().filter(record)
    assert "SECRET" not in record.getMessage()


# ------------------------------------------------------------------ ops


def test_readyz_checks_stores():
    with client(stubs.organic("a")) as c:
        assert c.get("/readyz").json() == {"status": "ready",
                                           "checks": {"cache": "ok", "state": "ok"}}

        async def broken():
            return False

        c.app.state.services.state.ping = broken
        assert c.get("/readyz").status_code == 503


def test_engines_listing_requires_auth_and_shows_rights():
    with client(stubs.organic("a")) as c:
        assert c.get("/v1/engines").status_code == 401
        listing = c.get("/v1/engines", headers=AUTH).json()
    entry = {e["name"]: e for e in listing["engines"]}["a"]
    assert entry["rights"]["provenance"] == "own-index" and entry["enabled"] is True


def test_locations_requires_a_key_and_handles_bad_upstream_data():
    # A self-hosted Nominatim has no politeness spacing, so the test needs no waiting.
    local = {"nominatim_url": "http://nominatim.local"}
    with client(stubs.organic("a"), **local) as c, respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__startswith="http://nominatim.local/search")
        route.respond(json=load("nominatim_austin.json"))
        assert c.get("/locations.json", params={"q": "Austin"}).status_code == 401
        ok = c.get("/locations.json", params={"q": "Austin", "limit": 1}, headers=AUTH)
        assert ok.json()[0]["canonical_name"] == "Austin,Travis County,Texas,United States"
        route.respond(json=[{"unexpected": True}])
        broken = c.get("/locations.json", params={"q": "Bogus"}, headers=AUTH)
    assert broken.status_code == 502 and "error" in broken.json()


def test_playground_is_served_under_a_hash_csp():
    with client(stubs.organic("a")) as c:
        response = c.get("/")
    csp = response.headers["Content-Security-Policy"]
    assert response.status_code == 200 and "fairserp playground" in response.text
    assert "script-src 'sha256-" in csp and "unsafe-inline" not in csp
    assert "connect-src 'self'" in csp
    assert "fonts.googleapis" not in response.text and "<script src" not in response.text


def test_no_engine_enabled_is_a_structured_error():
    from fairserp.rights import Provenance
    hidden = stubs.organic("firecrawl", rights_=stubs.rights(Provenance.UNDISCLOSED))
    with client(hidden) as c:
        body = c.get("/v1/search", params={"q": "x"}, headers=AUTH).json()
    assert body["error"]["code"] == "no_engine_enabled"
    assert body["error"]["details"][0]["engine"] == "firecrawl"
    assert len(body["error"]["message"]) < 300


def test_local_notes_and_area_reach_both_wire_formats():
    from fairserp.model import Area
    note = "Only 1 cafe result inside X; widened to nearby places."
    local = stubs.local_engine()
    local.output = local.output.model_copy(update={
        "notes": [note], "area": Area(name="X", latitude=1, longitude=2, bbox=[0, 2, 1, 3],
                                      has_boundary=True)})
    with TestClient(create_app(settings(organic_priority="a", upstream_budgets="",
                                        serpapi_engines="google_maps:openstreetmap"),
                               registry(stubs.organic("a"), local))) as c:
        native = c.get("/v1/search", params={"q": "coffee", "location": "X",
                                             "vertical": "local"}, headers=AUTH).json()
        serp = c.get("/search.json", params={"engine": "google_maps", "q": "coffee",
                                             "location": "X"}, headers=AUTH).json()
    assert native["notes"] == [note] and native["area"]["name"] == "X"
    assert serp["search_metadata"]["fairserp"]["notes"] == [note]
