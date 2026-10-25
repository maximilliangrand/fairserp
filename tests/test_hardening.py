"""Regression tests for the issues an adversarial review reproduced against v0.2."""

import asyncio
import logging

import fakeredis
import httpx
import pytest
import redis.asyncio as redis
import respx
from fastapi.testclient import TestClient

from fairserp.app import create_app
from fairserp.engines.base import EngineContext, EngineError, ErrorKind
from fairserp.engines.marginalia import MarginaliaEngine
from fairserp.http import HttpPool
from fairserp.limits import CircuitBreaker
from fairserp.logs import RedactSecrets, redact_url
from fairserp.model import SearchQuery
from fairserp.restrictor import apply
from fairserp.store import MemoryStore, RedisStore
from tests import stubs  # noqa: F401
from tests.conftest import settings
from tests.test_api import AUTH, client


def test_httpx_url_objects_and_path_secrets_are_redacted():
    record = logging.LogRecord(
        "httpx", logging.INFO, "", 0, 'HTTP Request: %s %s "%s"',
        ("GET", httpx.URL("https://api.marginalia.nu/MARG_SECRET/search/x?count=10&api_key=K2"),
         "HTTP/1.1 200 OK"), None)
    RedactSecrets(secrets=["MARG_SECRET"]).filter(record)
    message = record.getMessage()
    assert "MARG_SECRET" not in message and "K2" not in message


def test_configured_secrets_feed_the_filter():
    s = settings(marginalia_api_key="MARG_SECRET", brave_api_key="BRAVE_SECRET")
    assert {"MARG_SECRET", "BRAVE_SECRET", "good", "other"} <= set(s.secret_values())


def test_raw_fragment_in_request_line_is_redacted():
    assert "good" not in redact_url("/account.json?x=1#&api_key=good")


def test_keys_are_not_stored_at_rest():
    with client(stubs.organic("a")) as c:
        c.get("/search.json", params={"q": "x", "api_key": "good"})
        records = c.app.state.services.cache._data
    assert not any("good" in str(value) for value in records.values())


def test_upstream_rejections_do_not_trip_the_shared_circuit():
    async def run():
        breaker = CircuitBreaker(MemoryStore(), failures=2, cooldown=30)
        for _ in range(5):
            await breaker.record("a", EngineError("a", "404", ErrorKind.REJECTED))
            await breaker.record("a", EngineError("a", "throttled", ErrorKind.THROTTLED))
        await breaker.check("a")  # still closed
        for _ in range(2):
            await breaker.record("a", EngineError("a", "502"))
        with pytest.raises(EngineError):
            await breaker.check("a")
    asyncio.run(run())


async def test_marginalia_refuses_path_rewriting_queries():
    s = settings()
    http = HttpPool(s)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.route().respond(json={"results": []})
        with pytest.raises(EngineError, match="dots"):
            await MarginaliaEngine().search(SearchQuery(q=".."), EngineContext(s, http))
    assert not route.called
    await http.close()


def test_markdown_output_cannot_carry_script_links():
    evil = stubs.Stub("a", output=stubs.EngineOutput(web=[stubs.web("x", 1)[0].model_copy(
        update={"url": "https://evil.example/a)[Verify](javascript:alert(1)",
                "snippet": "Read [more](javascript:alert((1))) here"})]))
    with client(evil) as c:
        text = c.get("/search.md", params={"q": "x"}, headers=AUTH).text
    assert "](javascript" not in text and "(javascript" not in text.replace("\\(", "")


def test_unicode_digits_in_json_restrictor_are_a_400_and_free():
    engine = stubs.organic("a")
    with client(engine) as c:
        response = c.get("/search.json", params={"q": "x", "json_restrictor":
                                                 "organic_results[١]"}, headers=AUTH)
        used = c.get("/account.json", headers=AUTH).json()["this_hour_searches"]
    assert response.status_code == 400 and used == 0 and not engine.calls


def test_bad_mode_and_archive_restrictor_are_400s():
    with client(stubs.organic("a")) as c:
        mode = c.get("/search.json", params={"q": "x", "fairserp_mode": "bogus"}, headers=AUTH)
        archive = c.get("/searches/abc.json", params={"json_restrictor": "a[x"}, headers=AUTH)
    assert mode.status_code == 400 and archive.status_code == 400


def test_restrictor_merges_by_source_index():
    data = {"organic_results": [{"title": "T0", "link": "L0"}, {"title": "T1", "link": "L1"}]}
    assert apply(data, "organic_results[0].title,organic_results[1].link") == {
        "organic_results": [{"title": "T0"}, {"link": "L1"}]}
    assert apply(data, "organic_results[].title,organic_results[].link") == data


def test_default_budgets_cap_one_key_below_the_server_total():
    budgets = settings().parsed_budgets()
    server = [b for b in budgets if not b.per_key and b.window == "month"][0]
    per_key = [b for b in budgets if b.per_key and b.window == "month"][0]
    assert per_key.limit < server.limit


def test_auth_disabled_cannot_mint_quota_with_made_up_keys():
    with client(stubs.organic("a"), api_keys="", auth_disabled=True,
                default_hourly_limit=2) as c:
        codes = [c.get("/search.json", params={"q": f"q{i}", "api_key": f"k{i}"},
                       headers={"Host": "localhost"}).status_code for i in range(4)]
    assert codes == [200, 200, 429, 429]


def test_locations_is_metered():
    local = {"nominatim_url": "http://nominatim.local"}
    with client(stubs.organic("a"), **local) as c, respx.mock(assert_all_called=False) as mock:
        mock.get(url__startswith="http://nominatim.local/").respond(json=[])
        codes = [c.get("/locations.json", params={"q": f"p{i}"}, headers=AUTH).status_code
                 for i in range(4)]
    assert codes == [200, 200, 200, 429]


async def test_redis_pool_waits_instead_of_failing_under_bursts():
    server = fakeredis.FakeServer()
    pool = redis.BlockingConnectionPool(
        connection_class=fakeredis.aioredis.FakeAsyncRedisConnection, server=server,
        max_connections=5, timeout=5, decode_responses=True)
    store = RedisStore(client=redis.Redis(connection_pool=pool))
    results = await asyncio.gather(*(store.incr("k", 60) for _ in range(200)))
    assert sorted(results) == list(range(1, 201))


def test_throttling_does_not_open_circuits_for_other_tenants():
    flaky = stubs.failing("a", ErrorKind.THROTTLED)
    with client(flaky, circuit_failures=1) as c:
        for i in range(3):
            c.get("/search.json", params={"q": f"q{i}"}, headers=AUTH)
    assert len(flaky.calls) == 3


def test_app_wires_secret_values_into_log_filters():
    TestClient(create_app(settings(brave_api_key="BRAVE_SECRET_VALUE")))
    filters = [f for f in logging.getLogger("httpx").filters if isinstance(f, RedactSecrets)]
    assert filters and "BRAVE_SECRET_VALUE" in filters[0].secrets


def test_mcp_server_redacts_provider_keys_in_its_logs():
    from fairserp.mcp_server import build

    build(settings(mojeek_api_key="MOJEEK_SECRET_VALUE"))
    filters = [f for f in logging.getLogger("httpx").filters if isinstance(f, RedactSecrets)]
    assert filters and "MOJEEK_SECRET_VALUE" in filters[0].secrets
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.NOTSET)


def test_oversized_bodies_are_refused_before_auth():
    with client(stubs.organic("a")) as c:
        big = c.post("/v1/search", content=b"x" * 70_000,
                     headers={"Content-Type": "application/json"})
        ok = c.post("/v1/search", json={"q": "coffee"}, headers=AUTH)
    assert big.status_code == 413 and ok.status_code == 200
    assert ok.headers["x-content-type-options"] == "nosniff"


def test_auth_off_answers_only_loopback_hosts():
    with client(stubs.organic("a"), api_keys="", auth_disabled=True) as c:
        rebound = c.get("/v1/search", params={"q": "x"}, headers={"Host": "rebind.example"})
        local = c.get("/v1/search", params={"q": "x"}, headers={"Host": "127.0.0.1:8000"})
    assert rebound.status_code == 421 and local.status_code == 200


def test_unreachable_state_store_is_a_503_not_a_500():
    with client(stubs.organic("a"), state_backend="redis://127.0.0.1:1/0") as c:
        response = c.get("/v1/search", params={"q": "x"}, headers=AUTH)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "store_unavailable"


def test_startup_names_misspelled_engines(caplog):
    with caplog.at_level(logging.WARNING, logger="fairserp"), \
            client(stubs.organic("a"), organic_priority="a,brvae"):
        pass
    assert "brvae" in caplog.text
