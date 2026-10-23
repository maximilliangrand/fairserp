"""Guarded fetch: SSRF defenses, robots.txt per RFC 9309, extraction, and the MCP tools."""

import httpx
import pytest

from fairserp.fetch import Fetcher, FetchError, extract, is_public
from fairserp.robots import Robots
from tests.conftest import settings

PAGE = """<html><head><title>Tokio tutorial</title>
<meta property="article:published_time" content="2025-06-01">
<meta property="article:modified_time" content="2026-09-30"></head>
<body><nav>Home | Docs | Blog</nav><script>var x = 1;</script>
<p>Tokio is an asynchronous runtime for the Rust programming language.</p>
<p>It provides the building blocks needed for writing networking applications.</p>
<p>The select! macro waits on multiple concurrent branches and returns when the first completes.</p>
<footer>Copyright</footer></body></html>"""

ROBOTS = """User-agent: *
Disallow: /private
Disallow: /*.pdf$
Allow: /private/ok

User-agent: fairserp
Disallow: /no-agents
"""


def fake_dns(table):
    async def resolve(host, port):
        if host not in table:
            raise OSError("nxdomain")
        return table[host]
    return resolve


def transport(routes):
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        key = (request.headers["host"], request.url.path)
        if key not in routes:
            return httpx.Response(404)
        return routes[key](request)
    return httpx.MockTransport(handle), seen


def status(code, **headers):
    return lambda r: httpx.Response(code, headers=headers)


def html(body=PAGE):
    return lambda r: httpx.Response(200, text=body, headers={"content-type": "text/html"})


def text(body):
    return lambda r: httpx.Response(200, text=body, headers={"content-type": "text/plain"})


PUBLIC = {"example.org": ["93.184.215.14"], "evil.example": ["93.184.215.15"],
          "internal.example": ["10.0.0.7"], "rebind.example": ["93.184.215.16", "127.0.0.1"]}


def fetcher(routes):
    mock, seen = transport(routes)
    return Fetcher(settings(), None, resolver=fake_dns(PUBLIC), transport=mock), seen


async def test_fetch_returns_cited_passages_and_dates():
    f, seen = fetcher({("example.org", "/robots.txt"): text(ROBOTS),
                       ("example.org", "/tokio"): html()})
    evidence = await f.fetch("https://example.org/tokio", "select macro concurrent branches")
    assert evidence.title == "Tokio tutorial"
    assert evidence.published_at == "2025-06-01" and evidence.updated_at == "2026-09-30"
    assert evidence.passages[0].startswith("The select! macro")
    assert "Home | Docs" not in " ".join(evidence.passages)
    page_request = seen[-1]
    assert page_request.url.host == "93.184.215.14"  # pinned to the checked address
    assert page_request.headers["host"] == "example.org"
    assert page_request.extensions["sni_hostname"] == "example.org"


@pytest.mark.parametrize("url,reason", [
    ("file:///etc/passwd", "only http"),
    ("ftp://example.org/x", "only http"),
    ("http://example.org:8080/", "ports 80 and 443"),
    ("http://user:pw@example.org/", "credentials"),
    ("http://127.0.0.1/", "non-public"),
    ("http://169.254.169.254/latest/meta-data/", "non-public"),
    ("http://[::1]/", "non-public"),
    ("http://[::ffff:127.0.0.1]/", "non-public"),
    ("http://100.64.0.1/", "non-public"),
    ("http://0.0.0.0/", "non-public"),
    ("http://internal.example/", "non-public"),
    ("http://rebind.example/", "non-public"),
])
async def test_ssrf_guard(url, reason):
    f, seen = fetcher({})
    with pytest.raises(FetchError, match=reason):
        await f.fetch(url)
    assert not [r for r in seen if r.url.path != "/robots.txt"]


async def test_redirect_hops_are_rechecked():
    f, seen = fetcher({("example.org", "/robots.txt"): text(""),
                       ("example.org", "/go"): status(302, location="http://internal.example/")})
    with pytest.raises(FetchError, match="non-public"):
        await f.fetch("https://example.org/go")
    assert all(r.headers["host"] != "internal.example" for r in seen)


async def test_redirect_limit():
    f, _ = fetcher({("example.org", "/robots.txt"): text(""),
                    ("example.org", "/go"): status(302, location="/go")})
    with pytest.raises(FetchError, match="redirects"):
        await f.fetch("https://example.org/go")


async def test_robots_disallow_and_unreachable_robots():
    f, _ = fetcher({("example.org", "/robots.txt"): text(ROBOTS),
                    ("example.org", "/no-agents"): html()})
    with pytest.raises(FetchError, match="disallows"):
        await f.fetch("https://example.org/no-agents")
    g, _ = fetcher({("evil.example", "/robots.txt"): status(503),
                    ("evil.example", "/"): html()})
    with pytest.raises(FetchError, match="full disallow"):
        await g.fetch("https://evil.example/")


async def test_missing_robots_allows():
    f, _ = fetcher({("example.org", "/page"): html()})  # robots.txt 404
    assert (await f.fetch("https://example.org/page")).title == "Tokio tutorial"


async def test_content_type_and_size_caps():
    f, _ = fetcher({("example.org", "/robots.txt"): text(""),
                    ("example.org", "/a.pdf"): status(200, **{"content-type": "application/pdf"}),
                    ("example.org", "/huge"): text("a" * 3_000_000)})
    with pytest.raises(FetchError, match="content type"):
        await f.fetch("https://example.org/a.pdf")
    assert (await f.fetch("https://example.org/huge")).chars <= 2_000_000


def test_robots_rfc9309_semantics():
    robots = Robots(ROBOTS)
    assert not robots.allowed("Mozilla/5.0 fairserp/0.2", "https://x.org/no-agents")
    assert robots.allowed("fairserp", "https://x.org/private")  # its own group replaces *
    star = Robots(ROBOTS.split("\n\n")[0])
    assert not star.allowed("other", "https://x.org/private/secret")
    assert star.allowed("other", "https://x.org/private/ok")  # longer Allow wins
    assert not star.allowed("other", "https://x.org/files/report.pdf")
    assert star.allowed("other", "https://x.org/files/report.pdf?download=1")
    github = Robots("User-agent: *\nDisallow: /search$\n")
    assert not github.allowed("x", "https://github.com/search")
    assert github.allowed("x", "https://github.com/search?q=x")
    assert Robots("").allowed("x", "https://x.org/anything")


def test_public_address_classification():
    assert is_public("93.184.215.14") and is_public("2606:4700::1111")
    for address in ["10.1.2.3", "172.16.0.1", "192.168.1.1", "127.0.0.1", "::1",
                    "fe80::1", "fc00::1", "100.64.0.1", "224.0.0.1", "::ffff:10.0.0.1"]:
        assert not is_public(address), address


def test_extract_skips_navigation_and_scripts():
    page = extract(PAGE)
    assert "var x" not in page.text and "Copyright" not in page.text
    assert page.text.startswith("Tokio is an asynchronous runtime")


async def test_mcp_tools_use_the_same_policy():
    from fairserp.core import Searcher
    from fairserp.http import HttpPool
    from fairserp.mcp_server import Runtime, build
    from fairserp.store import MemoryStore
    from tests import stubs

    s = settings(organic_priority="a", upstream_budgets="")
    registry = {"a": stubs.organic("a"), "wikidata": stubs.entity_engine(),
                "duckduckgo_ia": stubs.answer_engine(), "openstreetmap": stubs.local_engine()}
    f, _ = fetcher({("example.org", "/page"): html()})
    runtime = Runtime(Searcher(s, registry, HttpPool(s), MemoryStore(100), MemoryStore()), f,
                      HttpPool(s))
    server = build(s, runtime)
    result = await server.call_tool("web_search", {"query": "coffee", "num": 3})
    payload = result.structured_content if hasattr(result, "structured_content") else result
    text_blob = str(payload)
    assert "a.example/0" in text_blob and "a#1 (own-index)" in text_blob
    fetched = str(await server.call_tool("fetch", {"url": "https://example.org/page"}))
    assert "Tokio tutorial" in fetched
    refused = str(await server.call_tool("fetch", {"url": "http://127.0.0.1/"}))
    assert "refused" in refused
