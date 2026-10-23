"""Outbound HTTP for engine adapters.

One shared client per egress route. A deployment has at most one global proxy plus sticky
per-engine overrides; there is no rotation across a pool, because spreading requests over
many IPs is how per-IP limits get evaded. Redirects are not followed and environment proxy
variables are ignored unless the operator turns trust_env on.
"""

import httpx

from fairserp.config import Settings


class HttpPool:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._clients: dict[str | None, httpx.AsyncClient] = {}

    def proxy_for(self, engine: str) -> str | None:
        override = self._settings.engine_proxies.get(engine)
        if override:
            return None if override == "none" else override
        return self._settings.proxy or None

    def _client(self, proxy: str | None) -> httpx.AsyncClient:
        if proxy not in self._clients:
            self._clients[proxy] = httpx.AsyncClient(
                proxy=proxy,
                timeout=self._settings.http_timeout_seconds,
                headers={"User-Agent": self._settings.user_agent},
                follow_redirects=False,
                trust_env=self._settings.http_trust_env,
            )
        return self._clients[proxy]

    async def request(self, engine: str, method: str, url: str, **kwargs) -> httpx.Response:
        return await self._client(self.proxy_for(engine)).request(method, url, **kwargs)

    async def close(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()
