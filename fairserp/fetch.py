"""Guarded page fetching for agents: compact, cited evidence from one URL.

This is the only place a caller-supplied URL is fetched, so it carries the SSRF guard:
- only http(s), on ports 80/443 unless the operator allows more;
- every resolved address must be public (no loopback, private, link-local, CGNAT,
  multicast, reserved or unspecified ranges); the request is pinned to the checked address
  so a second DNS answer cannot rebind it to an internal host;
- redirects are followed by hand, at most three, and each hop is checked again;
- response size, time and content type are capped.

It also respects robots.txt (RFC 9309): a disallowed path is refused, an unreachable or
5xx robots.txt counts as a full disallow, a 4xx counts as allow. It never retries around a
block and does not evade bot protection.
"""

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from fairserp.config import Settings
from fairserp.results import passages
from fairserp.robots import Robots
from fairserp.store import Store

MAX_REDIRECTS = 3
MAX_BYTES = 2_000_000
ROBOTS_MAX_BYTES = 512_000
ROBOTS_TTL = 3600
ALLOWED_TYPES = ("text/html", "application/xhtml+xml", "text/plain")
ROBOTS_TOKEN = "fairserp"
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


class FetchError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass
class Evidence:
    url: str
    final_url: str
    title: str
    published_at: str | None
    updated_at: str | None
    retrieved_at: str
    passages: list[str]
    content_type: str
    chars: int
    robots: str = "allowed"
    redirects: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast and ip not in _CGNAT


class Fetcher:
    def __init__(self, settings: Settings, cache: Store | None = None,
                 resolver=None, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._settings = settings
        self._cache = cache
        self._resolver = resolver or _resolve
        self._client = httpx.AsyncClient(
            timeout=settings.http_timeout_seconds, follow_redirects=False, trust_env=False,
            headers={"User-Agent": settings.user_agent,
                     "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8"},
            transport=transport)

    async def close(self) -> None:
        await self._client.aclose()

    async def fetch(self, url: str, query: str = "") -> Evidence:
        redirects: list[str] = []
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            target = await self._check(current)
            await self._check_robots(current)
            response, body = await self._get(current, target)
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                if not location:
                    raise FetchError("redirect without a Location header", 502)
                current = urljoin(current, location)
                redirects.append(current)
                continue
            if response.status_code >= 400:
                raise FetchError(f"the page answered HTTP {response.status_code}", 502)
            return self._evidence(url, current, response, body, query, redirects)
        raise FetchError(f"more than {MAX_REDIRECTS} redirects", 502)

    async def _check(self, url: str) -> str:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise FetchError("only http and https URLs can be fetched")
        if parts.username or parts.password:
            raise FetchError("URLs with credentials are not fetched")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        if port not in (80, 443):
            raise FetchError("only ports 80 and 443 can be fetched")
        try:
            addresses = [str(ipaddress.ip_address(parts.hostname))]
        except ValueError:
            addresses = None
        try:
            addresses = addresses or await self._resolver(parts.hostname, port)
        except OSError as exc:
            raise FetchError(f"cannot resolve {parts.hostname}", 502) from exc
        if not addresses:
            raise FetchError(f"cannot resolve {parts.hostname}", 502)
        for address in addresses:
            if not is_public(address):
                raise FetchError(f"{parts.hostname} resolves to a non-public address; "
                                 "fetching internal networks is not allowed", 403)
        return addresses[0]

    async def _get(self, url: str, address: str) -> tuple[httpx.Response, bytes]:
        parts = urlsplit(url)
        host = f"[{address}]" if ":" in address else address
        netloc = host if parts.port is None else f"{host}:{parts.port}"
        pinned = urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))
        headers = {"Host": parts.netloc.rsplit("@", 1)[-1]}
        extensions = {"sni_hostname": parts.hostname} if parts.scheme == "https" else {}
        request = self._client.build_request("GET", pinned, headers=headers,
                                             extensions=extensions)
        try:
            response = await self._client.send(request, stream=True)
        except httpx.TimeoutException as exc:
            raise FetchError("the page timed out", 504) from exc
        except httpx.HTTPError as exc:
            raise FetchError(f"could not connect: {type(exc).__name__}", 502) from exc
        try:
            if response.status_code in (301, 302, 303, 307, 308) or response.status_code >= 400:
                return response, b""
            kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if kind and kind not in ALLOWED_TYPES:
                raise FetchError(f"unsupported content type {kind!r}", 415)
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > MAX_BYTES:
                    break
            return response, bytes(body[:MAX_BYTES])
        finally:
            await response.aclose()

    async def _check_robots(self, url: str) -> None:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        rules = await self._robots(origin)
        if rules is None:
            raise FetchError(f"{origin}/robots.txt could not be read; RFC 9309 treats that "
                             "as a full disallow", 403)
        if not rules.allowed(ROBOTS_TOKEN, url):
            raise FetchError(f"robots.txt at {origin} disallows this path for automated "
                             "agents", 403)

    async def _robots(self, origin: str) -> Robots | None:
        key = f"robots:{origin}"
        text = None
        if self._cache is not None:
            cached = await self._cache.get_json(key)
            if cached is not None:
                text = cached.get("text")
                if cached.get("unreachable"):
                    return None
        if text is None:
            robots_url = f"{origin}/robots.txt"
            try:
                address = await self._check(robots_url)
                response, body = await self._get_robots(robots_url, address)
            except FetchError:
                return await self._remember(key, None)
            if response.status_code >= 500:
                return await self._remember(key, None)
            text = "" if response.status_code >= 400 else body.decode("utf-8", "replace")
            await self._remember(key, text)
        return Robots(text)

    async def _get_robots(self, url: str, address: str) -> tuple[httpx.Response, bytes]:
        parts = urlsplit(url)
        host = f"[{address}]" if ":" in address else address
        pinned = urlunsplit((parts.scheme, host if parts.port is None else
                             f"{host}:{parts.port}", parts.path, "", ""))
        extensions = {"sni_hostname": parts.hostname} if parts.scheme == "https" else {}
        request = self._client.build_request("GET", pinned, headers={"Host": parts.netloc},
                                             extensions=extensions)
        try:
            response = await self._client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise FetchError("robots.txt unreachable", 502) from exc
        try:
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > ROBOTS_MAX_BYTES:
                    break
            return response, bytes(body[:ROBOTS_MAX_BYTES])
        finally:
            await response.aclose()

    async def _remember(self, key: str, text: str | None) -> Robots | None:
        if self._cache is not None:
            value = {"unreachable": True} if text is None else {"text": text}
            await self._cache.set_json(key, value, ROBOTS_TTL)
        return None if text is None else Robots(text)

    def _evidence(self, url: str, final: str, response: httpx.Response, body: bytes,
                  query: str, redirects: list[str]) -> Evidence:
        kind = response.headers.get("content-type", "text/html").split(";")[0].strip()
        text = body.decode(response.encoding or "utf-8", "replace")
        if kind == "text/plain":
            page = Page(title="", text=text, published=None, updated=None)
        else:
            page = extract(text)
        chosen = passages(page.text, query, limit=5, max_chars=400) if query else \
            passages(page.text, page.title, limit=3, max_chars=400)
        return Evidence(
            url=url, final_url=final, title=page.title, published_at=page.published,
            updated_at=page.updated,
            retrieved_at=datetime.now(UTC).isoformat(timespec="seconds"),
            passages=chosen, content_type=kind, chars=len(page.text), redirects=redirects,
        )


async def _resolve(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(info[4][0] for info in infos))


@dataclass
class Page:
    title: str
    text: str
    published: str | None
    updated: str | None


_SKIP = {"script", "style", "noscript", "nav", "footer", "header", "aside", "form", "svg",
         "button", "iframe", "template"}
_BLOCK = {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "td", "th", "pre", "blockquote",
          "dd", "dt", "figcaption", "div", "section", "article", "br", "tr"}
_PUBLISHED = {"article:published_time", "og:published_time", "datepublished",
              "date", "dc.date", "dc.date.issued", "pubdate", "publishdate"}
_UPDATED = {"article:modified_time", "og:updated_time", "datemodified", "last-modified",
            "dc.date.modified"}


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta: dict[str, str] = {}
        self.blocks: list[str] = []
        self._current: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self._time: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {k.lower(): (v or "") for k, v in attrs}
        if tag in _SKIP:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            name = (attributes.get("property") or attributes.get("name")
                    or attributes.get("itemprop") or "").lower()
            if name and attributes.get("content"):
                self.meta.setdefault(name, attributes["content"])
        elif tag == "time" and attributes.get("datetime") and self._time is None:
            self._time = attributes["datetime"]
        if tag in _BLOCK:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        if tag in _BLOCK:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._skip_depth:
            self._current.append(data)

    def _flush(self) -> None:
        block = re.sub(r"\s+", " ", "".join(self._current)).strip()
        if len(block) > 1:
            self.blocks.append(block)
        self._current = []


def extract(html: str) -> Page:
    parser = _Extractor()
    parser.feed(html)
    parser.close()
    parser._flush()
    meta = parser.meta
    title = meta.get("og:title") or re.sub(r"\s+", " ", parser.title).strip()
    published = next((meta[k] for k in _PUBLISHED if k in meta), None) or parser._time
    updated = next((meta[k] for k in _UPDATED if k in meta), None)
    # Keep blocks that read like prose; drop menus and short labels.
    prose = [b for b in parser.blocks if len(b) >= 40 or b.endswith((".", "?", "!"))]
    return Page(title=title, text=" ".join(prose), published=published, updated=updated)

