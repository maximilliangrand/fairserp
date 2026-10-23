"""Outbound HTTP: one shared client per proxy, plus per-host politeness spacing.

Proxies exist for egress control (corporate networks, fixed egress IPs, data residency).
fairserp never rotates proxies to retry a blocked or challenged request: an upstream
403, 429 or bot challenge is surfaced to the caller as an engine error.
"""

import asyncio
import itertools
import time
from urllib.parse import urlsplit

import httpx

from fairserp.config import Settings

# Minimum seconds between requests to hosts whose usage policies ask for it.
HOST_MIN_INTERVAL = {
    "nominatim.openstreetmap.org": 1.0,
}


class HttpPool:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._clients: dict[str | None, httpx.AsyncClient] = {}
        proxies = settings.proxy_list()
        self._rotation = itertools.cycle(proxies) if proxies else None
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._host_last: dict[str, float] = {}

    def _proxy_for(self, engine: str) -> str | None:
        override = self._settings.engine_proxies.get(engine)
        if override:
            return None if override == "none" else override
        return next(self._rotation) if self._rotation else None

    def _client(self, proxy: str | None) -> httpx.AsyncClient:
        if proxy not in self._clients:
            self._clients[proxy] = httpx.AsyncClient(
                proxy=proxy,
                timeout=self._settings.http_timeout_seconds,
                headers={"User-Agent": self._settings.user_agent},
                follow_redirects=True,
            )
        return self._clients[proxy]

    async def request(self, engine: str, method: str, url: str, **kwargs) -> httpx.Response:
        await self._respect_host_interval(url)
        client = self._client(self._proxy_for(engine))
        return await client.request(method, url, **kwargs)

    async def _respect_host_interval(self, url: str) -> None:
        host = urlsplit(url).hostname or ""
        interval = HOST_MIN_INTERVAL.get(host)
        if not interval:
            return
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            wait = self._host_last.get(host, 0.0) + interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._host_last[host] = time.monotonic()

    async def close(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()
