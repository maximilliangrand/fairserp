"""Fetch hardening: embedded IPv4 in IPv6, malformed ports, the overall deadline, the
concurrency cap, extraction off the event loop, and robots.txt redirects."""

import asyncio
import threading

import httpx
import pytest

from fairserp import fetch as fetch_module
from fairserp.fetch import MAX_CONCURRENT_FETCHES, Fetcher, FetchError, is_public
from tests.conftest import settings
from tests.test_fetch import PUBLIC, ROBOTS, fake_dns, fetcher, html, status, text, transport


@pytest.mark.parametrize("address", [
    "64:ff9b::a9fe:a9fe",    # NAT64 of 169.254.169.254
    "64:ff9b::7f00:1",       # NAT64 of 127.0.0.1
    "64:ff9b::a00:7",        # NAT64 of 10.0.0.7
    "64:ff9b:1::a9fe:a9fe",  # local-use NAT64
    "64:ff9b:1::808:808",
    "::7f00:1",              # IPv4-compatible 127.0.0.1
    "::a9fe:a9fe",
    "::",
    "fec0::1",               # deprecated site-local
    "::ffff:127.0.0.1",      # mapped, still refused
    "2002:7f00:1::",         # 6to4, still refused
    "2001:0:4136:e378:8000:63bf:3fff:fdd2",  # Teredo, still refused
])
def test_embedded_ipv4_and_site_local_are_not_public(address):
    assert not is_public(address)


def test_nat64_of_a_public_address_stays_public():
    assert is_public("64:ff9b::808:808") and is_public("::ffff:8.8.8.8")


async def test_nat64_literal_is_refused_before_any_request():
    f, seen = fetcher({})
    with pytest.raises(FetchError, match="non-public"):
        await f.fetch("http://[64:ff9b::a9fe:a9fe]/latest/meta-data/")
    assert not seen


@pytest.mark.parametrize("url", ["http://example.org:99999/", "http://example.org:abc/"])
async def test_malformed_port_is_a_client_error(url):
    f, seen = fetcher({})
    with pytest.raises(FetchError, match="invalid port") as caught:
        await f.fetch(url)
    assert caught.value.status == 400 and not seen


class _Drip(httpx.AsyncByteStream):
    async def __aiter__(self):
        for _ in range(1000):
            await asyncio.sleep(0.01)
            yield b"a"


async def test_dripping_server_hits_the_overall_deadline():
    def drip(request):
        return httpx.Response(200, stream=_Drip(), headers={"content-type": "text/plain"})
    mock, _ = transport({("example.org", "/robots.txt"): text(""), ("example.org", "/slow"): drip})
    f = Fetcher(settings(http_timeout_seconds=0.05), None, resolver=fake_dns(PUBLIC),
                transport=mock)
    with pytest.raises(FetchError, match="took longer") as caught:
        await f.fetch("https://example.org/slow")
    assert caught.value.status == 504


async def test_concurrent_fetches_are_capped():
    active = 0
    peak = 0
    release = asyncio.Event()

    async def slow_page(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await release.wait()
        active -= 1
        return httpx.Response(200, text="<p>Plenty of words here to count as prose.</p>",
                              headers={"content-type": "text/html"})

    f, _ = fetcher({("example.org", "/robots.txt"): text(""), ("example.org", "/p"): slow_page})
    total = MAX_CONCURRENT_FETCHES + 4
    tasks = [asyncio.create_task(f.fetch("https://example.org/p")) for _ in range(total)]
    for _ in range(1000):
        if active == MAX_CONCURRENT_FETCHES:
            break
        await asyncio.sleep(0)
    for _ in range(50):
        await asyncio.sleep(0)
    assert active == MAX_CONCURRENT_FETCHES
    release.set()
    results = await asyncio.gather(*tasks)
    assert len(results) == total and peak == MAX_CONCURRENT_FETCHES


async def test_html_extraction_runs_off_the_event_loop(monkeypatch):
    threads = []
    original = fetch_module.extract

    def recording(markup):
        threads.append(threading.get_ident())
        return original(markup)

    monkeypatch.setattr(fetch_module, "extract", recording)
    f, _ = fetcher({("example.org", "/robots.txt"): text(""), ("example.org", "/t"): html()})
    assert (await f.fetch("https://example.org/t")).title == "Tokio tutorial"
    assert threads and threads[0] != threading.get_ident()


async def test_robots_redirect_is_followed():
    f, _ = fetcher({("example.org", "/robots.txt"): status(301, location="/real-robots.txt"),
                    ("example.org", "/real-robots.txt"): text(ROBOTS),
                    ("example.org", "/no-agents"): html(),
                    ("example.org", "/ok"): html()})
    with pytest.raises(FetchError, match="disallows"):
        await f.fetch("https://example.org/no-agents")
    assert (await f.fetch("https://example.org/ok")).title == "Tokio tutorial"


def _robots_chain(hops):
    routes = {("example.org", "/robots.txt"): status(302, location="/r1")}
    for hop in range(1, hops):
        routes[("example.org", f"/r{hop}")] = status(302, location=f"/r{hop + 1}")
    routes[("example.org", f"/r{hops}")] = text("")
    routes[("example.org", "/page")] = html()
    return routes


async def test_robots_follows_five_redirects_and_refuses_six():
    f, _ = fetcher(_robots_chain(5))
    assert (await f.fetch("https://example.org/page")).title == "Tokio tutorial"
    g, seen = fetcher(_robots_chain(6))
    with pytest.raises(FetchError, match="full disallow"):
        await g.fetch("https://example.org/page")
    assert all(r.url.path != "/page" for r in seen)


async def test_robots_redirect_hops_are_rechecked():
    f, seen = fetcher({("example.org", "/robots.txt"):
                       status(302, location="http://internal.example/robots.txt"),
                       ("example.org", "/page"): html()})
    with pytest.raises(FetchError, match="full disallow"):
        await f.fetch("https://example.org/page")
    assert all(r.headers["host"] != "internal.example" for r in seen)
    g, _ = fetcher({("example.org", "/robots.txt"):
                    status(302, location="http://example.org:8080/robots.txt"),
                    ("example.org", "/page"): html()})
    with pytest.raises(FetchError, match="full disallow"):
        await g.fetch("https://example.org/page")


async def test_robots_redirect_without_location_is_unreachable():
    f, _ = fetcher({("example.org", "/robots.txt"): status(302),
                    ("example.org", "/page"): html()})
    with pytest.raises(FetchError, match="full disallow"):
        await f.fetch("https://example.org/page")


async def test_robots_redirect_to_a_missing_file_allows():
    f, _ = fetcher({("example.org", "/robots.txt"): status(301, location="/gone"),
                    ("example.org", "/page"): html()})  # /gone answers 404
    assert (await f.fetch("https://example.org/page")).title == "Tokio tutorial"
