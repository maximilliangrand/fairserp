"""The Python client and LangChain integration send every request to the configured
base URL, sync and async, with bearer auth and never a key in the query string."""

import json

import httpx
import pytest

from fairserp.client import AsyncFairserp, Fairserp, FairserpError
from fairserp.integrations.langchain import FairserpAPIWrapper, FairserpSearchTool

BASE = "http://fairserp.internal:8000"
OUTCOME = {
    "id": "abc", "status": "success", "created_at": "2026-10-23 10:00:00 UTC",
    "elapsed_ms": 12, "query": {"q": "rust"}, "requested_engine": "auto",
    "resolved_engine": "auto", "params": {"applied": [], "ignored": []},
    "providers": [{"engine": "brave", "role": "organic", "status": "ok"}],
    "results": [{"url": "https://www.rust-lang.org/", "title": "Rust", "snippet": "Rust lang",
                 "rank": 1, "canonical_url": "https://rust-lang.org",
                 "sources": [{"engine": "brave", "rank": 1, "provenance": "own-index"}]}],
}
SERPAPI = {"organic_results": [{"position": 1, "snippet": "Rust lang"}]}


def recorder():
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = SERPAPI if request.url.path == "/search.json" else OUTCOME
        return httpx.Response(200, json=body)

    return seen, handle


def assert_all_local(seen):
    assert seen
    for request in seen:
        assert request.url.host == "fairserp.internal" and request.url.port == 8000
        assert request.headers["Authorization"] == "Bearer k"
        assert "api_key" not in request.url.params


def test_sync_client():
    seen, handle = recorder()
    with Fairserp(BASE, "k", transport=httpx.MockTransport(handle)) as client:
        outcome = client.search("rust", freshness="m")
    assert outcome.results[0].sources[0].engine == "brave"
    assert json.loads(seen[0].content) == {"q": "rust", "freshness": "m"}
    assert_all_local(seen)


async def test_async_client():
    seen, handle = recorder()
    async with AsyncFairserp(BASE, "k", transport=httpx.MockTransport(handle)) as client:
        await client.search("rust")
        await client.serpapi({"engine": "google", "q": "rust"})
    assert_all_local(seen)


def test_base_url_is_mandatory():
    with pytest.raises(ValueError):
        Fairserp("", "k")
    with pytest.raises(ValueError):
        Fairserp("serpapi.com", "k")


def test_errors_carry_the_server_message():
    transport = httpx.MockTransport(lambda r: httpx.Response(
        403, json={"error": {"code": "plan_rejected", "message": "firecrawl is opt-in"}}))
    with pytest.raises(FairserpError, match="firecrawl is opt-in"):
        Fairserp(BASE, "k", transport=transport).search("x")


async def test_langchain_tool_sync_and_async_stay_on_base_url():
    seen, handle = recorder()
    tool = FairserpSearchTool(base_url=BASE, api_key="k")
    tool._sync = Fairserp(BASE, "k", transport=httpx.MockTransport(handle))
    tool._async = AsyncFairserp(BASE, "k", transport=httpx.MockTransport(handle))
    text = tool.invoke("rust")
    atext = await tool.ainvoke("rust")
    assert "[1] Rust" in text and "via brave" in atext
    assert_all_local(seen)


async def test_langchain_wrapper_sync_and_async_stay_on_base_url():
    seen, handle = recorder()
    wrapper = FairserpAPIWrapper(BASE, "k", transport=httpx.MockTransport(handle),
                                 async_transport=httpx.MockTransport(handle))
    assert wrapper.run("rust") == "Rust lang"
    assert await wrapper.arun("rust") == "Rust lang"
    assert_all_local(seen)
    assert all(r.url.params["engine"] == "google" for r in seen)
